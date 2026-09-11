"""Racing callers.

Allocation, bootstrap election and deletion all happen under one lock, so two
requests arriving together cannot both win.  The dangerous shapes are two
initial servers each electing itself, and a cancelled worker waking up after
its node was replaced.
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor


from k3s_kvm_demo import meta
from k3s_kvm_demo.libvirtctl import DeployRefused


def run_together(calls, workers: int | None = None):
    """Fire every call at once and collect results or exceptions."""
    barrier = threading.Barrier(len(calls))

    def wrapped(fn):
        def inner():
            barrier.wait(timeout=10)
            try:
                return ("ok", fn())
            except Exception as exc:  # noqa: BLE001 - the point is to collect them
                return ("error", exc)

        return inner

    with ThreadPoolExecutor(max_workers=workers or len(calls)) as pool:
        return [future.result() for future in [pool.submit(wrapped(c)) for c in calls]]


def test_two_initial_servers_yield_exactly_one_bootstrap(manager):
    """Both see an empty cluster; only one may elect itself."""
    outcomes = run_together([lambda: manager.create(meta.ROLE_SERVER)] * 2)

    created = [value for status, value in outcomes if status == "ok"]
    errors = [value for status, value in outcomes if status == "error"]

    assert len(created) == 1, "the second must be refused, not elected"
    assert all(isinstance(exc, DeployRefused) for exc in errors), errors

    nodes = manager.list_nodes()
    assert len([node for node in nodes if node.bootstrap]) == 1


def test_concurrent_deploys_respect_the_node_limit(manager, cfg):
    server = manager.create(meta.ROLE_SERVER)
    manager.update_state(server.uuid, server.generation, meta.CONFIGURED)
    manager.join_ready = lambda node: True

    outcomes = run_together([lambda: manager.create(meta.ROLE_AGENT)] * 8)
    refusals = [value for status, value in outcomes if status == "error"]

    nodes = manager.list_nodes()
    assert len(nodes) <= cfg.vm.max_nodes
    assert len(nodes) == cfg.vm.max_nodes, "the limit should be reached, not undershot"
    assert all(isinstance(exc, DeployRefused) for exc in refusals), refusals


def test_concurrent_deploys_never_collide_on_a_name(manager):
    server = manager.create(meta.ROLE_SERVER)
    manager.update_state(server.uuid, server.generation, meta.CONFIGURED)
    manager.join_ready = lambda node: True

    run_together([lambda: manager.create(meta.ROLE_AGENT)] * 6)

    nodes = manager.list_nodes()
    names = [node.name for node in nodes]
    volumes = [node.volume for node in nodes]
    assert len(set(names)) == len(names)
    assert len(set(volumes)) == len(volumes)


def test_delete_and_create_do_not_interleave(manager):
    server = manager.create(meta.ROLE_SERVER)
    manager.update_state(server.uuid, server.generation, meta.CONFIGURED)
    manager.join_ready = lambda node: True
    victim = manager.create(meta.ROLE_AGENT)

    outcomes = run_together(
        [
            lambda: manager.delete(victim.uuid),
            lambda: manager.create(meta.ROLE_AGENT),
            lambda: manager.reset(),
        ]
    )
    for status, value in outcomes:
        if status == "error":
            assert isinstance(value, DeployRefused), value

    # Whatever the interleaving, nothing is half-built: every surviving node
    # has its overlay and every overlay has its node.
    nodes = manager.list_nodes()
    assert manager.unclaimed_volumes() == set()
    assert len({node.name for node in nodes}) == len(nodes)


def test_reset_racing_creates_leaves_no_orphans(manager):
    server = manager.create(meta.ROLE_SERVER)
    manager.update_state(server.uuid, server.generation, meta.CONFIGURED)
    manager.join_ready = lambda node: True

    run_together([lambda: manager.create(meta.ROLE_AGENT)] * 3 + [manager.reset, manager.reset])
    assert manager.unclaimed_volumes() == set()


def test_a_worker_cannot_write_state_for_a_node_that_was_replaced(manager):
    """The generation guard.

    A job stamped before a kill must not report success afterwards, even
    though the node it names has gone.
    """
    node = manager.create(meta.ROLE_SERVER)

    manager.delete(node.uuid)
    replacement = manager.create(meta.ROLE_SERVER)

    assert not manager.update_state(node.uuid, node.generation, meta.FAILED, error="late report")

    assert manager.get(replacement.name).state == meta.BOOTING
    assert manager.get(replacement.name).error is None


def test_stale_incomplete_snapshot_cannot_delete_launch_ready_node(manager, conn):
    node = manager.create(meta.ROLE_SERVER)
    dom = conn.lookupByUUIDString(node.uuid)
    meta.write(dom, meta.read(dom).advanced(meta.CREATING))
    stale = manager.get(node.name)
    manager.update_state(node.uuid, node.generation, meta.BOOTING)
    assert not manager.delete_if_current(stale.uuid, stale.generation, stale.state)
    assert manager.get(node.name).state == meta.BOOTING


def test_compare_and_set_serializes_observers(manager):
    node = manager.create(meta.ROLE_SERVER)
    outcomes = run_together(
        [
            lambda state=state: manager.update_state(
                node.uuid,
                node.generation,
                state,
                expected_states={meta.BOOTING},
            )
            for state in (meta.CONFIGURED, meta.FAILED)
        ]
    )
    assert sorted(value for status, value in outcomes if status == "ok") == [False, True]


def test_cleanup_and_state_update_share_one_lock(manager, conn, monkeypatch):
    node = manager.create(meta.ROLE_SERVER)
    dom = conn.lookupByUUIDString(node.uuid)
    meta.write(dom, meta.read(dom).advanced(meta.CREATING))
    read_entered = threading.Event()
    release = threading.Event()
    real = manager._delete

    def paused(conn, uuid):
        read_entered.set()
        assert release.wait(5)
        return real(conn, uuid)

    monkeypatch.setattr(manager, "_delete", paused)
    with ThreadPoolExecutor(max_workers=2) as executor:
        deleting = executor.submit(
            manager.delete_if_current,
            node.uuid,
            node.generation,
            meta.CREATING,
        )
        assert read_entered.wait(5)
        updating = executor.submit(manager.update_state, node.uuid, node.generation, meta.BOOTING)
        try:
            assert not updating.done()
        finally:
            release.set()
        assert deleting.result(timeout=5)
        assert not updating.result(timeout=5)
