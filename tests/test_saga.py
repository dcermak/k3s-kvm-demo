"""Create/delete transactionality.

The interesting failure is a call that succeeds on the daemon and then raises
on the way back to us.  Compensation therefore cannot assume the step did not
happen; it has to look.  In particular, a client-side error after ``create()``
means a *running* domain, and deleting its disk without destroying it first
would corrupt a live VM.
"""

from __future__ import annotations

import libvirt
import pytest
import xml.etree.ElementTree as ET

from k3s_kvm_demo import meta, names, pool as poolmod, seed

from .conftest import AFTER, BEFORE, TEST_POOL, add_volume, fake_libvirt_error, volumes


def domains(conn) -> set[str]:
    return {d.name() for d in conn.listAllDomains(0) if d.name().startswith("k3s-node-")}


@pytest.mark.parametrize("stage", ["define", "volume"])
@pytest.mark.parametrize("when", [BEFORE, AFTER])
def test_a_failed_create_leaves_nothing_behind(faulty_manager, faults, conn, stage, when):
    setattr(faults, stage, when)

    with pytest.raises(libvirt.libvirtError):
        faulty_manager.create(meta.ROLE_SERVER)

    # Stop injecting so the assertions see the real state.
    setattr(faults, stage, None)
    assert domains(conn) == set(), f"orphan domain after {stage} failed {when}"
    assert volumes(conn, "k3s-node-") == set(), f"orphan volume after {stage} failed {when}"
    assert faulty_manager.list_nodes() == []


@pytest.mark.parametrize("when", [BEFORE, AFTER])
def test_any_start_error_retains_launch_ready_domain_and_both_volumes(
    faulty_manager,
    faults,
    conn,
    when,
):
    faults.start = when
    with pytest.raises(libvirt.libvirtError):
        faulty_manager.create(meta.ROLE_SERVER)
    faults.start = None

    [node] = faulty_manager.list_nodes()
    assert node.state == meta.BOOTING
    assert bool(conn.lookupByUUIDString(node.uuid).isActive()) == (when == AFTER)
    assert volumes(conn, "k3s-node-") == {node.volume, node.seed_volume}


def test_a_failed_volume_delete_keeps_the_domain_and_its_ownership(faulty_manager, faults, conn):
    """Ownership must outlive the volume: if the disk cannot be removed, the
    domain stays defined in 'deleting' so reconciliation can try again."""
    node = faulty_manager.create(meta.ROLE_SERVER)

    faults.volume_delete = BEFORE
    with pytest.raises(libvirt.libvirtError):
        faulty_manager.delete(node.uuid)
    faults.volume_delete = None

    assert node.name in domains(conn)
    surviving = faulty_manager.get(node.name)
    assert surviving.state == meta.DELETING
    assert surviving.volume == node.volume, "the ownership record is still there"
    assert node.volume in volumes(conn, "k3s-node-")

    # A retry finishes the job.
    faulty_manager.delete(node.uuid)
    assert domains(conn) == set()
    assert volumes(conn, "k3s-node-") == set()


def test_delete_bumps_the_generation_before_touching_anything(faulty_manager, faults, conn):
    node = faulty_manager.create(meta.ROLE_SERVER)
    assert node.generation == 1

    faults.volume_delete = BEFORE
    with pytest.raises(libvirt.libvirtError):
        faulty_manager.delete(node.uuid)
    faults.volume_delete = None

    stale = faulty_manager.get(node.name)
    assert stale.generation == 2
    # A worker still holding generation 1 can no longer write.
    assert faulty_manager.update_state(node.uuid, 1, meta.CONFIGURED) is False


def test_deleting_twice_is_a_no_op(manager, conn):
    node = manager.create(meta.ROLE_SERVER)
    manager.delete(node.uuid)
    manager.delete(node.uuid)
    assert domains(conn) == set()
    assert volumes(conn, "k3s-node-") == set()


def test_a_half_built_node_is_finished_off_by_a_later_delete(faulty_manager, faults, conn):
    """Simulate the process dying right after defineXML: the domain exists in
    'creating' with no volume, and deletion must still tidy it away."""
    faults.volume = BEFORE
    with pytest.raises(libvirt.libvirtError):
        faulty_manager.create(meta.ROLE_SERVER)
    faults.volume = None
    assert domains(conn) == set()

    # Rebuild that state deliberately, as a crash would leave it.
    node = faulty_manager.create(meta.ROLE_SERVER)
    conn.storagePoolLookupByName(TEST_POOL).storageVolLookupByName(node.volume).delete(0)

    faulty_manager.delete(node.uuid)
    assert domains(conn) == set()


def test_seed_build_fails_before_domain_definition(manager, monkeypatch, conn):
    def fail(*args):
        raise RuntimeError("seed build failed")

    monkeypatch.setattr(seed, "build_seed_iso", fail)
    with pytest.raises(RuntimeError, match="seed build"):
        manager.create(meta.ROLE_SERVER)
    assert domains(conn) == set()
    assert volumes(conn, "k3s-node-") == set()


def test_upload_failure_cleans_both_claims(manager, monkeypatch, conn):
    def fail(*args):
        raise RuntimeError("upload failed")

    monkeypatch.setattr(seed, "upload_seed", fail)
    with pytest.raises(RuntimeError, match="upload"):
        manager.create(meta.ROLE_SERVER)
    assert domains(conn) == set()
    assert volumes(conn, "k3s-node-") == set()


@pytest.mark.parametrize("committed", [False, True])
def test_ambiguous_booting_commit_only_cleans_confirmed_creating(
    manager,
    monkeypatch,
    conn,
    committed,
):
    real = meta.write

    def ambiguous(dom, record):
        if record.state == meta.BOOTING:
            if committed:
                real(dom, record)
            raise RuntimeError("ambiguous commit")
        return real(dom, record)

    monkeypatch.setattr(meta, "write", ambiguous)
    with pytest.raises(RuntimeError, match="ambiguous"):
        manager.create(meta.ROLE_SERVER)
    if committed:
        [node] = manager.list_nodes()
        assert node.state == meta.BOOTING
        assert not conn.lookupByUUIDString(node.uuid).isActive()
        assert volumes(conn, "k3s-node-") == {node.volume, node.seed_volume}
    else:
        assert domains(conn) == set()
        assert volumes(conn, "k3s-node-") == set()


def test_unreadable_metadata_after_commit_failure_preserves_claims(manager, monkeypatch, conn):
    real_read = meta.read
    failed = False

    def fail_write(dom, record):
        nonlocal failed
        if record.state == meta.BOOTING:
            failed = True
            raise RuntimeError("commit failed")

    def fail_read(dom):
        if failed:
            raise RuntimeError("metadata unreadable")
        return real_read(dom)

    monkeypatch.setattr(meta, "write", fail_write)
    monkeypatch.setattr(meta, "read", fail_read)
    with pytest.raises(RuntimeError, match="commit failed"):
        manager.create(meta.ROLE_SERVER)
    assert len(domains(conn)) == 1
    assert len(volumes(conn, "k3s-node-")) == 2


def test_booting_commit_precedes_the_only_start(manager, monkeypatch):
    real = libvirt.virDomain.create
    calls = []

    def start(dom):
        calls.append(meta.read(dom).state)
        return real(dom)

    monkeypatch.setattr(libvirt.virDomain, "create", start)
    manager.create(meta.ROLE_SERVER)
    assert calls == [meta.BOOTING]


def test_delete_intent_failure_never_destroys_or_deletes(manager, monkeypatch, conn):
    node = manager.create(meta.ROLE_SERVER)

    def fail(*args):
        raise RuntimeError("intent failed")

    monkeypatch.setattr(meta, "write", fail)
    with pytest.raises(RuntimeError, match="intent"):
        manager.delete(node.uuid)
    assert conn.lookupByUUIDString(node.uuid).isActive()
    assert volumes(conn, "k3s-node-") == {node.volume, node.seed_volume}


def test_destroy_must_actually_stop_domain(manager, monkeypatch, conn):
    node = manager.create(meta.ROLE_SERVER)
    monkeypatch.setattr(libvirt.virDomain, "destroy", lambda dom: 0)
    with pytest.raises(poolmod.PoolError, match="still active"):
        manager.delete(node.uuid)
    assert volumes(conn, "k3s-node-") == {node.volume, node.seed_volume}


def test_partial_two_volume_delete_keeps_claims_until_retry(manager, monkeypatch, conn):
    node = manager.create(meta.ROLE_SERVER)
    real = libvirt.virStorageVol.delete

    def fail_seed(volume, flags=0):
        if volume.name() == node.seed_volume:
            raise fake_libvirt_error(libvirt.VIR_ERR_INTERNAL_ERROR)
        return real(volume, flags)

    with monkeypatch.context() as patch:
        patch.setattr(libvirt.virStorageVol, "delete", fail_seed)
        with pytest.raises(libvirt.libvirtError):
            manager.delete(node.uuid)
    assert manager.get(node.name).state == meta.DELETING
    assert volumes(conn, "k3s-node-") == {node.seed_volume}
    manager.delete(node.uuid)
    assert domains(conn) == set()
    assert volumes(conn, "k3s-node-") == set()


def test_allocation_collision_never_adopts_or_deletes_existing_volume(manager, monkeypatch, conn):
    name, uuid = names.allocate("k3s-node", set())
    volume = names.seed_volume_name(name)
    add_volume(conn, volume)
    monkeypatch.setattr(names, "allocate", lambda prefix, taken: (name, uuid))
    with pytest.raises(poolmod.PoolError, match="already exist"):
        manager.create(meta.ROLE_SERVER)
    assert domains(conn) == set()
    assert volumes(conn, "k3s-node-") == {volume}


def test_existing_uuid_is_never_compensated(manager, monkeypatch, conn):
    node = manager.create(meta.ROLE_SERVER)
    manager.update_state(node.uuid, node.generation, meta.CONFIGURED)
    manager.join_ready = lambda node: True
    monkeypatch.setattr(names, "allocate", lambda prefix, taken: (node.name, node.uuid))
    with pytest.raises(poolmod.PoolError, match="already exist"):
        manager.create(meta.ROLE_AGENT)
    assert manager.get(node.name).state == meta.CONFIGURED
    assert conn.lookupByUUIDString(node.uuid).isActive()
    assert volumes(conn, "k3s-node-") == {node.volume, node.seed_volume}


def test_undefine_requires_both_volumes_confirmed_gone(manager, monkeypatch, conn):
    node = manager.create(meta.ROLE_SERVER)
    monkeypatch.setattr(libvirt.virStorageVol, "delete", lambda volume, flags=0: 0)
    with pytest.raises(poolmod.PoolError, match="still exists"):
        manager.delete(node.uuid)
    assert manager.get(node.name).state == meta.DELETING
    assert volumes(conn, "k3s-node-") == {node.volume, node.seed_volume}


@pytest.mark.parametrize("when", [BEFORE, AFTER])
def test_second_volume_create_failure_cleans_existing_overlay(manager, conn, monkeypatch, when):
    real = libvirt.virStoragePool.createXML
    attempted = []

    def fail_seed(storage, xml, flags=0):
        name = ET.fromstring(xml).findtext("name")
        attempted.append(name)
        if name.endswith(".iso"):
            assert len(attempted) == 2
            assert attempted[0].endswith(".qcow2")
            assert attempted[0] in volumes(conn)
            [node] = manager.list_nodes()
            assert node.state == meta.CREATING
            assert not conn.lookupByUUIDString(node.uuid).isActive()
            if when == AFTER:
                real(storage, xml, flags)
                assert name in volumes(conn)
            raise fake_libvirt_error(libvirt.VIR_ERR_INTERNAL_ERROR, "seed create failed")
        return real(storage, xml, flags)

    monkeypatch.setattr(libvirt.virStoragePool, "createXML", fail_seed)
    with pytest.raises(libvirt.libvirtError, match="seed create failed"):
        manager.create(meta.ROLE_SERVER)
    assert len(attempted) == 2
    assert domains(conn) == set()
    assert volumes(conn, "k3s-node-") == set()
