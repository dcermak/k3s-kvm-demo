"""Launch recovery against libvirt's test driver; only guest polling and faults are substituted."""

from contextlib import contextmanager
from dataclasses import replace
import xml.etree.ElementTree as ET

import libvirt
import pytest

from k3s_kvm_demo import config, meta
from k3s_kvm_demo.app import build_state
from k3s_kvm_demo.kubeconfig_export import KubeconfigExporter
from k3s_kvm_demo.observer import Observer

from .conftest import fake_libvirt_error, unmanaged_domain_xml, volumes


@pytest.fixture
def launch(monkeypatch):
    # The driver has no guests. Keep background QGA/export threads out of these assertions.
    monkeypatch.setattr(Observer, "start", lambda self: None)
    monkeypatch.setattr(KubeconfigExporter, "start", lambda self: None)

    @contextmanager
    def run(cfg):
        state = build_state(cfg)
        try:
            state.startup()
            yield state
        finally:
            state.shutdown()

    return run


def configured_cluster(manager):
    nodes = []
    # Create a worker before the second server so inventory order differs from start order.
    for role in (meta.ROLE_SERVER, meta.ROLE_AGENT, meta.ROLE_SERVER):
        node = manager.create(role)
        manager.update_state(node.uuid, node.generation, meta.CONFIGURED)
        nodes.append(manager.get(node.name))
    return nodes


def durable_snapshot(dom):
    root = ET.fromstring(dom.XMLDesc(libvirt.VIR_DOMAIN_XML_INACTIVE))
    return (
        dom.UUIDString(),
        meta.read(dom),
        [ET.tostring(disk) for disk in root.findall("./devices/disk")],
    )


def test_launch_recovers_a_full_stopped_cluster_without_recreating_it(
    manager, conn, config_values, launch
):
    nodes = configured_cluster(manager)
    interrupted = manager.create(meta.ROLE_AGENT)
    interrupted_dom = conn.lookupByUUIDString(interrupted.uuid)
    interrupted_dom.destroy()
    meta.write(interrupted_dom, replace(meta.read(interrupted_dom), state=meta.CREATING))
    foreign = conn.defineXML(unmanaged_domain_xml("unrelated-stopped-vm"))
    for node in nodes:
        conn.lookupByUUIDString(node.uuid).destroy()
    before = {node.uuid: durable_snapshot(conn.lookupByUUIDString(node.uuid)) for node in nodes}
    retained_volumes = volumes(conn) - {interrupted.volume, interrupted.seed_volume}
    config_values["vm"]["max_nodes"] = len(nodes)

    with launch(config.from_mapping(config_values)) as state:
        assert {node.uuid for node in state.manager.list_nodes()} == set(before)
        for uuid, snapshot in before.items():
            dom = conn.lookupByUUIDString(uuid)
            assert dom.isActive()
            assert durable_snapshot(dom) == snapshot
        assert volumes(conn) == retained_volumes
        assert not foreign.isActive()
        # Starting VMs alone cannot authorize joins before fresh guest observations.
        assert not any(state.observer.join_ready(node) for node in state.manager.list_nodes())


def test_launch_opt_out_preserves_stopped_nodes(manager, conn, config_values, launch):
    node = manager.create(meta.ROLE_SERVER)
    dom = conn.lookupByUUIDString(node.uuid)
    dom.destroy()
    before = durable_snapshot(dom)
    before_volumes = volumes(conn)
    config_values["vm"]["autostart_on_launch"] = False

    with launch(config.from_mapping(config_values)):
        assert not dom.isActive()
        assert durable_snapshot(dom) == before
        assert volumes(conn) == before_volumes


@pytest.mark.parametrize("reply_lost", [False, True], ids=["start-refused", "reply-lost"])
def test_launch_continues_after_a_start_error_without_replaying_or_deleting(
    manager, conn, cfg, launch, monkeypatch, reply_lost
):
    nodes = configured_cluster(manager)
    failed = manager.create(meta.ROLE_AGENT)
    failed_dom = conn.lookupByUUIDString(failed.uuid)
    meta.write(
        failed_dom, replace(meta.read(failed_dom), state=meta.FAILED, error="prepare-failed")
    )
    for node in [*nodes, failed]:
        conn.lookupByUUIDString(node.uuid).destroy()
    before = {
        node.uuid: durable_snapshot(conn.lookupByUUIDString(node.uuid)) for node in [*nodes, failed]
    }
    before_volumes = volumes(conn)
    broken_uuid = nodes[0].uuid
    original_create = libvirt.virDomain.create
    attempts = []

    def start(dom):
        attempts.append(dom.UUIDString())
        if dom.UUIDString() != broken_uuid:
            return original_create(dom)
        if reply_lost:
            original_create(dom)
        raise fake_libvirt_error(libvirt.VIR_ERR_SYSTEM_ERROR, "start connection lost")

    monkeypatch.setattr(libvirt.virDomain, "create", start)
    with launch(cfg):
        # Even when the bootstrap start fails, try the other server before the worker.
        # Each UUID appears once: a lost reply must not replay the start.
        assert attempts == [nodes[0].uuid, nodes[2].uuid, nodes[1].uuid]
        assert bool(conn.lookupByUUIDString(broken_uuid).isActive()) == reply_lost
        assert all(conn.lookupByUUIDString(node.uuid).isActive() for node in nodes[1:])
        assert not failed_dom.isActive()
        assert volumes(conn) == before_volumes
        for uuid, snapshot in before.items():
            assert durable_snapshot(conn.lookupByUUIDString(uuid)) == snapshot
