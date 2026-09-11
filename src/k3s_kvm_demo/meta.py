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
from uuid import UUID

import libvirt

from . import names

LEGACY_NS = "https://github.com/SUSE-Rancher-Community/k3s-kvm-demo/1.0"
NS = "https://github.com/SUSE-Rancher-Community/k3s-kvm-demo/2.0"
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

#: How each state reads on a card.  Kept beside the states themselves so adding
#: one cannot leave the template silently rendering it as 'unknown'.
TONES = {
    CREATING: "busy",
    BOOTING: "busy",
    CONFIGURING: "busy",
    CONFIGURED: "ok",
    DELETING: "busy",
    FAILED: "bad",
}


def tone(state: str) -> str:
    """The tone for *state*; 'unknown' for one a future version wrote."""
    return TONES.get(state, "unknown")


MAX_ERROR_LEN = 512


class NotManaged(Exception):
    """The domain carries no metadata of ours."""


class CompatibilityError(ValueError):
    """Managed metadata cannot safely be interpreted by this version."""


@dataclass(frozen=True, slots=True)
class NodeMeta:
    role: str
    bootstrap: bool
    volume: str
    created: str
    generation: int
    state: str
    seed_volume: str
    pool_uuid: str
    scope_prefix: str
    guest_protocol: int = 1
    error: str | None = None

    def __post_init__(self) -> None:
        # Normalise on construction so the bound holds however a NodeMeta was
        # built, not only when it came through advanced().
        object.__setattr__(self, "error", _clip(self.error))

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
    ET.SubElement(root, tag("seed_volume")).text = meta.seed_volume
    ET.SubElement(root, tag("pool_uuid")).text = meta.pool_uuid
    ET.SubElement(root, tag("scope_prefix")).text = meta.scope_prefix
    ET.SubElement(root, tag("guest_protocol")).text = str(meta.guest_protocol)
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
    """Parse v2 metadata, accepting bare tags only for setMetadata round trips."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise CompatibilityError(f"metadata is not well-formed XML: {exc}") from exc
    if root.tag.startswith(f"{{{LEGACY_NS}}}"):
        raise CompatibilityError("legacy v1 metadata requires an explicit migration or reset")
    if root.tag not in (ROOT, f"{{{NS}}}{ROOT}") or root.attrib or (root.text or "").strip():
        raise CompatibilityError("unexpected metadata root or attributes")
    namespace = f"{{{NS}}}" if root.tag.startswith("{") else ""
    required = {
        "role",
        "bootstrap",
        "volume",
        "seed_volume",
        "pool_uuid",
        "scope_prefix",
        "guest_protocol",
        "created",
        "generation",
        "state",
    }
    fields = {}
    for child in root:
        name = _localname(child.tag)
        if (
            child.tag != namespace + name
            or name not in required | {"error"}
            or name in fields
            or child.attrib
            or len(child)
            or (child.tail or "").strip()
        ):
            raise CompatibilityError("unexpected or duplicate metadata field")
        fields[name] = (child.text or "").strip()
    if not required <= fields.keys() or any(not fields[name] for name in required):
        raise CompatibilityError("missing required metadata field")
    try:
        generation = int(fields["generation"])
        protocol = int(fields["guest_protocol"])
        if generation < 1 or protocol != 1:
            raise ValueError("unsupported generation or guest protocol")
        if str(UUID(fields["pool_uuid"])) != fields["pool_uuid"]:
            raise ValueError("pool UUID must be canonical")
        names.validate_prefix(fields["scope_prefix"])
        if _dt.datetime.fromisoformat(fields["created"]).tzinfo is None:
            raise ValueError("created must include a timezone")
        if fields["role"] not in ROLES or fields["state"] not in STATES:
            raise ValueError("unsupported role or state")
        if fields["bootstrap"] not in ("true", "false"):
            raise ValueError("bootstrap must be true or false")
        if fields["bootstrap"] == "true" and fields["role"] != ROLE_SERVER:
            raise ValueError("only a server can bootstrap")
        pattern = names.volume_pattern(fields["scope_prefix"])
        if (
            not pattern.fullmatch(fields["volume"])
            or not fields["volume"].endswith(".qcow2")
            or not pattern.fullmatch(fields["seed_volume"])
            or not fields["seed_volume"].endswith(".iso")
        ):
            raise ValueError("unsafe volume claim")
    except ValueError as exc:
        raise CompatibilityError(str(exc)) from exc
    return NodeMeta(
        role=fields["role"],
        bootstrap=fields["bootstrap"] == "true",
        volume=fields["volume"],
        seed_volume=fields["seed_volume"],
        pool_uuid=fields["pool_uuid"],
        scope_prefix=fields["scope_prefix"],
        guest_protocol=protocol,
        created=fields["created"],
        generation=generation,
        state=fields["state"],
        error=fields.get("error") or None,
    )


def read(dom: libvirt.virDomain) -> NodeMeta:
    """Read the durable metadata, or raise :class:`NotManaged`."""
    # Inspect both definitions: a legacy/live-only record is not an unmanaged VM.
    flags = [libvirt.VIR_DOMAIN_XML_INACTIVE] if dom.isPersistent() else [0]
    if dom.isActive() and dom.isPersistent():
        flags.append(0)
    seen = False
    for flag in flags:
        root = ET.fromstring(dom.XMLDesc(flag))
        records = []
        for element in root.findall("./metadata/*"):
            namespace = element.tag.partition("}")[0].removeprefix("{")
            if namespace.startswith(NS.rsplit("/", 1)[0] + "/"):
                if namespace != NS:
                    raise CompatibilityError(f"incompatible managed metadata namespace {namespace}")
                records.append(parse(ET.tostring(element, encoding="unicode")))
        if len(records) > 1:
            raise CompatibilityError("duplicate managed metadata records")
        seen = seen or bool(records)
        if records and not dom.isPersistent():
            raise CompatibilityError("managed metadata must have a persistent domain")
    if not dom.isPersistent():
        raise NotManaged("transient domain carries no metadata of ours")
    try:
        raw = dom.metadata(
            libvirt.VIR_DOMAIN_METADATA_ELEMENT, NS, libvirt.VIR_DOMAIN_AFFECT_CONFIG
        )
    except libvirt.libvirtError as exc:
        if exc.get_error_code() == libvirt.VIR_ERR_NO_DOMAIN_METADATA:
            if seen:
                raise CompatibilityError(
                    "managed metadata has no persistent ownership record"
                ) from exc
            raise NotManaged("domain carries no metadata of ours") from exc
        raise
    return parse(raw)


def try_read(dom: libvirt.virDomain) -> NodeMeta | None:
    try:
        return read(dom)
    except NotManaged:
        return None


def check_compatible(conn: libvirt.virConnect) -> None:
    """Fail closed before mutations if any host domain has incompatible metadata."""
    for dom in conn.listAllDomains(0):
        try_read(dom)


def write(dom: libvirt.virDomain, meta: NodeMeta) -> None:
    """Persist *meta*, always reaching the domain's persistent configuration."""
    flags = libvirt.VIR_DOMAIN_AFFECT_CONFIG
    if dom.isActive():
        flags |= libvirt.VIR_DOMAIN_AFFECT_LIVE
    dom.setMetadata(libvirt.VIR_DOMAIN_METADATA_ELEMENT, to_xml(meta), KEY, NS, flags)
