"""The blast radius.

Everything destructive is gated twice: the name must be one this deployment
could have issued, and the domain must still carry our metadata at the moment
we act.  The volume removed is the one that metadata names — never one derived
from a URL or from the domain's name.
"""

from __future__ import annotations

import dataclasses

import libvirt
import pytest

from k3s_kvm_demo import meta, pool as poolmod
from k3s_kvm_demo.libvirtctl import NodeNotFound

from .conftest import TEST_POOL, TEST_URI, unmanaged_domain_xml


@pytest.fixture
def conn():
    connection = libvirt.open(TEST_URI)
    yield connection
    connection.close()


def add_volume(conn, name: str) -> str:
    storage = conn.storagePoolLookupByName(TEST_POOL)
    storage.createXML(
        f"<volume type='file'><name>{name}</name>"
        "<capacity unit='bytes'>1048576</capacity>"
        "<target><format type='qcow2'/></target></volume>",
        0,
    )
    return storage.storageVolLookupByName(name).path()


def volumes(conn) -> set[str]:
    return {v.name() for v in conn.storagePoolLookupByName(TEST_POOL).listAllVolumes(0)}


LOOKALIKE = "k3s-node-" + "f" * 32


def test_a_lookalike_domain_that_is_not_ours_is_not_killable(manager, conn):
    """The name matches our pattern exactly, but there is no metadata, so it
    is somebody else's VM."""
    conn.defineXML(unmanaged_domain_xml(LOOKALIKE))

    with pytest.raises(NodeNotFound):
        manager.delete_by_name(LOOKALIKE)
    assert LOOKALIKE in {d.name() for d in conn.listAllDomains(0)}


def test_a_name_we_could_not_have_issued_is_rejected_before_any_lookup(manager, monkeypatch):
    def forbidden(self, name):
        raise AssertionError("no lookup may happen for a name that fails validation")

    monkeypatch.setattr(libvirt.virConnect, "lookupByName", forbidden)

    for bad in ("someone-elses-vm", "k3s-node", "k3s-node-01", "../../etc/passwd", ""):
        with pytest.raises(NodeNotFound):
            manager.delete_by_name(bad)


def test_reset_leaves_unmanaged_domains_alone(manager, conn):
    conn.defineXML(unmanaged_domain_xml("bystander"))
    conn.defineXML(unmanaged_domain_xml(LOOKALIKE))
    manager.create(meta.ROLE_SERVER)

    assert manager.reset() == 1
    survivors = {d.name() for d in conn.listAllDomains(0)}
    assert {"bystander", LOOKALIKE, "test"} <= survivors


def test_reset_leaves_volumes_it_does_not_own_alone(manager, conn):
    # The foreign volume is added after the node exists: creating one is
    # deliberately refused against a pool holding anything unrecognised.
    node, _ = manager.create(meta.ROLE_SERVER)
    add_volume(conn, "someone-elses.qcow2")

    manager.reset()
    remaining = volumes(conn)
    assert "someone-elses.qcow2" in remaining
    assert poolmod.MARKER_VOLUME in remaining
    assert node.volume not in remaining


def test_the_volume_deleted_is_the_one_metadata_names(manager, conn):
    """Not the one the domain name implies: metadata is the ownership record,
    and deletion must follow it."""
    node, _ = manager.create(meta.ROLE_SERVER)
    recorded = "k3s-node-" + "e" * 32 + ".qcow2"
    add_volume(conn, recorded)

    dom = conn.lookupByUUIDString(node.uuid)
    meta.write(dom, dataclasses.replace(meta.read(dom), volume=recorded))

    manager.delete(node.uuid)
    remaining = volumes(conn)
    assert recorded not in remaining, "the metadata-named volume must go"
    assert node.volume in remaining, "the name-derived volume was never ours to delete"


def test_delete_volume_refuses_a_name_outside_our_pattern(manager):
    for bad in ("someone-elses.qcow2", poolmod.MARKER_VOLUME, "k3s-node-01.qcow2"):
        with pytest.raises(ValueError, match="not an overlay"):
            manager.delete_volume(bad)


def test_orphan_detection_never_offers_a_foreign_volume(manager, conn):
    orphan = "k3s-node-" + "d" * 32 + ".qcow2"
    add_volume(conn, orphan)
    add_volume(conn, "someone-elses.qcow2")

    # Unrecognised volumes are not classified as overlays at all, so they can
    # never become reaping candidates however long they sit there.
    assert manager.unclaimed_volumes() == {orphan}
    assert poolmod.inspect(conn.storagePoolLookupByName(TEST_POOL), "k3s-node").unknown == (
        "someone-elses.qcow2",
    )


def test_orphan_detection_lists_only_unclaimed_overlays(manager, conn):
    manager.create(meta.ROLE_SERVER)
    orphan = "k3s-node-" + "d" * 32 + ".qcow2"
    add_volume(conn, orphan)

    assert manager.unclaimed_volumes() == {orphan}


def test_a_lookalike_domain_protects_its_disk_from_reaping(manager, conn):
    orphan = "k3s-node-" + "d" * 32 + ".qcow2"
    path = add_volume(conn, orphan)
    conn.defineXML(unmanaged_domain_xml(LOOKALIKE, disk=path))

    assert manager.unclaimed_volumes() == set()


def test_get_only_finds_our_own_nodes(manager, conn):
    conn.defineXML(unmanaged_domain_xml(LOOKALIKE))
    with pytest.raises(NodeNotFound):
        manager.get(LOOKALIKE)

    node, _ = manager.create(meta.ROLE_SERVER)
    assert manager.get(node.name).uuid == node.uuid
