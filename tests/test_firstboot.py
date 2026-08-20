"""The firstboot script, run for real by /bin/sh against a scratch root.

Reconciliation re-runs this script after a restart, so idempotency and the
crash-recovery markers are load-bearing rather than incidental.  Static checks
alone would not catch the case that mattered most: a crash after
``systemctl enable --now`` but before the done marker leaves k3s's data
directory behind, and an earlier design read that as a dirty golden image and
refused to continue.
"""

from __future__ import annotations

import base64
import fcntl
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest
import yaml

from k3s_kvm_demo import k3sconf, meta

from .conftest import FIRSTBOOT_TEMPLATE

UNIT = k3sconf.UNIT_NAME

SYSTEMCTL_SHIM = r"""#!/bin/bash
# Scripted stand-in for systemctl.
state_dir="$SHIM_STATE"
case "$1" in
  is-enabled)
    grep -qx "$2" "$state_dir/enabled" 2>/dev/null && exit 0 || exit 1 ;;
  daemon-reload)
    exit 0 ;;
  enable)
    if [ -n "$SHIM_FAIL_ENABLE" ]; then echo "boom" >&2; exit 99; fi
    echo "$2" >> "$state_dir/enabled"; exit 0 ;;
  start)
    shift
    while [ "${1#--}" != "$1" ]; do shift; done
    if [ -n "$SHIM_FAIL_START" ]; then echo "start failed" >&2; exit 98; fi
    printf %s "${SHIM_ACTIVE_AFTER_START:-active}" > "$state_dir/active"
    exit 0 ;;
  is-active)
    value="$(cat "$state_dir/active" 2>/dev/null || echo inactive)"
    echo "$value"
    [ "$value" = active ] && exit 0 || exit 3 ;;
esac
exit 1
"""

TRIVIAL_SHIM = "#!/bin/bash\nexit 0\n"
K3S_SHIM = "#!/bin/bash\n[ \"$1\" = --version ] && echo 'k3s version v1.33.4+k3s1'\nexit 0\n"


@dataclass
class Harness:
    root: Path
    paths: k3sconf.GuestPaths
    bin: Path
    state: Path

    def render(self, **overrides) -> str:
        kwargs = {
            "node_name": "k3s-node-" + "a" * 32,
            "role": meta.ROLE_AGENT,
            "token": "s3cr3t",
            "is_bootstrap": False,
            "server_url": "https://192.168.122.10:6443",
            "service_wait_s": 3,
        }
        kwargs.update(overrides)
        return k3sconf.render_firstboot(FIRSTBOOT_TEMPLATE, paths=self.paths, **kwargs)

    def run(self, script: str | None = None, **env) -> subprocess.CompletedProcess:
        environment = dict(os.environ)
        environment["PATH"] = f"{self.bin}:{environment['PATH']}"
        environment["SHIM_STATE"] = str(self.state)
        environment.update({k: str(v) for k, v in env.items() if v is not None})
        return subprocess.run(
            ["/bin/sh", "-s", "k3s-firstboot-marker"],
            input=script if script is not None else self.render(),
            capture_output=True,
            text=True,
            timeout=60,
            env=environment,
        )


@pytest.fixture
def harness(tmp_path: Path) -> Harness:
    root = tmp_path / "guest"
    bin_dir = tmp_path / "bin"
    state = tmp_path / "shim-state"
    bin_dir.mkdir()
    state.mkdir()
    for name, body in (
        ("systemctl", SYSTEMCTL_SHIM),
        ("hostnamectl", TRIVIAL_SHIM),
        ("journalctl", TRIVIAL_SHIM),
        ("k3s", K3S_SHIM),
    ):
        path = bin_dir / name
        path.write_text(body)
        path.chmod(0o755)
    return Harness(root=root, paths=k3sconf.GuestPaths.under(str(root)), bin=bin_dir, state=state)


# -- static properties -----------------------------------------------------


def test_rendered_script_is_valid_shell(harness):
    for role, bootstrap, url in (
        (meta.ROLE_SERVER, True, None),
        (meta.ROLE_SERVER, False, "https://s:6443"),
        (meta.ROLE_AGENT, False, "https://s:6443"),
    ):
        script = harness.render(role=role, is_bootstrap=bootstrap, server_url=url)
        check = subprocess.run(["/bin/sh", "-n"], input=script, capture_output=True, text=True)
        assert check.returncode == 0, check.stderr


def test_payloads_decode_to_exactly_the_generated_files(harness):
    script = harness.render(role=meta.ROLE_SERVER, is_bootstrap=True, server_url=None)
    blobs = re.findall(r"printf %s '([A-Za-z0-9+/=]+)'", script)
    assert len(blobs) == 2

    config, unit = (base64.b64decode(blob).decode() for blob in blobs)
    assert config == k3sconf.config_yaml(
        node_name="k3s-node-" + "a" * 32,
        role=meta.ROLE_SERVER,
        token="s3cr3t",
        is_bootstrap=True,
        server_url=None,
    )
    assert unit == k3sconf.systemd_unit(meta.ROLE_SERVER)


@pytest.mark.parametrize(
    "token",
    ["quote\"'inside", "line\nbreak", "EOF", "$(touch /tmp/pwned)", "'; rm -rf /; #"],
)
def test_hostile_token_never_appears_unencoded(harness, token):
    script = harness.render(token=token)
    assert token not in script
    # ...but it still arrives intact inside the base64 payload.
    blob = re.findall(r"printf %s '([A-Za-z0-9+/=]+)'", script)[0]
    assert yaml.safe_load(base64.b64decode(blob).decode())["token"] == token


# -- behaviour -------------------------------------------------------------


def test_happy_path_writes_both_files_and_the_markers(harness):
    done = harness.run()
    assert done.returncode == 0, done.stderr
    assert "firstboot complete" in done.stdout

    config = Path(harness.paths.config_path)
    assert yaml.safe_load(config.read_text())["token"] == "s3cr3t"
    assert oct(config.stat().st_mode & 0o777) == "0o600"
    assert oct(Path(harness.paths.config_dir).stat().st_mode & 0o777) == "0o700"
    assert Path(harness.paths.unit_path).read_text() == k3sconf.systemd_unit(meta.ROLE_AGENT)
    assert (Path(harness.paths.state_dir) / "firstboot.started").exists()
    assert (Path(harness.paths.state_dir) / "firstboot.done").exists()


def test_rerunning_after_success_is_a_no_op(harness):
    assert harness.run().returncode == 0
    Path(harness.paths.config_path).write_text("clobbered\n")

    again = harness.run()
    assert again.returncode == 0
    assert "already completed" in again.stdout
    assert Path(harness.paths.config_path).read_text() == "clobbered\n"


def test_a_held_lock_yields_exit_75_not_failure(harness):
    """75 is EX_TEMPFAIL: another run owns the guest, so the caller must back
    off and retry rather than mark the node broken."""
    state_dir = Path(harness.paths.state_dir)
    state_dir.mkdir(parents=True)
    lock = state_dir / "firstboot.lock"
    handle = os.open(lock, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        blocked = harness.run()
    finally:
        os.close(handle)
    assert blocked.returncode == k3sconf.EX_TEMPFAIL
    assert "holds the lock" in blocked.stderr


def test_a_dirty_image_is_rejected_before_anything_runs(harness):
    Path(harness.paths.rancher_dir).mkdir(parents=True)
    dirty = harness.run()
    assert dirty.returncode == 1
    assert "expected a clean golden image" in dirty.stderr
    assert not Path(harness.paths.config_path).exists()


def test_a_packaged_k3s_unit_is_rejected(harness):
    (harness.state / "enabled").write_text("k3s-server\n")
    clash = harness.run()
    assert clash.returncode == 1
    assert "would race" in clash.stderr


def test_missing_k3s_is_reported_clearly(harness):
    (harness.bin / "k3s").unlink()
    absent = harness.run()
    assert absent.returncode == 1
    assert "k3s is not on PATH" in absent.stderr


def test_a_unit_that_never_activates_fails_with_a_reason(harness):
    slow = harness.run(SHIM_ACTIVE_AFTER_START="activating")
    assert slow.returncode == 1
    assert "did not become active" in slow.stderr
    assert not (Path(harness.paths.state_dir) / "firstboot.done").exists()


def test_a_failed_unit_is_detected_immediately(harness):
    broken = harness.run(SHIM_ACTIVE_AFTER_START="failed")
    assert broken.returncode == 1
    assert "failed to start" in broken.stderr


# -- crash recovery --------------------------------------------------------


def test_interruption_after_enable_is_not_mistaken_for_a_dirty_image(harness):
    """The exact window an earlier design got wrong.

    k3s creates its data directory as soon as the unit starts, so a crash
    before the done marker leaves the lock free, no done marker, and the
    directory present.  Only the started marker distinguishes that from an
    image that was never clean.
    """
    state_dir = Path(harness.paths.state_dir)
    state_dir.mkdir(parents=True)
    (state_dir / "firstboot.started").touch()
    Path(harness.paths.rancher_dir).mkdir(parents=True)

    resumed = harness.run()
    assert resumed.returncode == 0, resumed.stderr
    assert (state_dir / "firstboot.done").exists()


@pytest.mark.parametrize(
    "interrupt",
    [
        {"SHIM_FAIL_ENABLE": "1"},
        {"SHIM_FAIL_START": "1"},
        {"SHIM_ACTIVE_AFTER_START": "activating"},
    ],
    ids=["during-enable", "during-start", "never-activates"],
)
def test_every_interrupted_substep_recovers_on_rerun(harness, interrupt):
    failed = harness.run(**interrupt)
    assert failed.returncode != 0
    assert (Path(harness.paths.state_dir) / "firstboot.started").exists()
    assert not (Path(harness.paths.state_dir) / "firstboot.done").exists()

    # Whatever we got half-way through, a clean re-run finishes the job.
    recovered = harness.run()
    assert recovered.returncode == 0, recovered.stderr
    assert (Path(harness.paths.state_dir) / "firstboot.done").exists()
    assert yaml.safe_load(Path(harness.paths.config_path).read_text())["token"] == "s3cr3t"


def test_interruption_between_the_two_file_writes_recovers(harness):
    """Simulate dying after config.yaml landed but before the unit did."""
    script = harness.render()
    truncated = script.split("systemctl daemon-reload")[0]
    partial = subprocess.run(
        ["/bin/sh", "-s", "marker"],
        input=truncated,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "PATH": f"{harness.bin}:{os.environ['PATH']}",
            "SHIM_STATE": str(harness.state),
        },
        timeout=30,
    )
    assert partial.returncode == 0
    assert Path(harness.paths.config_path).exists()
    assert not (Path(harness.paths.state_dir) / "firstboot.done").exists()

    recovered = harness.run()
    assert recovered.returncode == 0, recovered.stderr
    assert (Path(harness.paths.state_dir) / "firstboot.done").exists()


def test_no_temporary_files_are_left_behind(harness):
    assert harness.run().returncode == 0
    leftovers = [path for path in Path(harness.root).rglob("*.tmp")]
    assert leftovers == []
