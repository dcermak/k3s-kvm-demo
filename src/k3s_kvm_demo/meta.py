"""Durable per-node state, stored as libvirt domain metadata.

This module is the *only* caller of ``virDomain.setMetadata``.  That matters:
a write made with ``VIR_DOMAIN_AFFECT_LIVE`` alone never reaches the persistent
config, so a ``configured`` node would resurface as ``creating`` after libvirtd
restarts.  Writes therefore always carry ``AFFECT_CONFIG`` (plus ``AFFECT_LIVE``
when the domain is running) and reads always ask for ``AFFECT_CONFIG``.

The metadata is also the ownership record.  A domain without it is not ours and
must never be touched; libvirt signals that with ``VIR_ERR_NO_DOMAIN_METADATA``.
"""

from __future__ import annotations

import datetime as _dt
import xml.etree.ElementTree as ET
from dataclasses import dataclass, replace

import libvirt

NS = "https://github.com/SUSE-Rancher-Community/k3s-kvm-demo/1.0"
KEY = "k3s"
ROOT = "node"

ROLE_SERVER = "server"
ROLE_AGENT = "agent"
ROLES = frozenset({ROLE_SERVER, ROLE_AGENT})

#: Durable provisioning states.  Deliberately *not* including power state: a VM
#: being shut off must never overwrite the record of how provisioning ended.
CREATING = "creating"
BOOTING = "booting"
CONFIGURING = "configuring"
CONFIGURED = "configured"
FAILED = "failed"
DELETING = "deleting"
STATES = frozenset({CREATING, BOOTING, CONFIGURING, CONFIGURED, FAILED, DELETING})

#: States that mean an operation was interrupted half-way and must be completed.
INCOMPLETE = frozenset({CREATING, DELETING})

MAX_ERROR_LEN = 512


class NotManaged(Exception):
    """The domain carries no metadata of ours."""


@dataclass(frozen=True, slots=True)
class NodeMeta:
    role: str
    bootstrap: bool
    volume: str
    created: str
    generation: int
    state: str
    error: str | None = None

    def __post_init__(self) -> None:
        # Normalise on construction so the bound holds however a NodeMeta was
        # built, not only when it came through advanced().
        object.__setattr__(self, "error", _clip(self.error))

    @property
    def is_known_state(self) -> bool:
        """False for a state written by some future version of this app."""
        return self.state in STATES

    def advanced(self, state: str, *, error: str | None = None) -> NodeMeta:
        return replace(self, state=state, error=error)

    def bumped(self) -> NodeMeta:
        return replace(self, generation=self.generation + 1)


def now() -> str:
    return _dt.datetime.now(_dt.UTC).replace(microsecond=0).isoformat()


def _clip(error: str | None) -> str | None:
    if error is None:
        return None
    error = " ".join(error.split())
    if len(error) > MAX_ERROR_LEN:
        error = error[: MAX_ERROR_LEN - 1] + "…"
    return error or None


def _localname(tag: str) -> str:
    return tag.rpartition("}")[2]


def build_element(meta: NodeMeta, *, qualified: bool) -> ET.Element:
    """Build the metadata element.

    *qualified* selects namespace-prefixed tags, which is what an inline
    ``<metadata>`` block in a domain definition needs.  ``setMetadata`` instead
    takes a bare element and applies the namespace itself.
    """

    def tag(name: str) -> str:
        return f"{{{NS}}}{name}" if qualified else name

    root = ET.Element(tag(ROOT))
    ET.SubElement(root, tag("role")).text = meta.role
    ET.SubElement(root, tag("bootstrap")).text = "true" if meta.bootstrap else "false"
    ET.SubElement(root, tag("volume")).text = meta.volume
    ET.SubElement(root, tag("created")).text = meta.created
    ET.SubElement(root, tag("generation")).text = str(meta.generation)
    ET.SubElement(root, tag("state")).text = meta.state
    if meta.error:
        ET.SubElement(root, tag("error")).text = meta.error
    return root


def to_xml(meta: NodeMeta) -> str:
    """Serialise for ``setMetadata`` (bare element, namespace supplied out of band)."""
    return ET.tostring(build_element(meta, qualified=False), encoding="unicode")


def parse(xml_text: str) -> NodeMeta:
    """Parse what ``virDomain.metadata`` returned.

    libvirt normalises the stored element, and whether it hands back namespaced
    tags has varied, so local names are used throughout.
    """
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise NotManaged(f"metadata is not well-formed XML: {exc}") from exc
    if _localname(root.tag) != ROOT:
        raise NotManaged(f"unexpected metadata root <{_localname(root.tag)}>")

    fields = {_localname(child.tag): (child.text or "").strip() for child in root}

    role = fields.get("role", "")
    if role not in ROLES:
        raise NotManaged(f"unknown role {role!r}")
    volume = fields.get("volume", "")
    if not volume:
        raise NotManaged("metadata has no volume")
    try:
        generation = int(fields.get("generation", "0"))
    except ValueError:
        raise NotManaged(f"generation {fields.get('generation')!r} is not an integer") from None

    # An unrecognised state is kept verbatim rather than rejected, so a node
    # written by a newer version is still listed and still killable.
    return NodeMeta(
        role=role,
        bootstrap=fields.get("bootstrap") == "true",
        volume=volume,
        created=fields.get("created", ""),
        generation=generation,
        state=fields.get("state") or CREATING,
        error=fields.get("error") or None,
    )


def read(dom: libvirt.virDomain) -> NodeMeta:
    """Read the durable metadata, or raise :class:`NotManaged`."""
    try:
        raw = dom.metadata(
            libvirt.VIR_DOMAIN_METADATA_ELEMENT, NS, libvirt.VIR_DOMAIN_AFFECT_CONFIG
        )
    except libvirt.libvirtError as exc:
        if exc.get_error_code() in (
            libvirt.VIR_ERR_NO_DOMAIN_METADATA,
            libvirt.VIR_ERR_NO_DOMAIN,
        ):
            raise NotManaged("domain carries no metadata of ours") from exc
        raise
    return parse(raw)


def try_read(dom: libvirt.virDomain) -> NodeMeta | None:
    try:
        return read(dom)
    except NotManaged:
        return None


def write(dom: libvirt.virDomain, meta: NodeMeta) -> None:
    """Persist *meta*, always reaching the domain's persistent configuration."""
    flags = libvirt.VIR_DOMAIN_AFFECT_CONFIG
    if dom.isActive():
        flags |= libvirt.VIR_DOMAIN_AFFECT_LIVE
    dom.setMetadata(libvirt.VIR_DOMAIN_METADATA_ELEMENT, to_xml(meta), KEY, NS, flags)
