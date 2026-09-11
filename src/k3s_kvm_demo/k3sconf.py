"""Build k3s configuration as JSON, which k3s accepts as YAML."""

from __future__ import annotations

import json
from collections.abc import Sequence

from . import meta

UNIT_NAME = "k3s-node.service"
CONFIG_PATH = "/etc/rancher/k3s/config.yaml"


def config_yaml(
    *,
    node_name: str,
    role: str,
    token: str,
    is_bootstrap: bool,
    server_url: str | None,
    tls_san: Sequence[str] = (),
) -> str:
    """Render configuration without interpreting tokens or SANs as shell or YAML."""
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
