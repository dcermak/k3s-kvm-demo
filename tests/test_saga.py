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

from k3s_kvm_demo import meta

from .conftest import AFTER, BEFORE, TEST_POOL, TEST_URI


@pytest.fixture
def conn():
    connection = libvirt.open(TEST_URI)
    yield connection
    connection.close()


def volumes(conn) -> set[str]:
    return {
        v.name()
        for v in conn.storagePoolLookupByName(TEST_POOL).listAllVolumes(0)
        if v.name().startswith("k3s-node-")
    }


def domains(conn) -> set[str]:
    return {d.name() for d in conn.listAllDomains(0) if d.name().startswith("k3s-node-")}


@pytest.mark.parametrize("stage", ["define", "volume", "start"])
@pytest.mark.parametrize("when", [BEFORE, AFTER])
def test_a_failed_create_leaves_nothing_behind(faulty_manager, faults, conn, stage, when):
    setattr(faults, stage, when)

    with pytest.raises(libvirt.libvirtError):
        faulty_manager.create(meta.ROLE_SERVER)

    # Stop injecting so the assertions see the real state.
    setattr(faults, stage, None)
    assert domains(conn) == set(), f"orphan domain after {stage} failed {when}"
    assert volumes(conn) == set(), f"orphan volume after {stage} failed {when}"
    assert faulty_manager.list_nodes() == []


def test_a_client_side_error_after_start_destroys_the_domain_before_its_disk(
    faulty_manager, faults, conn, monkeypatch
):
    """The disaster case: libvirt started the VM, we never heard, and naive
    compensation would pull the disk out from under it."""
    order: list[str] = []
    real_destroy = libvirt.virDomain.destroy
    real_delete = libvirt.virStorageVol.delete

    def note_destroy(self):
        order.append("destroy")
        return real_destroy(self)

    def note_delete(self, flags=0):
        order.append("delete-volume")
        return real_delete(self, flags)

    monkeypatch.setattr(libvirt.virDomain, "destroy", note_destroy)
    monkeypatch.setattr(libvirt.virStorageVol, "delete", note_delete)

    faults.start = AFTER
    with pytest.raises(libvirt.libvirtError):
        faulty_manager.create(meta.ROLE_SERVER)
    faults.start = None

    assert order.index("destroy") < order.index("delete-volume")
    assert domains(conn) == set()
    assert volumes(conn) == set()


def test_a_failed_volume_delete_keeps_the_domain_and_its_ownership(faulty_manager, faults, conn):
    """Ownership must outlive the volume: if the disk cannot be removed, the
    domain stays defined in 'deleting' so reconciliation can try again."""
    node, _ = faulty_manager.create(meta.ROLE_SERVER)

    faults.volume_delete = BEFORE
    with pytest.raises(libvirt.libvirtError):
        faulty_manager.delete(node.uuid)
    faults.volume_delete = None

    assert node.name in domains(conn)
    surviving = faulty_manager.get(node.name)
    assert surviving.state == meta.DELETING
    assert surviving.volume == node.volume, "the ownership record is still there"
    assert node.volume in volumes(conn)

    # A retry finishes the job.
    faulty_manager.delete(node.uuid)
    assert domains(conn) == set()
    assert volumes(conn) == set()


def test_delete_bumps_the_generation_before_touching_anything(faulty_manager, faults, conn):
    node, _ = faulty_manager.create(meta.ROLE_SERVER)
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
    node, _ = manager.create(meta.ROLE_SERVER)
    manager.delete(node.uuid)
    manager.delete(node.uuid)
    assert domains(conn) == set()
    assert volumes(conn) == set()


def test_a_half_built_node_is_finished_off_by_a_later_delete(faulty_manager, faults, conn):
    """Simulate the process dying right after defineXML: the domain exists in
    'creating' with no volume, and deletion must still tidy it away."""
    faults.volume = BEFORE
    with pytest.raises(libvirt.libvirtError):
        faulty_manager.create(meta.ROLE_SERVER)
    faults.volume = None
    assert domains(conn) == set()

    # Rebuild that state deliberately, as a crash would leave it.
    node, _ = faulty_manager.create(meta.ROLE_SERVER)
    conn.storagePoolLookupByName(TEST_POOL).storageVolLookupByName(node.volume).delete(0)

    faulty_manager.delete(node.uuid)
    assert domains(conn) == set()
