"""Cluster-side housekeeping.

Scope is deliberately narrow.  Killing a server leaves a stale etcd member
behind, and deleting the Kubernetes ``Node`` does *not* reliably remove it —
etcd member names carry their own random suffix and k3s's removal path is
unreliable.  So this module deletes Node objects and *reports* etcd membership;
it does not claim to heal quorum.  Recovering a cluster that has lost quorum
means ``/reset``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from . import guestexec, meta, names
from .config import Config
from .guestexec import Agent
from .libvirtctl import Node

log = logging.getLogger(__name__)

KUBECONFIG = "/etc/rancher/k3s/k3s.yaml"
ETCD_ENDPOINTS = "https://127.0.0.1:2379"
ETCD_TLS_DIR = "/var/lib/rancher/k3s/server/tls/etcd"

COMMAND_TIMEOUT_S = 30


class NoServerAvailable(Exception):
    """No control plane node can run cluster commands right now."""


@dataclass
class PruneResult:
    server: str | None = None
    deleted: list[str] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)
    etcd_members: str | None = None

    def summary(self) -> str:
        if not self.deleted and not self.failed:
            return "no stale Kubernetes nodes to remove"
        parts = []
        if self.deleted:
            parts.append(f"removed {len(self.deleted)} stale Kubernetes node(s)")
        if self.failed:
            parts.append(f"{len(self.failed)} could not be removed")
        parts.append("etcd members are unchanged — use Reset to rebuild the cluster")
        return "; ".join(parts)


def pick_server(nodes: list[Node]) -> Node:
    """The oldest running, configured control plane node."""
    usable = [
        node for node in nodes if node.is_server and node.state == meta.CONFIGURED and node.running
    ]
    if not usable:
        raise NoServerAvailable(
            "no running, configured control plane node is available to run kubectl"
        )
    return min(usable, key=lambda node: (node.created, node.name))


def _run(agent: Agent, args: list[str], **kwargs) -> guestexec.ExecResult:
    # argv, never a shell: no quoting to get wrong.
    return guestexec.run(agent, "/usr/bin/env", args, timeout_s=COMMAND_TIMEOUT_S, **kwargs)


def _kubectl(agent: Agent, *args: str, **kwargs) -> guestexec.ExecResult:
    return _run(agent, ["k3s", "kubectl", "--kubeconfig", KUBECONFIG, *args], **kwargs)


def list_kubernetes_nodes(agent: Agent, **kwargs) -> list[str]:
    result = _kubectl(agent, "get", "nodes", "-o", "jsonpath={.items[*].metadata.name}", **kwargs)
    if not result.ok:
        raise NoServerAvailable(f"kubectl get nodes failed: {result.stderr.strip()}")
    return result.stdout.split()


def etcd_member_list(agent: Agent, **kwargs) -> str | None:
    result = _run(
        agent,
        [
            "ETCDCTL_API=3",
            "etcdctl",
            f"--cert={ETCD_TLS_DIR}/client.crt",
            f"--key={ETCD_TLS_DIR}/client.key",
            f"--cacert={ETCD_TLS_DIR}/server-ca.crt",
            f"--endpoints={ETCD_ENDPOINTS}",
            "member",
            "list",
        ],
        **kwargs,
    )
    if not result.ok:
        # etcdctl is optional in the image; its absence is not an error here.
        return None
    return result.stdout.strip() or None


def prune_nodes(cfg: Config, nodes: list[Node], agent: Agent, **kwargs) -> PruneResult:
    """Delete Kubernetes Node objects with no managed domain behind them.

    A node is only a candidate if its name is one this deployment could have
    issued *and* no managed domain of that name exists at all.  Power state is
    irrelevant on purpose: a node retained as "configured, shut off" after a
    host reboot is still ours and must survive.
    """
    server = pick_server(nodes)
    result = PruneResult(server=server.name)

    pattern = names.name_pattern(cfg.vm.name_prefix)
    alive = {node.name for node in nodes}

    for candidate in list_kubernetes_nodes(agent, **kwargs):
        if not pattern.match(candidate) or candidate in alive:
            continue
        deletion = _kubectl(agent, "delete", "node", candidate, **kwargs)
        if deletion.ok:
            result.deleted.append(candidate)
        else:
            result.failed.append((candidate, deletion.stderr.strip() or deletion.describe()))

    try:
        result.etcd_members = etcd_member_list(agent, **kwargs)
    except guestexec.ExecTimeout:
        log.debug("etcdctl member list timed out", exc_info=True)
    return result
