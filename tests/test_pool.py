"""Storage pool ownership and orphan eligibility.

The app deletes disks, so "is this pool mine?" has to be answered by evidence
rather than by a naming convention.  Two rules do the work: a marker volume
must be present, and every volume in the pool must be one the app recognises —
otherwise it refuses to run rather than guessing.
"""

from __future__ import annotations

import libvirt
import pytest

from k3s_kvm_demo import pool as poolmod

from .conftest import TEST_POOL, TEST_URI, unmanaged_domain_xml


@pytest.fixture
def conn():
    connection = libvirt.open(TEST_URI)
    yield connection
    connection.close()


@pytest.fixture
def storage(conn):
    return conn.storagePoolLookupByName(TEST_POOL)


def add_volume(storage, name: str) -> None:
    storage.createXML(
        f"<volume type='file'><name>{name}</name>"
        "<capacity unit='bytes'>1048576</capacity>"
        "<target><format type='qcow2'/></target></volume>",
        0,
    )


OVERLAY = "k3s-node-" + "a" * 32 + ".qcow2"
OTHER_OVERLAY = "k3s-node-" + "b" * 32 + ".qcow2"


def test_a_pool_without_the_marker_is_refused(storage):
    with pytest.raises(poolmod.PoolError, match="init-pool"):
        poolmod.require_owned(storage, "k3s-node")


def test_a_marked_pool_with_only_overlays_is_accepted(storage):
    storage.createXML(poolmod.build_marker_xml(), 0)
    add_volume(storage, OVERLAY)
    status = poolmod.require_owned(storage, "k3s-node")
    assert status.marker is True
    assert status.overlays == frozenset({OVERLAY})
    assert status.unknown == ()


def test_an_unrecognised_volume_refuses_startup_without_deleting_anything(storage):
    storage.createXML(poolmod.build_marker_xml(), 0)
    add_volume(storage, "someone-elses-disk.qcow2")

    with pytest.raises(poolmod.PoolError, match="does not recognise"):
        poolmod.require_owned(storage, "k3s-node")

    remaining = {v.name() for v in storage.listAllVolumes(0)}
    assert "someone-elses-disk.qcow2" in remaining, "refusing must never mean deleting"


def test_overlays_of_another_prefix_count_as_unrecognised(storage):
    storage.createXML(poolmod.build_marker_xml(), 0)
    add_volume(storage, "other-" + "c" * 32 + ".qcow2")
    status = poolmod.inspect(storage, "k3s-node")
    assert status.overlays == frozenset()
    assert status.unknown == ("other-" + "c" * 32 + ".qcow2",)


# -- orphan detection ------------------------------------------------------


def test_an_unclaimed_overlay_is_a_candidate(conn, storage):
    storage.createXML(poolmod.build_marker_xml(), 0)
    add_volume(storage, OVERLAY)
    assert poolmod.unclaimed_volumes(conn, storage, "k3s-node", claimed=set()) == {OVERLAY}


def test_a_claimed_overlay_is_never_a_candidate(conn, storage):
    storage.createXML(poolmod.build_marker_xml(), 0)
    add_volume(storage, OVERLAY)
    assert poolmod.unclaimed_volumes(conn, storage, "k3s-node", claimed={OVERLAY}) == set()


def test_a_volume_used_by_an_unmanaged_domain_is_never_a_candidate(conn, storage):
    """However it got into the pool, a disk some other VM is booting from must
    not be deleted."""
    storage.createXML(poolmod.build_marker_xml(), 0)
    add_volume(storage, OVERLAY)
    path = storage.storageVolLookupByName(OVERLAY).path()
    conn.defineXML(unmanaged_domain_xml("someone-elses-vm", disk=path))

    assert poolmod.unclaimed_volumes(conn, storage, "k3s-node", claimed=set()) == set()


def test_the_marker_is_never_an_orphan(conn, storage):
    storage.createXML(poolmod.build_marker_xml(), 0)
    assert poolmod.unclaimed_volumes(conn, storage, "k3s-node", claimed=set()) == set()


# -- the tracker -----------------------------------------------------------


def test_deletion_needs_two_passes_and_the_age_threshold():
    tracker = poolmod.OrphanTracker(min_age_s=100, min_passes=2)
    assert tracker.observe({OVERLAY}, now=0) == []  # first sighting
    assert tracker.observe({OVERLAY}, now=50) == []  # seen twice, too young
    assert tracker.observe({OVERLAY}, now=150) == [OVERLAY]


def test_one_pass_is_never_enough_even_when_old():
    tracker = poolmod.OrphanTracker(min_age_s=0, min_passes=2)
    assert tracker.observe({OVERLAY}, now=1000) == []
    assert tracker.observe({OVERLAY}, now=2000) == [OVERLAY]


def test_a_volume_that_gets_claimed_again_restarts_the_clock():
    tracker = poolmod.OrphanTracker(min_age_s=100, min_passes=2)
    tracker.observe({OVERLAY}, now=0)
    tracker.observe(set(), now=10)  # claimed again, or deleted
    assert tracker.observe({OVERLAY}, now=20) == []
    assert tracker.observe({OVERLAY}, now=60) == []
    assert tracker.observe({OVERLAY}, now=130) == [OVERLAY]


def test_tracking_is_per_volume():
    tracker = poolmod.OrphanTracker(min_age_s=10, min_passes=2)
    tracker.observe({OVERLAY}, now=0)
    tracker.observe({OVERLAY, OTHER_OVERLAY}, now=20)
    assert tracker.observe({OVERLAY, OTHER_OVERLAY}, now=40) == [OVERLAY, OTHER_OVERLAY]


def test_forget_drops_the_history():
    tracker = poolmod.OrphanTracker(min_age_s=0, min_passes=2)
    tracker.observe({OVERLAY}, now=0)
    tracker.forget(OVERLAY)
    assert tracker.observe({OVERLAY}, now=100) == []


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
    add_volume(storage, "pre-existing.qcow2")
    with pytest.raises(poolmod.PoolError, match="already in use"):
        poolmod.init_pool(conn, TEST_POOL, tmp_path / "unused")


def test_init_pool_is_idempotent_on_an_already_marked_pool(conn, storage, tmp_path):
    storage.createXML(poolmod.build_marker_xml(), 0)
    again = poolmod.init_pool(conn, TEST_POOL, tmp_path / "unused")
    markers = [v.name() for v in again.listAllVolumes(0) if v.name() == poolmod.MARKER_VOLUME]
    assert markers == [poolmod.MARKER_VOLUME]
