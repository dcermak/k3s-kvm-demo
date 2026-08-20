from __future__ import annotations

import xml.etree.ElementTree as ET

import libvirt
import pytest

from k3s_kvm_demo import domxml, meta

from .conftest import TEST_URI, make_meta


class FakeConn:
    """Just enough connection to exercise domain-type selection."""

    def __init__(self, kind: str, capabilities: str) -> None:
        self._kind = kind
        self._capabilities = capabilities

    def getType(self) -> str:
        return self._kind

    def getCapabilities(self) -> str:
        return self._capabilities


def capabilities(arch: str, *types: str) -> str:
    entries = "".join(f"<domain type='{t}'/>" for t in types)
    return (
        "<capabilities><guest><os_type>hvm</os_type>"
        f"<arch name='{arch}'>{entries}</arch></guest></capabilities>"
    )


def test_domain_type_prefers_kvm_over_tcg():
    conn = FakeConn("QEMU", capabilities("x86_64", "qemu", "kvm"))
    assert domxml.select_domain_type(conn, arch="x86_64") == domxml.TYPE_KVM


def test_domain_type_falls_back_to_qemu_when_kvm_is_absent():
    conn = FakeConn("QEMU", capabilities("x86_64", "qemu"))
    assert domxml.select_domain_type(conn, arch="x86_64") == domxml.TYPE_QEMU


def test_domain_type_ignores_other_architectures():
    conn = FakeConn("QEMU", capabilities("aarch64", "qemu", "kvm"))
    with pytest.raises(domxml.UnsupportedHypervisor):
        domxml.select_domain_type(conn, arch="x86_64")


def test_domain_type_for_the_test_driver_is_not_guessed_from_capabilities():
    """getType() is the only reliable signal here: the test driver's own
    getDomainCapabilities happily reports 'kvm'."""
    conn = FakeConn("TEST", capabilities("i686", "test"))
    assert domxml.select_domain_type(conn, arch="x86_64") == domxml.TYPE_TEST


def test_volume_xml_declares_the_backing_format(cfg):
    root = ET.fromstring(domxml.build_volume_xml(cfg, volume="k3s-node-a.qcow2"))
    backing = root.find("backingStore")
    assert backing is not None
    assert backing.findtext("path") == str(cfg.vm.base_image)
    # libvirt refuses to probe this; without it the overlay is unusable.
    assert backing.find("format").get("type") == "qcow2"
    assert root.find("target/format").get("type") == "qcow2"
    assert int(root.findtext("capacity")) == cfg.vm.disk_bytes


def test_domain_xml_has_what_provisioning_depends_on(cfg):
    node_meta = make_meta(role=meta.ROLE_SERVER, bootstrap=True)
    xml = domxml.build_domain_xml(
        cfg,
        name="k3s-node-" + "a" * 32,
        uuid="11111111-2222-3333-4444-555555555555",
        node_meta=node_meta,
        domain_type=domxml.TYPE_KVM,
        disk_path="/pool/k3s-node-a.qcow2",
    )
    root = ET.fromstring(xml)
    assert root.get("type") == "kvm"

    channels = root.findall("./devices/channel/target")
    assert any(c.get("name") == domxml.GUEST_AGENT_CHANNEL for c in channels), (
        "guest-exec is the only channel into the guest"
    )
    assert root.find("./devices/memballoon") is not None, "stats.py will need this"
    assert root.find("./devices/interface/source").get("network") == cfg.libvirt.network
    assert root.find("./devices/disk/source").get("file") == "/pool/k3s-node-a.qcow2"
    assert root.find("cpu").get("mode") == "host-passthrough"

    stored = root.find(f"./metadata/{{{meta.NS}}}node")
    assert stored is not None
    assert meta.parse(ET.tostring(stored, encoding="unicode")) == node_meta


def test_test_driver_domains_omit_host_specific_devices(cfg):
    xml = domxml.build_domain_xml(
        cfg,
        name="k3s-node-" + "b" * 32,
        uuid="22222222-2222-3333-4444-555555555555",
        node_meta=make_meta(),
        domain_type=domxml.TYPE_TEST,
        disk_path="/pool/x.qcow2",
    )
    root = ET.fromstring(xml)
    assert root.find("cpu") is None
    assert root.find("./devices/serial") is None
    # The two things the app genuinely needs are still there.
    assert root.find("./devices/memballoon") is not None
    assert root.findall("./devices/channel/target")


def test_hostile_values_are_escaped_not_interpolated(cfg, tmp_path):
    """XML is built with ElementTree, so quotes and angle brackets in a path
    or a volume name cannot break the document."""
    hostile = "evil'\"<&>.qcow2"
    root = ET.fromstring(domxml.build_volume_xml(cfg, volume=hostile))
    assert root.findtext("name") == hostile

    node_meta = make_meta(volume=hostile)
    xml = domxml.build_domain_xml(
        cfg,
        name="k3s-node-" + "c" * 32,
        uuid="33333333-2222-3333-4444-555555555555",
        node_meta=node_meta,
        domain_type=domxml.TYPE_TEST,
        disk_path='/pool/we\'re "here" & <there>.qcow2',
    )
    root = ET.fromstring(xml)
    assert root.find("./devices/disk/source").get("file") == '/pool/we\'re "here" & <there>.qcow2'
    stored = root.find(f"./metadata/{{{meta.NS}}}node")
    assert meta.parse(ET.tostring(stored, encoding="unicode")).volume == hostile


def test_generated_domain_is_accepted_by_libvirt(cfg, marked_pool):
    """The XML is not merely well-formed; the driver takes it."""
    conn = libvirt.open(TEST_URI)
    xml = domxml.build_domain_xml(
        cfg,
        name="k3s-node-" + "d" * 32,
        uuid="44444444-2222-3333-4444-555555555555",
        node_meta=make_meta(),
        domain_type=domxml.TYPE_TEST,
        disk_path="/default-pool/k3s-node-d.qcow2",
    )
    dom = conn.defineXML(xml)
    try:
        dom.create()
        assert dom.isActive()
        assert meta.read(dom).state == meta.CREATING
    finally:
        if dom.isActive():
            dom.destroy()
        dom.undefine()
        conn.close()


def test_volume_path_is_known_before_the_volume_exists(marked_pool):
    path = domxml.volume_path(marked_pool, "k3s-node-e.qcow2")
    assert str(path).endswith("/k3s-node-e.qcow2")
    existing = {v.name() for v in marked_pool.listAllVolumes(0)}
    assert "k3s-node-e.qcow2" not in existing
