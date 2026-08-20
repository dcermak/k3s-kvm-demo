"""Recovery after a restart.

Every combination of durable state and observed power must reach a defined
outcome.  Two of them carry most of the weight: a node that finished
provisioning and is merely powered off must survive untouched — a host reboot
must not silently destroy the demo — and a node whose provisioning was
interrupted must never be left stuck.
"""

from __future__ import annotations

import dataclasses

import libvirt
import pytest

from k3s_kvm_demo import libvirtctl, meta

from .conftest import TEST_POOL, TEST_URI, is_firstboot, is_probe, result


@pytest.fixture
def conn():
    connection = libvirt.open(TEST_URI)
    yield connection
    connection.close()


def force(conn, node, state: str, *, error: str | None = None, running: bool = True):
    """Put a node into a given durable state and power state."""
    dom = conn.lookupByUUIDString(node.uuid)
    if not running and dom.isActive():
        dom.destroy()
    meta.write(dom, dataclasses.replace(meta.read(dom), state=state, error=error))
    return dom


def volumes(conn) -> set[str]:
    return {
        v.name()
        for v in conn.storagePoolLookupByName(TEST_POOL).listAllVolumes(0)
        if v.name().startswith("k3s-node-")
    }


@pytest.fixture
def node(manager, provisioner, agent):
    agent.responses = [(is_firstboot, result(0)), (is_probe, result(0))]
    created, url = manager.create(meta.ROLE_SERVER)
    provisioner.submit(created, url)
    return manager.get(created.name)


# -- interrupted work is completed ----------------------------------------


@pytest.mark.parametrize("state", [meta.CREATING, meta.DELETING])
@pytest.mark.parametrize("running", [True, False])
def test_interrupted_operations_are_finished_off(manager, provisioner, conn, node, state, running):
    force(conn, node, state, running=running)
    provisioner.reconcile()

    assert manager.list_nodes() == []
    assert volumes(conn) == set()


# -- a stopped VM mid-provisioning fails, it is not deleted ---------------


@pytest.mark.parametrize(
    ("state", "fragment"),
    [
        (meta.BOOTING, "before first boot"),
        (meta.CONFIGURING, "during configuration"),
    ],
)
def test_a_vm_that_stopped_mid_provisioning_is_failed_with_a_reason(
    manager, provisioner, conn, node, state, fragment
):
    force(conn, node, state, running=False)
    provisioner.reconcile()

    survivor = manager.get(node.name)
    assert survivor.state == meta.FAILED
    assert fragment in survivor.error
    assert survivor.power == libvirtctl.POWER_SHUT_OFF
    assert node.volume in volumes(conn), "a failed node is kept for inspection"


# -- resumable work is resumed --------------------------------------------


@pytest.mark.parametrize("state", [meta.BOOTING, meta.CONFIGURING])
def test_provisioning_resumes_from_either_phase(manager, provisioner, conn, node, agent, state):
    force(conn, node, state)
    agent.calls.clear()
    provisioner.reconcile()

    assert manager.get(node.name).state == meta.CONFIGURED
    assert any(call.args[:1] == ["-s"] for call in agent.calls)


def test_resuming_is_skipped_while_no_join_target_exists(manager, provisioner, conn, node, agent):
    """An agent mid-provisioning cannot be resumed until a server is
    configured; it waits rather than failing."""
    server = node
    agent.responses = [(is_firstboot, result(0)), (is_probe, result(0))]
    worker, url = manager.create(meta.ROLE_AGENT)
    provisioner.submit(worker, url)

    force(conn, worker, meta.CONFIGURING)
    force(conn, server, meta.BOOTING)  # the only server is no longer usable

    provisioner.reconcile()
    refreshed = manager.get(worker.name)
    assert refreshed.state == meta.CONFIGURING, "left alone, not failed"


# -- completed work is never rewritten ------------------------------------


def test_a_configured_node_that_is_shut_off_survives_a_restart(manager, provisioner, conn, node):
    """The host-reboot case.  An earlier design deleted every non-running
    node, which would have wiped the demo on a power cycle."""
    force(conn, node, meta.CONFIGURED, running=False)
    provisioner.reconcile()

    survivor = manager.get(node.name)
    assert survivor.state == meta.CONFIGURED
    assert survivor.power == libvirtctl.POWER_SHUT_OFF
    assert survivor.error is None
    assert node.volume in volumes(conn)


def test_a_failed_node_keeps_its_reason_across_a_power_cycle(manager, provisioner, conn, node):
    force(conn, node, meta.FAILED, error="k3s is not on PATH", running=False)
    provisioner.reconcile()

    survivor = manager.get(node.name)
    assert survivor.state == meta.FAILED
    assert survivor.error == "k3s is not on PATH"


def test_a_configured_running_node_is_verified_not_re_provisioned(
    manager, provisioner, conn, node, agent
):
    force(conn, node, meta.CONFIGURED)
    agent.calls.clear()
    agent.responses = [(is_probe, result(0))]
    provisioner.reconcile()

    assert manager.get(node.name).state == meta.CONFIGURED
    assert all(call.args[:1] == ["-c"] for call in agent.calls), "probe only"


def test_an_unrecognised_state_is_left_completely_alone(manager, provisioner, conn, node, agent):
    force(conn, node, "quiescing")
    agent.calls.clear()
    provisioner.reconcile()

    survivor = manager.get(node.name)
    assert survivor.state == "quiescing"
    assert survivor.known_state is False
    assert agent.calls == []
    assert node.volume in volumes(conn)


# -- repeated passes converge ---------------------------------------------


def test_repeated_reconciliation_is_stable(manager, provisioner, conn, node, agent):
    force(conn, node, meta.CONFIGURED)
    agent.responses = [(is_probe, result(0))]
    for _ in range(3):
        provisioner.reconcile()

    assert manager.get(node.name).state == meta.CONFIGURED
    assert provisioner.in_flight == 0


def test_one_broken_node_does_not_stop_the_pass(manager, provisioner, conn, node, agent):
    """A pass that gave up on the first bad node would strand every node
    behind it."""
    worker, url = manager.create(meta.ROLE_AGENT)
    provisioner.submit(worker, url)

    force(conn, node, meta.CONFIGURING)
    force(conn, worker, meta.CREATING)  # interrupted; should be cleaned up

    seen: list[str] = []
    original = provisioner._apply

    def explode_on_the_server(candidate, nodes, decision):
        seen.append(candidate.name)
        if candidate.uuid == node.uuid:
            raise RuntimeError("something went wrong with this one")
        return original(candidate, nodes, decision)

    provisioner._apply = explode_on_the_server
    agent.responses = [(is_firstboot, result(0)), (is_probe, result(0))]
    provisioner.reconcile()

    assert set(seen) == {node.name, worker.name}, "both nodes were visited"
    remaining = {n.name for n in manager.list_nodes()}
    assert worker.name not in remaining, "the interrupted node was still cleaned up"
    assert node.name in remaining


def test_orphan_maintenance_runs_after_each_pass(manager, cfg, agent):
    from k3s_kvm_demo.provision import Provisioner

    ran = {"n": 0}

    def maintenance():
        ran["n"] += 1

    from .conftest import ImmediatePool, _FakeClock

    prov = Provisioner(
        manager,
        cfg,
        pool=ImmediatePool(),
        agent_factory=lambda uuid, timeout: agent,
        maintenance=maintenance,
        sleep=lambda _s: None,
        now=_FakeClock(),
        wait=lambda _e, _t: False,
    )
    prov.reconcile()
    prov.reconcile()
    assert ran["n"] == 2
