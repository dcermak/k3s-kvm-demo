"""Discovery, creation, deletion and the reconciliation decision table."""

from __future__ import annotations

import libvirt
import pytest

from k3s_kvm_demo import libvirtctl, meta, names
from k3s_kvm_demo.libvirtctl import Action, DeployRefused

from .conftest import TEST_POOL, TEST_URI, fake_libvirt_error, unmanaged_domain_xml


@pytest.fixture
def conn():
    connection = libvirt.open(TEST_URI)
    yield connection
    connection.close()


def volumes(conn) -> set[str]:
    return {v.name() for v in conn.storagePoolLookupByName(TEST_POOL).listAllVolumes(0)}


def test_create_defines_starts_and_records_the_node(manager, conn):
    node, server_url = manager.create(meta.ROLE_SERVER)

    assert node.role == meta.ROLE_SERVER
    assert node.bootstrap is True
    assert server_url is None, "the first server bootstraps rather than joining"
    assert node.state == meta.BOOTING
    assert node.power == libvirtctl.POWER_RUNNING
    assert names.name_pattern("k3s-node").match(node.name)
    assert node.volume == names.volume_name(node.name)
    assert node.volume in volumes(conn)


def test_the_domain_uuid_is_the_one_in_the_name(manager):
    node, _ = manager.create(meta.ROLE_SERVER)
    assert node.uuid.replace("-", "") == node.name.removeprefix("k3s-node-")


def test_listing_ignores_domains_that_are_not_ours(manager, conn):
    conn.defineXML(unmanaged_domain_xml("someone-elses-vm"))
    manager.create(meta.ROLE_SERVER)

    listed = {node.name for node in manager.list_nodes()}
    assert len(listed) == 1
    assert "someone-elses-vm" not in listed
    assert "test" not in listed, "the test driver's built-in domain is not ours either"


def test_a_running_node_reports_its_lease_address(manager):
    node, _ = manager.create(meta.ROLE_SERVER)
    assert node.ip is not None
    assert node.api_url == f"https://{node.ip}:6443"


def test_delete_removes_both_the_domain_and_its_overlay(manager, conn):
    node, _ = manager.create(meta.ROLE_SERVER)
    manager.delete_by_name(node.name)

    assert manager.list_nodes() == []
    assert node.volume not in volumes(conn)


def test_delete_is_idempotent(manager):
    node, _ = manager.create(meta.ROLE_SERVER)
    manager.delete(node.uuid)
    manager.delete(node.uuid)  # must not raise
    assert manager.list_nodes() == []


def test_a_shut_off_node_deletes_cleanly(manager, conn):
    node, _ = manager.create(meta.ROLE_SERVER)
    conn.lookupByUUIDString(node.uuid).destroy()

    manager.delete(node.uuid)
    assert manager.list_nodes() == []
    assert node.volume not in volumes(conn)


def test_undefine_falls_back_only_for_unsupported_flags(manager, monkeypatch):
    """The test driver rejects UNDEFINE_NVRAM with VIR_ERR_INVALID_ARG, which
    is exactly the case the fallback exists for."""
    node, _ = manager.create(meta.ROLE_SERVER)
    used: list[int] = []
    real = libvirt.virDomain.undefineFlags

    def record(self, flags=0):
        used.append(flags)
        return real(self, flags)

    monkeypatch.setattr(libvirt.virDomain, "undefineFlags", record)
    manager.delete(node.uuid)
    assert used == [libvirtctl.UNDEFINE_FLAGS]
    assert manager.list_nodes() == []


def test_undefine_reraises_any_other_error(manager, monkeypatch):
    node, _ = manager.create(meta.ROLE_SERVER)

    def refuse(self, flags=0):
        raise fake_libvirt_error(libvirt.VIR_ERR_OPERATION_FAILED, "disk busy")

    def should_not_run(self):
        raise AssertionError("plain undefine() must not paper over a real failure")

    monkeypatch.setattr(libvirt.virDomain, "undefineFlags", refuse)
    monkeypatch.setattr(libvirt.virDomain, "undefine", should_not_run)

    with pytest.raises(libvirt.libvirtError):
        manager.delete(node.uuid)


def test_max_nodes_is_enforced(manager, cfg):
    manager.create(meta.ROLE_SERVER)
    for _ in range(cfg.vm.max_nodes - 1):
        _promote(manager)
        manager.create(meta.ROLE_AGENT)

    _promote(manager)
    with pytest.raises(DeployRefused, match="node limit"):
        manager.create(meta.ROLE_AGENT)


def _promote(manager) -> None:
    """Mark every node configured, as a successful firstboot would."""
    for node in manager.list_nodes():
        manager.update_state(node.uuid, node.generation, meta.CONFIGURED)


def test_agents_are_refused_until_a_server_is_configured(manager):
    with pytest.raises(DeployRefused, match="no control plane node"):
        manager.create(meta.ROLE_AGENT)

    manager.create(meta.ROLE_SERVER)
    with pytest.raises(DeployRefused, match="finished provisioning"):
        manager.create(meta.ROLE_AGENT)

    _promote(manager)
    agent, server_url = manager.create(meta.ROLE_AGENT)
    assert agent.bootstrap is False
    assert server_url.endswith(":6443")


def test_a_second_server_joins_the_bootstrap(manager):
    first, _ = manager.create(meta.ROLE_SERVER)
    _promote(manager)

    second, server_url = manager.create(meta.ROLE_SERVER)
    assert second.bootstrap is False
    assert server_url is not None
    assert manager.get(first.name).ip in server_url


def test_bootstrap_election_is_frozen_in_metadata(manager):
    first, _ = manager.create(meta.ROLE_SERVER)
    _promote(manager)
    manager.create(meta.ROLE_SERVER)

    bootstraps = [node.name for node in manager.list_nodes() if node.bootstrap]
    assert bootstraps == [first.name]


def test_reset_removes_everything_managed(manager, conn):
    manager.create(meta.ROLE_SERVER)
    _promote(manager)
    manager.create(meta.ROLE_AGENT)
    conn.defineXML(unmanaged_domain_xml("bystander"))

    assert manager.reset() == 2
    assert manager.list_nodes() == []
    assert {d.name() for d in conn.listAllDomains(0)} == {"test", "bystander"}


# -- durable writes --------------------------------------------------------


def test_update_state_refuses_a_stale_generation(manager):
    node, _ = manager.create(meta.ROLE_SERVER)
    assert manager.update_state(node.uuid, node.generation, meta.CONFIGURING) is True
    assert manager.update_state(node.uuid, node.generation + 5, meta.CONFIGURED) is False
    assert manager.get(node.name).state == meta.CONFIGURING


def test_update_state_refuses_a_node_being_deleted(manager, conn):
    node, _ = manager.create(meta.ROLE_SERVER)
    dom = conn.lookupByUUIDString(node.uuid)
    meta.write(dom, meta.read(dom).advanced(meta.DELETING))

    assert manager.update_state(node.uuid, node.generation, meta.CONFIGURED) is False


def test_update_state_on_a_vanished_node_is_a_no_op(manager):
    node, _ = manager.create(meta.ROLE_SERVER)
    manager.delete(node.uuid)
    assert manager.update_state(node.uuid, node.generation, meta.CONFIGURED) is False


# -- the decision table ----------------------------------------------------


@pytest.mark.parametrize(
    ("state", "power", "expected"),
    [
        (meta.CREATING, libvirtctl.POWER_RUNNING, Action.DELETE),
        (meta.CREATING, libvirtctl.POWER_SHUT_OFF, Action.DELETE),
        (meta.DELETING, libvirtctl.POWER_RUNNING, Action.DELETE),
        (meta.DELETING, libvirtctl.POWER_SHUT_OFF, Action.DELETE),
        (meta.BOOTING, libvirtctl.POWER_RUNNING, Action.AWAIT_AGENT),
        (meta.BOOTING, libvirtctl.POWER_SHUT_OFF, Action.FAIL),
        (meta.BOOTING, "crashed", Action.FAIL),
        (meta.CONFIGURING, libvirtctl.POWER_RUNNING, Action.CONFIGURE),
        (meta.CONFIGURING, libvirtctl.POWER_SHUT_OFF, Action.FAIL),
        (meta.CONFIGURED, libvirtctl.POWER_RUNNING, Action.VERIFY),
        (meta.CONFIGURED, libvirtctl.POWER_SHUT_OFF, Action.NONE),
        (meta.CONFIGURED, "paused", Action.NONE),
        (meta.FAILED, libvirtctl.POWER_RUNNING, Action.NONE),
        (meta.FAILED, libvirtctl.POWER_SHUT_OFF, Action.NONE),
    ],
)
def test_every_state_and_power_combination_is_decided(state, power, expected):
    assert libvirtctl.decide(state, power).action is expected


def test_an_unrecognised_state_is_left_alone():
    decision = libvirtctl.decide("quiescing", libvirtctl.POWER_RUNNING, known_state=False)
    assert decision.action is Action.NONE
    assert "unrecognised" in decision.reason


def test_failures_carry_a_reason():
    for state in (meta.BOOTING, meta.CONFIGURING):
        decision = libvirtctl.decide(state, libvirtctl.POWER_SHUT_OFF)
        assert decision.action is Action.FAIL
        assert decision.reason


def test_short_name_is_readable_but_the_full_name_is_kept(manager):
    node, _ = manager.create(meta.ROLE_SERVER)
    assert node.short_name.startswith("k3s-node-")
    assert len(node.short_name) == len("k3s-node-") + 8
    assert node.name.startswith(node.short_name)
