"""Storage pool ownership.

The app deletes overlay volumes, so it must be certain the pool is its own.
Rejecting the name ``default`` would prove nothing; instead ``init-pool``
places a marker volume, and startup refuses to run against a pool that lacks
it or that contains anything the app cannot account for.

libvirt has no storage-pool metadata API, so a marker *volume* is the only
ownership record reachable through the same API and privileges already needed.
"""

from __future__ import annotations

import logging
import posixpath
import xml.etree.ElementTree as ET
from collections.abc import Set
from dataclasses import dataclass
from pathlib import Path

import libvirt

from . import domxml, meta, names

log = logging.getLogger(__name__)

MARKER_VOLUME = "k3s-kvm-demo.marker"
MARKER_BYTES = 1024 * 1024


class PoolError(Exception):
    """The pool cannot be used safely."""


@dataclass(frozen=True, slots=True)
class PoolStatus:
    marker: bool
    overlays: frozenset[str]
    unknown: tuple[str, ...]


def build_pool_xml(name: str, path: Path | str) -> str:
    root = ET.Element("pool", {"type": "dir"})
    ET.SubElement(root, "name").text = name
    target = ET.SubElement(root, "target")
    ET.SubElement(target, "path").text = str(path)
    return ET.tostring(root, encoding="unicode")


def build_marker_xml() -> str:
    root = ET.Element("volume", {"type": "file"})
    ET.SubElement(root, "name").text = MARKER_VOLUME
    ET.SubElement(root, "capacity", {"unit": "bytes"}).text = str(MARKER_BYTES)
    ET.SubElement(root, "allocation", {"unit": "bytes"}).text = "0"
    target = ET.SubElement(root, "target")
    ET.SubElement(target, "format", {"type": "raw"})
    return ET.tostring(root, encoding="unicode")


def inspect(pool: libvirt.virStoragePool, prefix: str) -> PoolStatus:
    """Classify every volume in *pool*: the marker, an overlay, or unknown."""
    overlay_re = names.volume_pattern(prefix)
    marker = False
    overlays: set[str] = set()
    unknown: list[str] = []
    for volume in pool.listAllVolumes(0):
        name = volume.name()
        if name == MARKER_VOLUME:
            marker = True
        elif overlay_re.match(name):
            overlays.add(name)
        else:
            unknown.append(name)
    return PoolStatus(marker=marker, overlays=frozenset(overlays), unknown=tuple(sorted(unknown)))


def require_owned(pool: libvirt.virStoragePool, prefix: str) -> PoolStatus:
    """Return the pool status, or explain why the pool must not be used."""
    status = inspect(pool, prefix)
    if not status.marker:
        raise PoolError(
            f"storage pool {pool.name()!r} has no {MARKER_VOLUME!r} volume, so it is not "
            "known to belong to this demo. Run 'k3s-demo init-pool' to create a dedicated "
            "pool, or point libvirt.pool at one that has already been initialised."
        )
    if status.unknown:
        raise PoolError(
            f"storage pool {pool.name()!r} contains volumes this app does not recognise: "
            f"{', '.join(status.unknown)}. Refusing to run against a pool it cannot account "
            "for. Move those volumes elsewhere or use a dedicated pool."
        )
    return status


def init_pool(conn: libvirt.virConnect, name: str, path: Path | str) -> libvirt.virStoragePool:
    """Create (or adopt an empty) pool and mark it as ours."""
    meta.check_compatible(conn)
    path = Path(path)
    try:
        pool = conn.storagePoolLookupByName(name)
    except libvirt.libvirtError as exc:
        if exc.get_error_code() != libvirt.VIR_ERR_NO_STORAGE_POOL:
            raise
        pool = None

    if pool is None:
        if path.exists() and any(path.iterdir()):
            raise PoolError(
                f"{path} already exists and is not empty; refusing to adopt it as the demo pool"
            )
        pool = conn.storagePoolDefineXML(build_pool_xml(name, path), 0)
        try:
            pool.build(0)
        except libvirt.libvirtError:
            log.debug("pool build failed (directory may already exist)", exc_info=True)

    if not pool.isActive():
        pool.create(0)
    pool.setAutostart(True)
    pool.refresh(0)

    existing = {volume.name() for volume in pool.listAllVolumes(0)}
    if MARKER_VOLUME not in existing:
        if existing:
            raise PoolError(
                f"storage pool {name!r} already holds {', '.join(sorted(existing))} but no "
                f"{MARKER_VOLUME!r}; refusing to claim a pool that is already in use"
            )
        pool.createXML(build_marker_xml(), 0)
    return pool


def source_path(conn: libvirt.virConnect, source: ET.Element) -> str:
    """Resolve supported local disk sources without consulting the client filesystem."""
    if len(source):
        raise PoolError("unsupported nested disk source syntax")
    attrs = set(source.attrib) - {"startupPolicy", "index"}
    if attrs in ({"file"}, {"dev"}):
        path = source.get("file") or source.get("dev")
    elif attrs == {"pool", "volume"}:
        storage = conn.storagePoolLookupByName(source.get("pool"))
        volume = source.get("volume")
        if not volume or "/" in volume or volume in (".", ".."):
            raise PoolError("unsafe volume reference")
        try:
            path = storage.storageVolLookupByName(volume).path()
        except libvirt.libvirtError as exc:
            if exc.get_error_code() != libvirt.VIR_ERR_NO_STORAGE_VOL:
                raise
            path = str(domxml.volume_path(storage, volume))
    else:
        raise PoolError("unsupported disk source syntax; cannot prove exclusive ownership")
    if not path or not path.startswith("/"):
        raise PoolError("disk references must be absolute local paths")
    return posixpath.normpath(path)


def referenced_disk_paths(
    conn: libvirt.virConnect,
    *,
    exclude_uuid: str | None = None,
    strict: bool = False,
    strict_uuids: Set[str] | None = None,
) -> set[str]:
    """Collect all live and persistent references, with optional backing-chain validation.

    Inactive XML can omit backing images that QEMU would discover on startup.
    Require complete evidence for all domains with strict=True, or just strict_uuids.
    Scoped deletion assumes foreign images never depend on deployment-owned volumes;
    hidden foreign backing references cannot be detected. Unscoped, non-strict scans
    are diagnostics, not authorization to delete a volume.
    """
    paths: set[str] = set()
    for dom in conn.listAllDomains(0):
        uuid = dom.UUIDString()
        if uuid == exclude_uuid:
            continue
        require_chain = strict or (strict_uuids is not None and uuid in strict_uuids)
        flags = [libvirt.VIR_DOMAIN_XML_INACTIVE] if dom.isPersistent() else [0]
        if dom.isActive() and dom.isPersistent():
            flags.append(0)
        for flag in flags:
            root = ET.fromstring(dom.XMLDesc(flag))
            for disk in root.findall("./devices/disk"):
                for source in disk.findall(".//source"):
                    paths.add(source_path(conn, source))
                for path in disk.findall(".//backingStore/path"):
                    paths.add(source_path(conn, ET.Element("source", {"file": path.text or ""})))
                if not require_chain:
                    continue
                if disk.get("device") == "cdrom" and disk.find("readonly") is not None:
                    continue
                layer = disk
                fmt = disk.find("driver")
                while True:
                    sources = layer.findall("source")
                    if len(sources) != 1:
                        raise PoolError(f"cannot establish disk source for domain {dom.name()!r}")
                    kind = fmt.get("type") if fmt is not None else None
                    backing = layer.findall("backingStore")
                    if len(backing) > 1:
                        raise PoolError("unsupported multiple backing stores")
                    if kind == "raw" and not backing:
                        break
                    if kind not in {"raw", "qcow2"} or not backing:
                        raise PoolError(
                            f"cannot prove complete backing chain for domain {dom.name()!r}; "
                            "provide explicit disk formats and a backingStore chain ending in "
                            "<backingStore/> after verifying the image's backing files"
                        )
                    layer = backing[0]
                    if not layer.attrib and not len(layer) and not (layer.text or "").strip():
                        break
                    fmt = layer.find("format")
    return paths


def unclaimed_volumes(
    conn: libvirt.virConnect,
    pool: libvirt.virStoragePool,
    status: PoolStatus,
    claimed: set[str],
) -> set[str]:
    """Diagnostic candidates with no visible claim; hidden backing references may exist."""
    target = domxml.pool_target_path(pool)
    referenced = referenced_disk_paths(conn)
    return {
        name
        for name in status.overlays
        if name not in claimed and posixpath.normpath(str(target / name)) not in referenced
    }
