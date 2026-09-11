"""The blast radius.

Everything destructive is gated twice: the name must be one this deployment
could have issued, and the domain must still carry our metadata at the moment
we act.  The volume removed is the one that metadata names — never one derived
from a URL or from the domain's name.
"""

from __future__ import annotations

import dataclasses
import xml.etree.ElementTree as ET

import libvirt
import pytest

from k3s_kvm_demo import meta, pool as poolmod
from k3s_kvm_demo.libvirtctl import NodeNotFound

from .conftest import TEST_POOL, add_volume, unmanaged_domain_xml, volumes


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
    node = manager.create(meta.ROLE_SERVER)
    add_volume(conn, "someone-elses.qcow2")

    manager.reset()
    remaining = volumes(conn)
    assert "someone-elses.qcow2" in remaining
    assert poolmod.MARKER_VOLUME in remaining
    assert node.volume not in remaining


def test_a_claim_cannot_redirect_deletion_to_another_volume(manager, conn):
    node = manager.create(meta.ROLE_SERVER)
    recorded = "k3s-node-" + "e" * 32 + ".qcow2"
    add_volume(conn, recorded)

    dom = conn.lookupByUUIDString(node.uuid)
    meta.write(dom, dataclasses.replace(meta.read(dom), volume=recorded))

    with pytest.raises(poolmod.PoolError, match="identity"):
        manager.delete(node.uuid)
    remaining = volumes(conn)
    assert recorded in remaining
    assert node.volume in remaining
    assert dom.isActive()


def test_unclaimed_volume_deletion_api_is_absent(manager):
    assert not hasattr(manager, "delete_volume")


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

    node = manager.create(meta.ROLE_SERVER)
    assert manager.get(node.name).uuid == node.uuid


@pytest.mark.parametrize("operation", ["create", "delete", "reset", "update", "check"])
@pytest.mark.parametrize("namespace", [meta.LEGACY_NS, meta.NS])
def test_incompatible_metadata_anywhere_blocks_mutation(manager, conn, operation, namespace):
    node = manager.create(meta.ROLE_SERVER)
    foreign = conn.defineXML(unmanaged_domain_xml("foreign-incompatible"))
    foreign.setMetadata(
        libvirt.VIR_DOMAIN_METADATA_ELEMENT,
        "<node/>",
        meta.KEY,
        namespace,
        libvirt.VIR_DOMAIN_AFFECT_CONFIG,
    )
    calls = {
        "create": lambda: manager.create(meta.ROLE_SERVER),
        "delete": lambda: manager.delete(node.uuid),
        "reset": manager.reset,
        "update": lambda: manager.update_state(node.uuid, node.generation, meta.CONFIGURED),
        "check": manager.check_compatible,
    }
    with pytest.raises(meta.CompatibilityError):
        calls[operation]()
    assert conn.lookupByUUIDString(node.uuid).isActive()
    assert {node.volume, node.seed_volume} <= volumes(conn)


@pytest.mark.parametrize(
    "field,value",
    [
        ("pool_uuid", "00000000-0000-0000-0000-000000000001"),
        ("scope_prefix", "foreign"),
    ],
)
def test_discovery_and_mutation_are_scoped(manager, conn, field, value):
    node = manager.create(meta.ROLE_SERVER)
    dom = conn.lookupByUUIDString(node.uuid)
    changes = {field: value}
    if field == "scope_prefix":
        changes.update(
            volume="foreign-" + "a" * 32 + ".qcow2", seed_volume="foreign-" + "a" * 32 + ".iso"
        )
    record = dataclasses.replace(meta.read(dom), **changes)
    meta.write(dom, record)
    assert manager.list_nodes() == []
    assert manager.states() == []
    with pytest.raises(NodeNotFound):
        manager.get(node.name)
    with pytest.raises(NodeNotFound):
        manager.delete(node.uuid)
    assert not manager.update_state(node.uuid, node.generation, meta.CONFIGURED)
    assert not manager.delete_if_current(node.uuid, node.generation, meta.BOOTING)
    assert manager.reset() == 0
    assert meta.read(dom) == record
    assert dom.isActive()
    assert {node.volume, node.seed_volume} <= volumes(conn)


@pytest.mark.parametrize("claim", ["volume", "seed_volume"])
@pytest.mark.parametrize("syntax", ["file", "volume", "backing"])
@pytest.mark.parametrize("view", ["persistent", "live"])
def test_other_domain_references_protect_both_disks(
    manager,
    conn,
    monkeypatch,
    claim,
    syntax,
    view,
):
    node = manager.create(meta.ROLE_SERVER)
    volume = getattr(node, claim)
    path = conn.storagePoolLookupByName(TEST_POOL).storageVolLookupByName(volume).path()
    foreign = conn.defineXML(unmanaged_domain_xml("sharing-domain"))
    foreign.create()
    real = libvirt.virDomain.XMLDesc

    def reference(dom, flags=0):
        xml = real(dom, flags)
        relevant = (flags == libvirt.VIR_DOMAIN_XML_INACTIVE) == (view == "persistent")
        if dom.UUIDString() != foreign.UUIDString() or not relevant:
            return xml
        root = ET.fromstring(xml)
        disk = ET.SubElement(root.find("devices"), "disk", {"type": "file", "device": "disk"})
        ET.SubElement(disk, "driver", {"type": "qcow2" if syntax == "backing" else "raw"})
        if syntax == "file":
            ET.SubElement(disk, "source", {"file": path})
        elif syntax == "volume":
            ET.SubElement(disk, "source", {"pool": TEST_POOL, "volume": volume})
        else:
            ET.SubElement(disk, "source", {"file": "/foreign/overlay.qcow2"})
            backing = ET.SubElement(disk, "backingStore", {"type": "file"})
            ET.SubElement(backing, "format", {"type": "raw"})
            ET.SubElement(backing, "source", {"file": path})
        return ET.tostring(root, encoding="unicode")

    monkeypatch.setattr(libvirt.virDomain, "XMLDesc", reference)
    with pytest.raises(poolmod.PoolError, match="another domain"):
        manager.delete(node.uuid)
    assert conn.lookupByUUIDString(node.uuid).isActive()
    assert {node.volume, node.seed_volume} <= volumes(conn)


def test_unsupported_reference_syntax_fails_closed(manager, conn, monkeypatch):
    node = manager.create(meta.ROLE_SERVER)
    foreign = conn.defineXML(unmanaged_domain_xml("unsupported"))
    real = libvirt.virDomain.XMLDesc

    def unsupported(dom, flags=0):
        root = ET.fromstring(real(dom, flags))
        if dom.UUIDString() == foreign.UUIDString():
            disk = ET.SubElement(root.find("devices"), "disk")
            ET.SubElement(disk, "source", {"protocol": "rbd", "name": "unknown"})
        return ET.tostring(root, encoding="unicode")

    monkeypatch.setattr(libvirt.virDomain, "XMLDesc", unsupported)
    with pytest.raises(poolmod.PoolError, match="unsupported"):
        manager.delete(node.uuid)
    assert conn.lookupByUUIDString(node.uuid).isActive()


def test_missing_recorded_pool_never_falls_back_to_pool_name(manager, conn, monkeypatch):
    node = manager.create(meta.ROLE_SERVER)
    from .conftest import fake_libvirt_error

    def missing(conn, uuid):
        raise fake_libvirt_error(libvirt.VIR_ERR_NO_STORAGE_POOL)

    monkeypatch.setattr(libvirt.virConnect, "storagePoolLookupByUUIDString", missing)
    with pytest.raises(libvirt.libvirtError):
        manager.delete(node.uuid)
    assert conn.lookupByUUIDString(node.uuid).isActive()
    assert {node.volume, node.seed_volume} <= volumes(conn)


@pytest.mark.parametrize("claim", ["volume", "seed_volume"])
def test_missing_volume_is_safe_for_partial_cleanup(manager, conn, claim):
    node = manager.create(meta.ROLE_SERVER)
    conn.lookupByUUIDString(node.uuid).destroy()
    conn.storagePoolLookupByName(TEST_POOL).storageVolLookupByName(getattr(node, claim)).delete(0)
    manager.delete(node.uuid)
    assert manager.list_nodes() == []
    assert not {node.volume, node.seed_volume} & volumes(conn)


def test_disk_sources_must_match_claims_before_destroy(manager, conn, monkeypatch):
    node = manager.create(meta.ROLE_SERVER)
    real = libvirt.virDomain.XMLDesc

    def redirected(dom, flags=0):
        root = ET.fromstring(real(dom, flags))
        if dom.UUIDString() == node.uuid:
            root.find("./devices/disk/source").set("file", "/foreign/base.qcow2")
        return ET.tostring(root, encoding="unicode")

    monkeypatch.setattr(libvirt.virDomain, "XMLDesc", redirected)
    with pytest.raises(poolmod.PoolError, match="disks do not match"):
        manager.delete(node.uuid)
    assert conn.lookupByUUIDString(node.uuid).isActive()
    assert {node.volume, node.seed_volume} <= volumes(conn)


def test_unreadable_foreign_domain_fails_closed(manager, conn, monkeypatch):
    from .conftest import fake_libvirt_error

    node = manager.create(meta.ROLE_SERVER)
    foreign = conn.defineXML(unmanaged_domain_xml("unreadable"))
    real = libvirt.virDomain.XMLDesc

    def unreadable(dom, flags=0):
        if dom.UUIDString() == foreign.UUIDString():
            raise fake_libvirt_error(libvirt.VIR_ERR_OPERATION_FAILED)
        return real(dom, flags)

    monkeypatch.setattr(libvirt.virDomain, "XMLDesc", unreadable)
    with pytest.raises(libvirt.libvirtError):
        manager.delete(node.uuid)
    assert conn.lookupByUUIDString(node.uuid).isActive()
    assert {node.volume, node.seed_volume} <= volumes(conn)


@pytest.mark.parametrize("claim", ["volume", "seed_volume"])
def test_base_image_path_cannot_be_deleted(manager, conn, claim):
    node = manager.create(meta.ROLE_SERVER)
    path = (
        conn.storagePoolLookupByName(TEST_POOL).storageVolLookupByName(getattr(node, claim)).path()
    )
    from pathlib import Path

    manager.cfg = dataclasses.replace(
        manager.cfg, vm=dataclasses.replace(manager.cfg.vm, base_image=Path(path))
    )
    with pytest.raises(poolmod.PoolError, match="base image"):
        manager.delete(node.uuid)
    assert conn.lookupByUUIDString(node.uuid).isActive()
    assert {node.volume, node.seed_volume} <= volumes(conn)


@pytest.mark.parametrize("has_source", [False, True])
def test_extra_disk_refuses_deletion_even_when_empty(manager, conn, monkeypatch, has_source):
    node = manager.create(meta.ROLE_SERVER)
    real = libvirt.virDomain.XMLDesc

    def extra_disk(dom, flags=0):
        root = ET.fromstring(real(dom, flags))
        if dom.UUIDString() == node.uuid:
            disk = ET.SubElement(root.find("devices"), "disk", {"device": "cdrom"})
            if has_source:
                ET.SubElement(disk, "source", {"file": "/foreign/extra.iso"})
        return ET.tostring(root, encoding="unicode")

    monkeypatch.setattr(libvirt.virDomain, "XMLDesc", extra_disk)
    with pytest.raises(poolmod.PoolError, match="disks do not match"):
        manager.delete(node.uuid)
    assert manager.get(node.name).state == meta.BOOTING
    assert conn.lookupByUUIDString(node.uuid).isActive()
    assert {node.volume, node.seed_volume} <= volumes(conn)


@pytest.mark.parametrize("evidence", ["partial", "complete"])
def test_stopped_foreign_child_image_protects_visible_backing(
    manager,
    conn,
    monkeypatch,
    evidence,
):
    node = manager.create(meta.ROLE_SERVER)
    storage = conn.storagePoolLookupByName(TEST_POOL)
    # A visible dependency protects its parent even without a complete chain.
    foreign = conn.defineXML(unmanaged_domain_xml("foreign-child", disk="/foreign/child.qcow2"))
    real = libvirt.virDomain.XMLDesc

    def described(dom, flags=0):
        root = ET.fromstring(real(dom, flags))
        if dom.UUIDString() == foreign.UUIDString():
            disk = root.find("./devices/disk")
            ET.SubElement(disk, "driver", {"type": "qcow2"})
            backing = ET.SubElement(disk, "backingStore", {"type": "file"})
            ET.SubElement(backing, "format", {"type": "qcow2"})
            ET.SubElement(
                backing, "source", {"file": storage.storageVolLookupByName(node.volume).path()}
            )
            if evidence == "complete":
                ET.SubElement(backing, "backingStore")
        return ET.tostring(root, encoding="unicode")

    monkeypatch.setattr(libvirt.virDomain, "XMLDesc", described)
    with pytest.raises(poolmod.PoolError, match="another domain"):
        manager.delete(node.uuid)
    assert not foreign.isActive()
    assert manager.get(node.name).state == meta.BOOTING
    assert conn.lookupByUUIDString(node.uuid).isActive()
    assert {node.volume, node.seed_volume} <= volumes(conn)


def test_hidden_external_backing_dependency_is_an_accepted_limitation(manager, conn):
    node = manager.create(meta.ROLE_SERVER)
    path = conn.storagePoolLookupByName(TEST_POOL).storageVolLookupByName(node.volume).path()
    external = conn.storagePoolCreateXML(
        "<pool type='dir'><name>external-child-pool</name>"
        "<target><path>/foreign/child-pool</path></target></pool>",
        0,
    )
    foreign = None
    try:
        child = external.createXML(
            "<volume><name>child.qcow2</name><capacity>1048576</capacity>"
            "<target><format type='qcow2'/></target><backingStore>"
            f"<path>{path}</path><format type='qcow2'/></backingStore></volume>",
            0,
        )
        root = ET.fromstring(unmanaged_domain_xml("hidden-external-child", disk=child.path()))
        ET.SubElement(root.find("./devices/disk"), "driver", {"type": "qcow2"})
        foreign = conn.defineXML(ET.tostring(root, encoding="unicode"))
        before = foreign.XMLDesc(libvirt.VIR_DOMAIN_XML_INACTIVE)
        child_before = child.XMLDesc(0)
        assert ET.fromstring(child_before).findtext("./backingStore/path") == path
        assert ET.fromstring(before).find("./devices/disk/backingStore") is None

        manager.delete(node.uuid)

        assert manager.list_nodes() == []
        assert node.uuid not in {dom.UUIDString() for dom in conn.listAllDomains(0)}
        assert not {node.volume, node.seed_volume} & volumes(conn)
        assert foreign.isPersistent()
        assert not foreign.isActive()
        assert foreign.XMLDesc(libvirt.VIR_DOMAIN_XML_INACTIVE) == before
        assert child.XMLDesc(0) == child_before
    finally:
        if foreign is not None:
            foreign.undefine()
        for volume in external.listAllVolumes(0):
            volume.delete(0)
        external.destroy()


@pytest.mark.parametrize("scope", ["managed", "pool_uuid", "scope_prefix"])
@pytest.mark.parametrize("after_stop", [False, True])
def test_incomplete_peer_chain_is_strict_only_in_managed_scope(
    manager, conn, monkeypatch, scope, after_stop
):
    node = manager.create(meta.ROLE_SERVER)
    manager.update_state(node.uuid, node.generation, meta.CONFIGURED)
    peer = manager.create(meta.ROLE_AGENT)
    target = conn.lookupByUUIDString(node.uuid)
    peer_dom = conn.lookupByUUIDString(peer.uuid)
    if scope == "pool_uuid":
        meta.write(
            peer_dom,
            dataclasses.replace(
                meta.read(peer_dom), pool_uuid="00000000-0000-0000-0000-000000000001"
            ),
        )
    elif scope == "scope_prefix":
        meta.write(
            peer_dom,
            dataclasses.replace(
                meta.read(peer_dom),
                scope_prefix="foreign",
                volume="foreign-" + "a" * 32 + ".qcow2",
                seed_volume="foreign-" + "a" * 32 + ".iso",
            ),
        )
    record = meta.read(peer_dom)
    real = libvirt.virDomain.XMLDesc
    before = [real(peer_dom, flag) for flag in (libvirt.VIR_DOMAIN_XML_INACTIVE, 0)]

    def incomplete(dom, flags=0):
        xml = real(dom, flags)
        if dom.UUIDString() != peer.uuid or (after_stop and target.isActive()):
            return xml
        root = ET.fromstring(xml)
        disk = root.find("./devices/disk")
        backing = disk.find("backingStore")
        assert backing is not None
        disk.remove(backing)
        return ET.tostring(root, encoding="unicode")

    monkeypatch.setattr(libvirt.virDomain, "XMLDesc", incomplete)
    if scope == "managed":
        with pytest.raises(poolmod.PoolError, match="complete backing chain"):
            manager.delete(node.uuid)
        assert manager.get(node.name).state == (meta.DELETING if after_stop else meta.CONFIGURED)
        assert bool(target.isActive()) is not after_stop
        assert target.isPersistent()
        assert {node.volume, node.seed_volume} <= volumes(conn)
    else:
        manager.delete(node.uuid)
        monkeypatch.setattr(libvirt.virDomain, "XMLDesc", real)
        assert manager.list_nodes() == []
        assert node.uuid not in {dom.UUIDString() for dom in conn.listAllDomains(0)}
        assert not {node.volume, node.seed_volume} & volumes(conn)
    assert peer_dom.isActive()
    assert peer_dom.isPersistent()
    assert meta.read(peer_dom) == record
    assert [real(peer_dom, flag) for flag in (libvirt.VIR_DOMAIN_XML_INACTIVE, 0)] == before
    assert {peer.volume, peer.seed_volume} <= volumes(conn)


@pytest.mark.parametrize("name", ["foreign-incomplete", LOOKALIKE])
@pytest.mark.parametrize("kind", ["unknown", "qcow2"])
def test_incomplete_foreign_disk_does_not_block_delete(manager, conn, name, kind):
    node = manager.create(meta.ROLE_SERVER)
    root = ET.fromstring(unmanaged_domain_xml(name, disk="/foreign/incomplete.qcow2"))
    if kind == "qcow2":
        ET.SubElement(root.find("./devices/disk"), "driver", {"type": "qcow2"})
    foreign = conn.defineXML(ET.tostring(root, encoding="unicode"))
    before = foreign.XMLDesc(libvirt.VIR_DOMAIN_XML_INACTIVE)
    assert ET.fromstring(before).find("./devices/disk/backingStore") is None

    manager.delete(node.uuid)

    assert manager.list_nodes() == []
    assert node.uuid not in {dom.UUIDString() for dom in conn.listAllDomains(0)}
    assert not {node.volume, node.seed_volume} & volumes(conn)
    assert foreign.isPersistent()
    assert not foreign.isActive()
    assert meta.try_read(foreign) is None
    assert foreign.XMLDesc(libvirt.VIR_DOMAIN_XML_INACTIVE) == before


@pytest.mark.parametrize("claim", ["volume", "seed_volume"])
def test_foreign_reference_added_after_stop_preserves_deleting_node(
    manager, conn, monkeypatch, claim
):
    node = manager.create(meta.ROLE_SERVER)
    path = (
        conn.storagePoolLookupByName(TEST_POOL).storageVolLookupByName(getattr(node, claim)).path()
    )
    foreign = conn.defineXML(unmanaged_domain_xml("late-reference"))
    real = libvirt.virDomain.destroy

    def stop_and_attach(dom):
        result = real(dom)
        if dom.UUIDString() == node.uuid:
            root = ET.fromstring(unmanaged_domain_xml(foreign.name(), disk=path))
            ET.SubElement(root, "uuid").text = foreign.UUIDString()
            conn.defineXML(ET.tostring(root, encoding="unicode"))
        return result

    monkeypatch.setattr(libvirt.virDomain, "destroy", stop_and_attach)
    with pytest.raises(poolmod.PoolError, match="another domain"):
        manager.delete(node.uuid)

    target = conn.lookupByUUIDString(node.uuid)
    assert not target.isActive()
    assert target.isPersistent()
    assert manager.get(node.name).state == meta.DELETING
    assert {node.volume, node.seed_volume} <= volumes(conn)
    assert foreign.isPersistent()
    assert not foreign.isActive()
    root = ET.fromstring(foreign.XMLDesc(libvirt.VIR_DOMAIN_XML_INACTIVE))
    assert root.find("./devices/disk/source").get("file") == path


@pytest.mark.parametrize("kind", ["raw", "cdrom", "terminated-qcow2"])
def test_self_contained_foreign_disk_does_not_block_delete(manager, conn, monkeypatch, kind):
    node = manager.create(meta.ROLE_SERVER)
    foreign = conn.defineXML(unmanaged_domain_xml("self-contained", disk="/foreign/disk"))
    real = libvirt.virDomain.XMLDesc

    def described(dom, flags=0):
        root = ET.fromstring(real(dom, flags))
        if dom.UUIDString() == foreign.UUIDString():
            disk = root.find("./devices/disk")
            if kind == "cdrom":
                disk.set("device", "cdrom")
                ET.SubElement(disk, "readonly")
            else:
                ET.SubElement(disk, "driver", {"type": "raw" if kind == "raw" else "qcow2"})
                if kind == "terminated-qcow2":
                    ET.SubElement(disk, "backingStore")
        return ET.tostring(root, encoding="unicode")

    monkeypatch.setattr(libvirt.virDomain, "XMLDesc", described)
    assert "/foreign/disk" in poolmod.referenced_disk_paths(
        conn, strict_uuids={foreign.UUIDString()}
    )
    manager.delete(node.uuid)
    assert manager.list_nodes() == []
    assert foreign.isPersistent()
