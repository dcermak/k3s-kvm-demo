"""Kubernetes-side housekeeping.

The scope is deliberately narrow, and these tests hold it there: prune deletes
Node objects and reports etcd membership.  It does not remove etcd members and
must not pretend to, because deleting a Node does not reliably remove one.
"""

from __future__ import annotations

import pytest

from k3s_kvm_demo import cluster, meta

from .conftest import FakeAgent, result


def kubectl_get(path, args):
    return "get" in args and "nodes" in args


def kubectl_delete(path, args):
    return "delete" in args and "node" in args


def etcdctl(path, args):
    return "etcdctl" in args


def deleted_names(agent: FakeAgent) -> list[str]:
    return [call.args[-1] for call in agent.calls if "delete" in call.args]


@pytest.fixture
def server(manager, provisioner, agent):
    from .conftest import is_firstboot, is_probe

    agent.responses = [(is_firstboot, result(0)), (is_probe, result(0))]
    node, url = manager.create(meta.ROLE_SERVER)
    provisioner.submit(node, url)
    return manager.get(node.name)


ALIEN = "some-other-cluster-node"
GONE = "k3s-node-" + "9" * 32


def test_only_stale_demo_owned_nodes_are_deleted(cfg, manager, server):
    agent = FakeAgent(
        responses=[
            (kubectl_get, result(0, stdout=f"{server.name} {GONE} {ALIEN}\n")),
            (kubectl_delete, result(0)),
            (etcdctl, result(0, stdout="1234, started, node-a")),
        ]
    )
    outcome = cluster.prune_nodes(cfg, manager.list_nodes(), agent)

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
        cluster.prune_nodes(cfg, nodes, agent)
    assert deleted_names(agent) == []


def test_a_shut_off_node_is_excluded_from_the_candidate_set(cfg, manager, server, conn_factory):
    """Same rule, exercised with a second server still running so prune can
    actually execute."""
    other, _ = manager.create(meta.ROLE_SERVER)
    manager.update_state(other.uuid, other.generation, meta.CONFIGURED)
    conn_factory().lookupByUUIDString(other.uuid).destroy()

    agent = FakeAgent(
        responses=[
            (kubectl_get, result(0, stdout=f"{server.name} {other.name} {GONE}\n")),
            (kubectl_delete, result(0)),
        ]
    )
    outcome = cluster.prune_nodes(cfg, manager.list_nodes(), agent)
    assert outcome.deleted == [GONE]
    assert other.name not in deleted_names(agent)


def test_no_etcd_member_is_ever_removed(cfg, manager, server):
    agent = FakeAgent(
        responses=[
            (kubectl_get, result(0, stdout=f"{GONE}\n")),
            (kubectl_delete, result(0)),
            (etcdctl, result(0, stdout="1234, started, node-a")),
        ]
    )
    outcome = cluster.prune_nodes(cfg, manager.list_nodes(), agent)

    etcd_calls = [call.args for call in agent.calls if "etcdctl" in call.args]
    assert etcd_calls, "membership is reported"
    assert all("remove" not in args for args in etcd_calls), "but never modified"
    assert outcome.etcd_members == "1234, started, node-a"
    assert "etcd members are unchanged" in outcome.summary()


def test_a_missing_etcdctl_is_not_an_error(cfg, manager, server):
    agent = FakeAgent(
        responses=[
            (kubectl_get, result(0, stdout=f"{GONE}\n")),
            (kubectl_delete, result(0)),
            (etcdctl, result(127, stderr="etcdctl: not found")),
        ]
    )
    outcome = cluster.prune_nodes(cfg, manager.list_nodes(), agent)
    assert outcome.deleted == [GONE]
    assert outcome.etcd_members is None


def test_a_failed_deletion_is_reported_rather_than_claimed(cfg, manager, server):
    agent = FakeAgent(
        responses=[
            (kubectl_get, result(0, stdout=f"{GONE}\n")),
            (kubectl_delete, result(1, stderr="Error from server: forbidden")),
            (etcdctl, result(0, stdout="")),
        ]
    )
    outcome = cluster.prune_nodes(cfg, manager.list_nodes(), agent)

    assert outcome.deleted == []
    assert outcome.failed == [(GONE, "Error from server: forbidden")]
    assert "could not be removed" in outcome.summary()


def test_a_broken_kubectl_surfaces_clearly(cfg, manager, server):
    agent = FakeAgent(responses=[(kubectl_get, result(1, stderr="connection refused"))])
    with pytest.raises(cluster.NoServerAvailable, match="connection refused"):
        cluster.prune_nodes(cfg, manager.list_nodes(), agent)


def test_nothing_to_do_says_so(cfg, manager, server):
    agent = FakeAgent(
        responses=[
            (kubectl_get, result(0, stdout=f"{server.name}\n")),
            (etcdctl, result(0, stdout="")),
        ]
    )
    outcome = cluster.prune_nodes(cfg, manager.list_nodes(), agent)
    assert outcome.deleted == []
    assert "no stale" in outcome.summary()


def test_commands_are_argv_never_a_shell(cfg, manager, server):
    agent = FakeAgent(
        responses=[
            (kubectl_get, result(0, stdout=f"{GONE}\n")),
            (kubectl_delete, result(0)),
            (etcdctl, result(0)),
        ]
    )
    cluster.prune_nodes(cfg, manager.list_nodes(), agent)

    for call in agent.calls:
        assert call.path == "/usr/bin/env"
        assert "-c" not in call.args, "no shell means no quoting to get wrong"


def test_pick_server_requires_a_running_configured_control_plane(manager, server, conn_factory):
    assert cluster.pick_server(manager.list_nodes()).name == server.name

    conn_factory().lookupByUUIDString(server.uuid).destroy()
    with pytest.raises(cluster.NoServerAvailable):
        cluster.pick_server(manager.list_nodes())
