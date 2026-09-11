"""Cluster-side policy and housekeeping.

Scope is deliberately narrow.  Killing a server leaves a stale etcd member
behind, and deleting the Kubernetes ``Node`` does *not* reliably remove it —
etcd member names carry their own random suffix and k3s's removal path is
unreliable.  So this module deletes Node objects only; it does not claim to
heal quorum.  Recovering a cluster that has lost quorum means ``/reset``.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

import libvirt
import yaml

from . import guestexec, meta, names
from .config import Config
from .guestexec import Agent
from .libvirtctl import Node, NodeManager, NodeNotFound

KUBECONFIG = "/etc/rancher/k3s/k3s.yaml"

COMMAND_TIMEOUT_S = 30


class NoServerAvailable(Exception):
    """No control plane node can run cluster commands right now."""


class KubeconfigExportError(Exception):
    """Kubeconfig could not be retrieved or parsed."""


def export_kubeconfig(nodes: list[Node], agent_factory: Callable[[str], Agent]) -> str:
    """Export one server's credentials with its host-reachable API address."""
    try:
        server = pick_server([node for node in nodes if node.ip])
    except NoServerAvailable:
        raise KubeconfigExportError("no running, configured control plane node with an IP address")
    try:
        result = guestexec.run(
            agent_factory(server.uuid),
            "/usr/bin/timeout",
            ["10", "/bin/cat", KUBECONFIG],
            timeout_s=COMMAND_TIMEOUT_S,
        )
    except (
        guestexec.AgentUnavailable, guestexec.AgentError, guestexec.ExecTimeout,
        libvirt.libvirtError,
    ):
        raise KubeconfigExportError("could not read kubeconfig through the guest agent") from None
    if not result.ok:
        raise KubeconfigExportError("could not read kubeconfig from the control plane node")
    if not result.stdout.strip() or guestexec.TRUNCATION_NOTE in result.stdout:
        raise KubeconfigExportError("guest returned an empty or truncated kubeconfig")
    try:
        document = yaml.safe_load(result.stdout)
        context = next(
            entry["context"] for entry in document["contexts"]
            if entry["name"] == document["current-context"]
        )
        selected = next(
            entry["cluster"] for entry in document["clusters"]
            if entry["name"] == context["cluster"]
        )
        if not isinstance(selected["server"], str):
            raise ValueError("invalid server address")
        selected["server"] = server.api_url
        return yaml.safe_dump(document, sort_keys=False)
    except (yaml.YAMLError, KeyError, TypeError, StopIteration, ValueError):
        # Parser errors can include source lines containing private keys.
        raise KubeconfigExportError("guest returned an invalid kubeconfig") from None


@dataclass
class Quorum:
    servers: int
    running: int
    needed: int
    warning: str | None = None


def assess_quorum(nodes: list[Node]) -> Quorum:
    """Advisory only.

    Derived from the VMs this app manages, which cannot see etcd members left
    behind by earlier kills, so it can understate the real member count.  The
    template says so; nothing is ever refused on the strength of it.
    """
    servers = [node for node in nodes if node.is_server]
    running = [node for node in servers if node.running]
    needed = len(servers) // 2 + 1 if servers else 0

    warning = None
    if servers and len(running) < needed:
        warning = (
            f"{len(running)} of {len(servers)} control plane nodes are running; "
            f"etcd needs {needed} for quorum. The cluster is down until enough return."
        )
    elif len(servers) > 1 and len(servers) % 2 == 0:
        warning = (
            f"{len(servers)} control plane nodes is an even number; etcd tolerates no "
            "more failures than an odd count one lower. Add or remove one."
        )
    return Quorum(servers=len(servers), running=len(running), needed=needed, warning=warning)


@dataclass
class PruneResult:
    deleted: list[str] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)

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


def _kubectl(agent: Agent, *args: str, **kwargs) -> guestexec.ExecResult:
    # argv, never a shell: no quoting to get wrong.
    return guestexec.run(
        agent,
        "/usr/bin/env",
        ["k3s", "kubectl", "--kubeconfig", KUBECONFIG, "--request-timeout=10s", *args],
        timeout_s=COMMAND_TIMEOUT_S,
        **kwargs,
    )


def list_kubernetes_nodes(agent: Agent, **kwargs) -> list[str]:
    result = _kubectl(agent, "get", "nodes", "-o", "jsonpath={.items[*].metadata.name}", **kwargs)
    if not result.ok:
        raise NoServerAvailable(f"kubectl get nodes failed: {result.stderr.strip()}")
    return result.stdout.split()


def prune_nodes(
    cfg: Config,
    nodes: list[Node],
    agent_factory: Callable[[str], Agent],
    *,
    manager: NodeManager,
    **kwargs,
) -> PruneResult:
    """Delete Kubernetes Node objects with no managed domain behind them.

    A node is only a candidate if its name is one this deployment could have
    issued *and* no managed domain of that name exists at all.  Power state is
    irrelevant on purpose: a node retained as "configured, shut off" after a
    host reboot is still ours and must survive.
    """
    agent = agent_factory(pick_server(nodes).uuid)
    result = PruneResult()

    pattern = names.name_pattern(cfg.vm.name_prefix)
    alive = {node.name for node in nodes}

    for candidate in list_kubernetes_nodes(agent, **kwargs):
        if not pattern.match(candidate) or candidate in alive:
            continue
        # The snapshot can predate a joining VM. Serialize revalidation and
        # deletion with VM creation, but leave the Kubernetes listing unlocked.
        with manager.lock:
            manager.check_compatible()
            try:
                manager.get(candidate)
            except NodeNotFound:
                deletion = _kubectl(agent, "delete", "node", candidate, **kwargs)
            else:
                continue
        if deletion.ok:
            result.deleted.append(candidate)
        else:
            result.failed.append((candidate, deletion.stderr.strip() or deletion.describe()))

    return result
