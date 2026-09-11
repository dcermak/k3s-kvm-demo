"""Storage pool ownership and orphan eligibility.

The app deletes disks, so "is this pool mine?" has to be answered by evidence
rather than by a naming convention.  Two rules do the work: a marker volume
must be present, and every volume in the pool must be one the app recognises —
otherwise it refuses to run rather than guessing.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET

import libvirt
import pytest

from k3s_kvm_demo import meta, pool as poolmod

from .conftest import TEST_POOL, add_volume, unmanaged_domain_xml


@pytest.fixture
def storage(conn):
    return conn.storagePoolLookupByName(TEST_POOL)


def unclaimed(conn, storage, claimed: set[str]) -> set[str]:
    return poolmod.unclaimed_volumes(conn, storage, poolmod.inspect(storage, "k3s-node"), claimed)


OVERLAY = "k3s-node-" + "a" * 32 + ".qcow2"
OTHER_OVERLAY = "k3s-node-" + "b" * 32 + ".qcow2"


def test_a_pool_without_the_marker_is_refused(storage):
    with pytest.raises(poolmod.PoolError, match="init-pool"):
        poolmod.require_owned(storage, "k3s-node")


def test_a_marked_pool_with_only_overlays_is_accepted(conn, storage):
    storage.createXML(poolmod.build_marker_xml(), 0)
    add_volume(conn, OVERLAY)
    status = poolmod.require_owned(storage, "k3s-node")
    assert status.marker is True
    assert status.overlays == frozenset({OVERLAY})
    assert status.unknown == ()


def test_an_unrecognised_volume_refuses_startup_without_deleting_anything(conn, storage):
    storage.createXML(poolmod.build_marker_xml(), 0)
    add_volume(conn, "someone-elses-disk.qcow2")

    with pytest.raises(poolmod.PoolError, match="does not recognise"):
        poolmod.require_owned(storage, "k3s-node")

    remaining = {v.name() for v in storage.listAllVolumes(0)}
    assert "someone-elses-disk.qcow2" in remaining, "refusing must never mean deleting"


def test_overlays_of_another_prefix_count_as_unrecognised(conn, storage):
    storage.createXML(poolmod.build_marker_xml(), 0)
    add_volume(conn, "other-" + "c" * 32 + ".qcow2")
    status = poolmod.inspect(storage, "k3s-node")
    assert status.overlays == frozenset()
    assert status.unknown == ("other-" + "c" * 32 + ".qcow2",)


# -- orphan detection ------------------------------------------------------


def test_an_unclaimed_overlay_is_a_candidate(conn, storage):
    storage.createXML(poolmod.build_marker_xml(), 0)
    add_volume(conn, OVERLAY)
    assert unclaimed(conn, storage, set()) == {OVERLAY}


def test_a_claimed_overlay_is_never_a_candidate(conn, storage):
    storage.createXML(poolmod.build_marker_xml(), 0)
    add_volume(conn, OVERLAY)
    assert unclaimed(conn, storage, {OVERLAY}) == set()


def test_a_volume_used_by_an_unmanaged_domain_is_never_a_candidate(conn, storage):
    """However it got into the pool, a disk some other VM is booting from must
    not be deleted."""
    storage.createXML(poolmod.build_marker_xml(), 0)
    add_volume(conn, OVERLAY)
    path = storage.storageVolLookupByName(OVERLAY).path()
    conn.defineXML(unmanaged_domain_xml("someone-elses-vm", disk=path))

    assert unclaimed(conn, storage, set()) == set()


def test_the_marker_is_never_an_orphan(conn, storage):
    storage.createXML(poolmod.build_marker_xml(), 0)
    assert unclaimed(conn, storage, set()) == set()


def test_seed_is_classified_and_unclaimed_but_never_automatically_deleted(conn, storage):
    name = OVERLAY.removesuffix(".qcow2") + ".iso"
    add_volume(conn, name)
    assert unclaimed(conn, storage, set()) == {name}
    assert not hasattr(poolmod, "OrphanTracker")


# -- init-pool -------------------------------------------------------------


def test_init_pool_creates_and_marks_a_fresh_pool(conn, tmp_path):
    target = tmp_path / "demo-pool"
    created = poolmod.init_pool(conn, "k3s-demo-fresh", target)
    try:
        assert created.isActive()
        names = {v.name() for v in created.listAllVolumes(0)}
        assert poolmod.MARKER_VOLUME in names
        poolmod.require_owned(created, "k3s-node")
    finally:
        created.destroy()
        created.undefine()


def test_init_pool_refuses_a_directory_that_is_already_in_use(conn, tmp_path):
    target = tmp_path / "occupied"
    target.mkdir()
    (target / "somebody-elses.qcow2").write_bytes(b"")
    with pytest.raises(poolmod.PoolError, match="not empty"):
        poolmod.init_pool(conn, "k3s-demo-occupied", target)


def test_init_pool_refuses_to_claim_a_pool_that_already_holds_volumes(conn, storage, tmp_path):
    add_volume(conn, "pre-existing.qcow2")
    with pytest.raises(poolmod.PoolError, match="already in use"):
        poolmod.init_pool(conn, TEST_POOL, tmp_path / "unused")


def test_init_pool_is_idempotent_on_an_already_marked_pool(conn, storage, tmp_path):
    storage.createXML(poolmod.build_marker_xml(), 0)
    again = poolmod.init_pool(conn, TEST_POOL, tmp_path / "unused")
    markers = [v.name() for v in again.listAllVolumes(0) if v.name() == poolmod.MARKER_VOLUME]
    assert markers == [poolmod.MARKER_VOLUME]


def test_init_pool_checks_host_metadata_before_mutating(conn, tmp_path, monkeypatch):
    dom = conn.defineXML(unmanaged_domain_xml("legacy"))
    dom.setMetadata(
        libvirt.VIR_DOMAIN_METADATA_ELEMENT,
        "<node/>",
        meta.KEY,
        meta.LEGACY_NS,
        libvirt.VIR_DOMAIN_AFFECT_CONFIG,
    )

    def forbidden(*args):
        raise AssertionError("pool mutation must not happen")

    monkeypatch.setattr(libvirt.virConnect, "storagePoolDefineXML", forbidden)
    with pytest.raises(meta.CompatibilityError):
        poolmod.init_pool(conn, "blocked-pool", tmp_path / "pool")


@pytest.mark.parametrize("kind", [None, "qcow2", "vmdk"])
def test_strict_scan_refuses_missing_backing_evidence(conn, monkeypatch, kind):
    foreign = conn.defineXML(unmanaged_domain_xml("unknown-chain", disk="/foreign/child"))
    real = libvirt.virDomain.XMLDesc

    def described(dom, flags=0):
        root = ET.fromstring(real(dom, flags))
        if dom.UUIDString() == foreign.UUIDString() and kind:
            ET.SubElement(root.find("./devices/disk"), "driver", {"type": kind})
        return ET.tostring(root, encoding="unicode")

    monkeypatch.setattr(libvirt.virDomain, "XMLDesc", described)
    assert "/foreign/child" in poolmod.referenced_disk_paths(conn)
    with pytest.raises(poolmod.PoolError, match="complete backing chain"):
        poolmod.referenced_disk_paths(conn, strict=True)


@pytest.mark.parametrize("selection", ["empty", "other", "selected"])
@pytest.mark.parametrize("strict", [False, True])
def test_scoped_chain_validation_collects_foreign_references(conn, selection, strict):
    root = ET.fromstring(unmanaged_domain_xml("unknown-chain", disk="/foreign/child.qcow2"))
    disk = root.find("./devices/disk")
    ET.SubElement(disk, "driver", {"type": "qcow2"})
    backing = ET.SubElement(disk, "backingStore", {"type": "file"})
    ET.SubElement(backing, "format", {"type": "qcow2"})
    ET.SubElement(backing, "source", {"file": "/foreign/parent.qcow2"})
    foreign = conn.defineXML(ET.tostring(root, encoding="unicode"))
    selected = {
        "empty": set(),
        "other": {conn.lookupByName("test").UUIDString()},
        "selected": {foreign.UUIDString()},
    }[selection]

    if strict or selection == "selected":
        with pytest.raises(poolmod.PoolError, match="complete backing chain"):
            poolmod.referenced_disk_paths(conn, strict=strict, strict_uuids=selected)
    else:
        paths = poolmod.referenced_disk_paths(conn, strict_uuids=selected)
        assert {"/foreign/child.qcow2", "/foreign/parent.qcow2"} <= paths

    paths = poolmod.referenced_disk_paths(
        conn, exclude_uuid=foreign.UUIDString(), strict=strict, strict_uuids=selected
    )
    assert not {"/foreign/child.qcow2", "/foreign/parent.qcow2"} & paths
