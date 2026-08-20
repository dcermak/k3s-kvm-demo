"""Durable metadata.

The load-bearing test here is the LIVE vs CONFIG one: a write that only
reaches the live domain is lost when libvirtd restarts, which would resurface
a ``configured`` node as ``creating`` and get it deleted as interrupted work.
"""

from __future__ import annotations

import libvirt
import pytest

from k3s_kvm_demo import meta

from .conftest import TEST_URI, make_meta

DOMAIN_XML = """
<domain type='test'>
  <name>meta-probe</name>
  <uuid>11111111-2222-3333-4444-555555555555</uuid>
  <memory unit='MiB'>256</memory><vcpu>1</vcpu>
  <os><type arch='x86_64'>hvm</type></os>
  <devices/>
</domain>
"""


@pytest.fixture
def domain():
    conn = libvirt.open(TEST_URI)
    dom = conn.defineXML(DOMAIN_XML)
    yield dom
    try:
        if dom.isActive():
            dom.destroy()
        dom.undefine()
    except libvirt.libvirtError:
        pass
    conn.close()


def test_round_trip_through_a_domain_definition():
    conn = libvirt.open(TEST_URI)
    import xml.etree.ElementTree as ET

    original = make_meta(role=meta.ROLE_SERVER, bootstrap=True, state=meta.BOOTING)
    ET.register_namespace(meta.KEY, meta.NS)
    element = ET.tostring(meta.build_element(original, qualified=True), encoding="unicode")
    dom = conn.defineXML(
        DOMAIN_XML.replace("<devices/>", f"<metadata>{element}</metadata><devices/>")
    )
    try:
        assert meta.read(dom) == original
    finally:
        dom.undefine()
        conn.close()


def test_live_only_write_never_reaches_the_persistent_config(domain):
    """The regression that would strand a configured node after a libvirtd
    restart."""
    domain.create()
    domain.setMetadata(
        libvirt.VIR_DOMAIN_METADATA_ELEMENT,
        meta.to_xml(make_meta(state=meta.CONFIGURED)),
        meta.KEY,
        meta.NS,
        libvirt.VIR_DOMAIN_AFFECT_LIVE,
    )

    with pytest.raises(libvirt.libvirtError) as excinfo:
        domain.metadata(
            libvirt.VIR_DOMAIN_METADATA_ELEMENT, meta.NS, libvirt.VIR_DOMAIN_AFFECT_CONFIG
        )
    assert excinfo.value.get_error_code() == libvirt.VIR_ERR_NO_DOMAIN_METADATA

    # meta.write always includes AFFECT_CONFIG, so it survives.
    meta.write(domain, make_meta(state=meta.CONFIGURED))
    persisted = domain.metadata(
        libvirt.VIR_DOMAIN_METADATA_ELEMENT, meta.NS, libvirt.VIR_DOMAIN_AFFECT_CONFIG
    )
    assert meta.parse(persisted).state == meta.CONFIGURED


def test_state_survives_the_domain_being_stopped(domain):
    domain.create()
    meta.write(domain, make_meta(state=meta.CONFIGURED))
    domain.destroy()
    assert meta.read(domain).state == meta.CONFIGURED

    # ...and can still be updated while inactive.
    meta.write(domain, meta.read(domain).advanced(meta.FAILED, error="boom"))
    assert meta.read(domain).state == meta.FAILED


def test_error_is_normalised_and_clipped(domain):
    noisy = "  line one\n\n   line   two  " + "x" * 900
    meta.write(domain, make_meta(state=meta.FAILED, error=noisy))
    stored = meta.read(domain).error
    assert stored is not None
    assert len(stored) <= meta.MAX_ERROR_LEN
    assert "\n" not in stored
    assert stored.startswith("line one line two")
    assert stored.endswith("…")


def test_empty_error_is_dropped(domain):
    meta.write(domain, make_meta(state=meta.FAILED, error="   "))
    assert meta.read(domain).error is None


def test_unmanaged_domain_reads_as_not_ours(domain):
    assert meta.try_read(domain) is None
    with pytest.raises(meta.NotManaged):
        meta.read(domain)


def test_unrecognised_state_is_preserved_rather_than_rejected(domain):
    """Forward compatibility: a node written by a newer version stays listed
    and stays killable."""
    meta.write(domain, make_meta(state="quiescing"))
    parsed = meta.read(domain)
    assert parsed.state == "quiescing"
    assert parsed.is_known_state is False


def test_malformed_metadata_is_not_managed():
    for broken in ("<node><role>bogus</role></node>", "<other/>", "not xml", "<node/>"):
        with pytest.raises(meta.NotManaged):
            meta.parse(broken)


def test_non_integer_generation_is_rejected():
    with pytest.raises(meta.NotManaged):
        meta.parse(
            "<node><role>agent</role><volume>v.qcow2</volume><generation>x</generation></node>"
        )


def test_generation_bump_and_advance_are_pure():
    original = make_meta(generation=3, state=meta.BOOTING)
    assert original.bumped().generation == 4
    assert original.generation == 3
    assert original.advanced(meta.FAILED, error="why").state == meta.FAILED
    assert original.state == meta.BOOTING
