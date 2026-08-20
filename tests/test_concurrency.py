"""Racing callers.

Allocation, bootstrap election and deletion all happen under one lock, so two
requests arriving together cannot both win.  The dangerous shapes are two
initial servers each electing itself, and a cancelled worker waking up after
its node was replaced.
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor


from k3s_kvm_demo import meta, provision
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
    server, _ = manager.create(meta.ROLE_SERVER)
    manager.update_state(server.uuid, server.generation, meta.CONFIGURED)

    outcomes = run_together([lambda: manager.create(meta.ROLE_AGENT)] * 8)
    refusals = [value for status, value in outcomes if status == "error"]

    nodes = manager.list_nodes()
    assert len(nodes) <= cfg.vm.max_nodes
    assert len(nodes) == cfg.vm.max_nodes, "the limit should be reached, not undershot"
    assert all(isinstance(exc, DeployRefused) for exc in refusals), refusals


def test_concurrent_deploys_never_collide_on_a_name(manager):
    server, _ = manager.create(meta.ROLE_SERVER)
    manager.update_state(server.uuid, server.generation, meta.CONFIGURED)

    run_together([lambda: manager.create(meta.ROLE_AGENT)] * 6)

    nodes = manager.list_nodes()
    names = [node.name for node in nodes]
    volumes = [node.volume for node in nodes]
    assert len(set(names)) == len(names)
    assert len(set(volumes)) == len(volumes)


def test_delete_and_create_do_not_interleave(manager):
    server, _ = manager.create(meta.ROLE_SERVER)
    manager.update_state(server.uuid, server.generation, meta.CONFIGURED)
    victim, _ = manager.create(meta.ROLE_AGENT)

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
    server, _ = manager.create(meta.ROLE_SERVER)
    manager.update_state(server.uuid, server.generation, meta.CONFIGURED)

    run_together([lambda: manager.create(meta.ROLE_AGENT)] * 3 + [manager.reset, manager.reset])
    assert manager.unclaimed_volumes() == set()


def test_a_worker_cannot_write_state_for_a_node_that_was_replaced(manager, cfg, provisioner):
    """The generation guard.

    A job stamped before a kill must not report success afterwards, even
    though the node it names has gone.
    """
    node, _ = manager.create(meta.ROLE_SERVER)
    stale = provision.Job(uuid=node.uuid, generation=node.generation)

    manager.delete(node.uuid)
    replacement, _ = manager.create(meta.ROLE_SERVER)

    provisioner._fail(stale, "late report from a killed node")

    assert manager.get(replacement.name).state == meta.BOOTING
    assert manager.get(replacement.name).error is None


def test_a_job_is_claimed_only_once(provisioner):
    first = provisioner._claim("uuid-1", 1)
    assert first is not None
    assert provisioner._claim("uuid-1", 1) is None
    provisioner._release("uuid-1")
    assert provisioner._claim("uuid-1", 1) is not None


def test_claiming_is_atomic_across_threads(provisioner):
    outcomes = run_together([lambda: provisioner._claim("uuid-2", 1)] * 8)
    claims = [value for status, value in outcomes if status == "ok" and value is not None]
    assert len(claims) == 1
