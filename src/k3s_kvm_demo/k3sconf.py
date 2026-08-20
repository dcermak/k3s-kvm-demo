"""The two files firstboot drops into a guest.

``/etc/rancher/k3s/config.yaml`` is emitted as JSON.  JSON is a subset of
YAML 1.2, so a token or SAN containing quotes, newlines, ``#`` or ``$(...)``
is quoted correctly by construction instead of by hand.  k3s reads that path
for both roles regardless of how it was installed.

The unit is written by us rather than reusing the distribution's
``k3s-server.service``/``k3s-agent.service``, so the role decision lives in one
place and the result does not depend on how k3s was packaged into the image.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

import jinja2

from . import meta

UNIT_NAME = "k3s-node.service"
UNIT_PATH = f"/etc/systemd/system/{UNIT_NAME}"
CONFIG_DIR = "/etc/rancher/k3s"
CONFIG_PATH = f"{CONFIG_DIR}/config.yaml"
STATE_DIR = "/var/lib/k3s-kvm-demo"
DONE_MARKER = f"{STATE_DIR}/firstboot.done"
RANCHER_DIR = "/var/lib/rancher/k3s"

#: Passed as ``sh -s <marker>`` so a timed-out run stays findable by ``pkill -f``.
PROCESS_MARKER = "k3s-firstboot"

#: Terminal-condition probe.  Built only from module constants, never from
#: configuration, so passing it to ``sh -c`` introduces no injection surface.
PROBE_COMMAND = f"test -e {DONE_MARKER} || systemctl is-active --quiet {UNIT_NAME}"

#: Exit status firstboot uses for "another run holds the lock" (EX_TEMPFAIL).
EX_TEMPFAIL = 75


class InvalidUnitValue(ValueError):
    """A systemd unit value cannot span lines."""


@dataclass(frozen=True, slots=True)
class GuestPaths:
    """Where firstboot writes inside the guest.

    Overridable only so the test suite can run the real script against a
    scratch directory instead of the system ones.
    """

    state_dir: str = STATE_DIR
    config_dir: str = CONFIG_DIR
    config_path: str = CONFIG_PATH
    unit_name: str = UNIT_NAME
    unit_path: str = UNIT_PATH
    rancher_dir: str = RANCHER_DIR

    @classmethod
    def under(cls, root: str, *, unit_name: str = UNIT_NAME) -> GuestPaths:
        return cls(
            state_dir=f"{root}/var/lib/k3s-kvm-demo",
            config_dir=f"{root}/etc/rancher/k3s",
            config_path=f"{root}/etc/rancher/k3s/config.yaml",
            unit_name=unit_name,
            unit_path=f"{root}/etc/systemd/system/{unit_name}",
            rancher_dir=f"{root}/var/lib/rancher/k3s",
        )


def config_yaml(
    *,
    node_name: str,
    role: str,
    token: str,
    is_bootstrap: bool,
    server_url: str | None,
    tls_san: Sequence[str] = (),
) -> str:
    """Render ``/etc/rancher/k3s/config.yaml``."""
    if role not in meta.ROLES:
        raise ValueError(f"unknown role {role!r}")
    if role == meta.ROLE_SERVER:
        if is_bootstrap and server_url:
            raise ValueError("the bootstrap server must not be given a server URL")
        if not is_bootstrap and not server_url:
            raise ValueError("a joining server needs a server URL")
    else:
        if is_bootstrap:
            raise ValueError("an agent cannot bootstrap the cluster")
        if not server_url:
            raise ValueError("an agent needs a server URL")

    document: dict[str, object] = {"node-name": node_name, "token": token}

    if role == meta.ROLE_SERVER:
        document["write-kubeconfig-mode"] = "0644"
        if is_bootstrap:
            document["cluster-init"] = True
        else:
            document["server"] = server_url
        if tls_san:
            document["tls-san"] = list(tls_san)
    else:
        document["server"] = server_url

    return json.dumps(document, indent=2, sort_keys=True) + "\n"


def systemd_unit(role: str) -> str:
    """Render the unit that runs k3s in the guest."""
    if role not in meta.ROLES:
        raise ValueError(f"unknown role {role!r}")
    subcommand = "server" if role == meta.ROLE_SERVER else "agent"

    sections: list[tuple[str, list[tuple[str, str]]]] = [
        (
            "Unit",
            [
                ("Description", f"k3s {subcommand} (k3s-kvm-demo)"),
                ("Documentation", "https://k3s.io"),
                ("Wants", "network-online.target"),
                ("After", "network-online.target"),
            ],
        ),
        (
            "Service",
            [
                ("Type", "notify"),
                ("ExecStartPre", "-/sbin/modprobe br_netfilter"),
                ("ExecStartPre", "-/sbin/modprobe overlay"),
                # 'env' resolves k3s wherever the image put it: /usr/bin for the
                # openSUSE RPM, /usr/local/bin for the upstream installer.
                ("ExecStart", f"/usr/bin/env k3s {subcommand}"),
                ("KillMode", "process"),
                ("Delegate", "yes"),
                ("Restart", "always"),
                ("RestartSec", "5s"),
                ("TimeoutStartSec", "0"),
                ("LimitNOFILE", "1048576"),
                ("LimitNPROC", "infinity"),
                ("LimitCORE", "infinity"),
                ("TasksMax", "infinity"),
            ],
        ),
        ("Install", [("WantedBy", "multi-user.target")]),
    ]
    return _render_unit(sections)


def _render_unit(sections: Iterable[tuple[str, list[tuple[str, str]]]]) -> str:
    lines: list[str] = []
    for name, entries in sections:
        if lines:
            lines.append("")
        lines.append(f"[{name}]")
        for key, value in entries:
            if "\n" in value or "\r" in value:
                raise InvalidUnitValue(f"{key} value must not contain a newline")
            lines.append(f"{key}={value}")
    return "\n".join(lines) + "\n"


def _b64(text: str) -> str:
    return base64.b64encode(text.encode()).decode("ascii")


#: Rendered with autoescaping *off* — the output is shell, not HTML — and with
#: StrictUndefined so a renamed placeholder fails loudly instead of silently
#: producing a script with a hole in it.
_SHELL_ENV = jinja2.Environment(
    autoescape=False,  # noqa: S701 - shell output; payloads are base64
    undefined=jinja2.StrictUndefined,
    keep_trailing_newline=True,
)


def render_firstboot(
    template_path: Path,
    *,
    node_name: str,
    role: str,
    token: str,
    is_bootstrap: bool,
    server_url: str | None,
    tls_san: Sequence[str] = (),
    service_wait_s: int = 240,
    paths: GuestPaths | None = None,
) -> str:
    """Render the firstboot script for one node.

    The config and unit are handed over base64-encoded.  Base64 is
    ``[A-Za-z0-9+/=]``, so no value in them — token, URL, SAN — can close a
    quote, start a command or terminate a heredoc.  The only other
    substitutions are a validated DNS-1123 label and integers.

    *paths* exists so tests can drive the real script against a scratch
    directory; production always uses the defaults.
    """
    paths = paths or GuestPaths()
    config = config_yaml(
        node_name=node_name,
        role=role,
        token=token,
        is_bootstrap=is_bootstrap,
        server_url=server_url,
        tls_san=tls_san,
    )
    template = _SHELL_ENV.from_string(template_path.read_text())
    return template.render(
        state_dir=paths.state_dir,
        unit_name=paths.unit_name,
        unit_path=paths.unit_path,
        config_dir=paths.config_dir,
        config_path=paths.config_path,
        rancher_dir=paths.rancher_dir,
        node_name=node_name,
        config_b64=_b64(config),
        unit_b64=_b64(systemd_unit(role)),
        service_wait_s=int(service_wait_s),
    )
