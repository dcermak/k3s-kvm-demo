"""Kubernetes-side housekeeping.

The scope is deliberately narrow, and these tests hold it there: prune deletes
Node objects and nothing else.  It does not touch etcd membership and must not
pretend to, because deleting a Node does not reliably remove a member.
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

import libvirt
import pytest

from k3s_kvm_demo import cluster, meta

from .conftest import FakeAgent, result, unmanaged_domain_xml


def kubectl_get(path, args):
    return "get" in args and "nodes" in args


def kubectl_delete(path, args):
    return "delete" in args and "node" in args


def deleted_names(agent: FakeAgent) -> list[str]:
    return [call.args[-1] for call in agent.calls if "delete" in call.args]


@pytest.fixture
def server(deployed_node):
    return deployed_node


ALIEN = "some-other-cluster-node"
GONE = "k3s-node-" + "9" * 32


def test_only_stale_demo_owned_nodes_are_deleted(cfg, manager, server):
    agent = FakeAgent(
        responses=[
            (kubectl_get, result(0, stdout=f"{server.name} {GONE} {ALIEN}\n")),
            (kubectl_delete, result(0)),
        ]
    )
    outcome = cluster.prune_nodes(cfg, manager.list_nodes(), lambda _uuid: agent, manager=manager)

    assert outcome.deleted == [GONE]
    assert deleted_names(agent) == [GONE]
    assert ALIEN not in deleted_names(agent), "another cluster's node is not ours to delete"
    assert server.name not in deleted_names(agent)


def test_a_shut_off_managed_node_is_never_pruned(cfg, manager, server, conn_factory):
    """A node retained as 'configured, shut off' after a host reboot is still
    ours; only power state changed."""
    conn = conn_factory()
    conn.lookupByUUIDString(server.uuid).destroy()

    nodes = manager.list_nodes()
    assert nodes[0].power != "running"

    agent = FakeAgent(
        responses=[
            (kubectl_get, result(0, stdout=f"{server.name}\n")),
            (kubectl_delete, result(0)),
        ]
    )
    with pytest.raises(cluster.NoServerAvailable):
        # ...and with no running server there is nothing to run kubectl on.
        cluster.prune_nodes(cfg, nodes, lambda _uuid: agent, manager=manager)
    assert deleted_names(agent) == []


def test_a_shut_off_node_is_excluded_from_the_candidate_set(cfg, manager, server, conn_factory):
    """Same rule, exercised with a second server still running so prune can
    actually execute."""
    other = manager.create(meta.ROLE_SERVER)
    manager.update_state(other.uuid, other.generation, meta.CONFIGURED)
    conn_factory().lookupByUUIDString(other.uuid).destroy()

    agent = FakeAgent(
        responses=[
            (kubectl_get, result(0, stdout=f"{server.name} {other.name} {GONE}\n")),
            (kubectl_delete, result(0)),
        ]
    )
    outcome = cluster.prune_nodes(cfg, manager.list_nodes(), lambda _uuid: agent, manager=manager)
    assert outcome.deleted == [GONE]
    assert other.name not in deleted_names(agent)


def test_a_failed_deletion_is_reported_rather_than_claimed(cfg, manager, server):
    agent = FakeAgent(
        responses=[
            (kubectl_get, result(0, stdout=f"{GONE}\n")),
            (kubectl_delete, result(1, stderr="Error from server: forbidden")),
        ]
    )
    outcome = cluster.prune_nodes(cfg, manager.list_nodes(), lambda _uuid: agent, manager=manager)

    assert outcome.deleted == []
    assert outcome.failed == [(GONE, "Error from server: forbidden")]
    assert "could not be removed" in outcome.summary()


def test_a_broken_kubectl_surfaces_clearly(cfg, manager, server):
    agent = FakeAgent(responses=[(kubectl_get, result(1, stderr="connection refused"))])
    with pytest.raises(cluster.NoServerAvailable, match="connection refused"):
        cluster.prune_nodes(cfg, manager.list_nodes(), lambda _uuid: agent, manager=manager)


def test_nothing_to_do_says_so(cfg, manager, server):
    agent = FakeAgent(
        responses=[
            (kubectl_get, result(0, stdout=f"{server.name}\n")),
        ]
    )
    outcome = cluster.prune_nodes(cfg, manager.list_nodes(), lambda _uuid: agent, manager=manager)
    assert outcome.deleted == []
    assert "no stale" in outcome.summary()


def test_commands_are_argv_never_a_shell(cfg, manager, server):
    agent = FakeAgent(
        responses=[
            (kubectl_get, result(0, stdout=f"{GONE}\n")),
            (kubectl_delete, result(0)),
        ]
    )
    cluster.prune_nodes(cfg, manager.list_nodes(), lambda _uuid: agent, manager=manager)

    for call in agent.calls:
        assert call.path == "/usr/bin/env"
        assert "-c" not in call.args, "no shell means no quoting to get wrong"
        assert call.input_data is None
        assert "--request-timeout=10s" in call.args


def test_prune_never_changes_etcd_membership(cfg, manager, server):
    agent = FakeAgent(responses=[(kubectl_get, result(stdout=GONE))])
    outcome = cluster.prune_nodes(cfg, manager.list_nodes(), lambda _uuid: agent, manager=manager)
    assert outcome.deleted == [GONE]
    assert "etcd members are unchanged" in outcome.summary()
    assert len(agent.calls) == 2
    assert agent.calls[1].args[-3:] == ["delete", "node", GONE]


@pytest.mark.parametrize("candidate", ["k3s-node-short", GONE + ";id", GONE + "-extra"])
def test_lookalike_names_are_not_pruned(cfg, manager, server, candidate):
    agent = FakeAgent(responses=[(kubectl_get, result(stdout=candidate))])
    outcome = cluster.prune_nodes(cfg, manager.list_nodes(), lambda _uuid: agent, manager=manager)
    assert outcome.deleted == []
    assert deleted_names(agent) == []


@pytest.mark.parametrize("shut_off", [False, True])
def test_node_created_during_kubectl_get_is_not_pruned(cfg, manager, server, conn, shut_off):
    nodes = manager.list_nodes()
    listing = threading.Barrier(2, timeout=5)
    created = threading.Barrier(2, timeout=5)

    def get_response():
        listing.wait()
        created.wait()
        return result(stdout=f"{joining.name} {GONE}")

    agent = FakeAgent(responses=[(kubectl_get, get_response)])
    with ThreadPoolExecutor(max_workers=1) as executor:
        pruning = executor.submit(
            cluster.prune_nodes, cfg, nodes, lambda _uuid: agent, manager=manager
        )
        listing.wait()
        joining = manager.create(meta.ROLE_AGENT)
        if shut_off:
            conn.lookupByUUIDString(joining.uuid).destroy()
        created.wait()
        outcome = pruning.result(timeout=5)

    assert joining.name not in {node.name for node in nodes}
    assert manager.get(joining.name).uuid == joining.uuid
    assert outcome.deleted == [GONE]
    assert deleted_names(agent) == [GONE]


def test_revalidation_and_guest_deletion_hold_manager_lock(cfg, manager, server, monkeypatch):
    get = manager.get
    checked = []

    def locked_get(name):
        assert manager.lock._is_owned()
        checked.append(name)
        return get(name)

    def delete_response():
        assert manager.lock._is_owned()
        assert checked == [GONE]
        return result()

    monkeypatch.setattr(manager, "get", locked_get)
    agent = FakeAgent(
        responses=[(kubectl_get, result(stdout=GONE)), (kubectl_delete, delete_response)]
    )
    outcome = cluster.prune_nodes(cfg, manager.list_nodes(), lambda _uuid: agent, manager=manager)
    assert outcome.deleted == [GONE]


@pytest.mark.parametrize("namespace", [meta.LEGACY_NS, meta.NS.rsplit("/", 1)[0] + "/99"])
@pytest.mark.parametrize("after_first_delete", [False, True])
def test_incompatible_metadata_blocks_each_deletion(
    cfg, manager, server, conn, namespace, after_first_delete
):
    nodes = manager.list_nodes()
    other_gone = "k3s-node-" + "8" * 32

    def add_incompatible_domain():
        dom = conn.defineXML(unmanaged_domain_xml("incompatible"))
        dom.setMetadata(
            libvirt.VIR_DOMAIN_METADATA_ELEMENT,
            "<node><role>server</role></node>",
            meta.KEY,
            namespace,
            libvirt.VIR_DOMAIN_AFFECT_CONFIG,
        )
        return result(stdout=f"{GONE} {other_gone}")

    agent = FakeAgent(
        responses=[
            (
                kubectl_get,
                result(stdout=f"{GONE} {other_gone}")
                if after_first_delete
                else add_incompatible_domain,
            ),
            (kubectl_delete, add_incompatible_domain),
        ]
    )
    with pytest.raises(meta.CompatibilityError):
        cluster.prune_nodes(cfg, nodes, lambda _uuid: agent, manager=manager)
    assert deleted_names(agent) == ([GONE] if after_first_delete else [])


def test_pick_server_requires_a_running_configured_control_plane(manager, server, conn_factory):
    assert cluster.pick_server(manager.list_nodes()).name == server.name

    conn_factory().lookupByUUIDString(server.uuid).destroy()
    with pytest.raises(cluster.NoServerAvailable):
        cluster.pick_server(manager.list_nodes())
