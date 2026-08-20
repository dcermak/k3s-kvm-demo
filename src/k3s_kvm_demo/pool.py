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
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

import libvirt

from . import names

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

    @property
    def is_owned(self) -> bool:
        return self.marker and not self.unknown


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


def referenced_disk_paths(conn: libvirt.virConnect) -> set[str]:
    """Every disk source path referenced by *any* domain on the host.

    Not just managed ones: a volume an unrelated VM happens to use must never
    be considered an orphan, however it got into the pool.
    """
    paths: set[str] = set()
    for dom in conn.listAllDomains(0):
        try:
            xml = dom.XMLDesc(libvirt.VIR_DOMAIN_XML_INACTIVE)
        except libvirt.libvirtError:
            continue
        for source in ET.fromstring(xml).findall("./devices/disk/source"):
            file_path = source.get("file") or source.get("dev")
            if file_path:
                paths.add(file_path)
    return paths


@dataclass
class OrphanTracker:
    """Requires a volume to stay unclaimed before it is eligible for deletion.

    Age is measured from the first observation that saw the volume unclaimed
    rather than from a filesystem timestamp: libvirt exposes no volume mtime,
    and the pool directory is often unreadable by the service account.  A
    restart resets the clock, which errs safely.
    """

    min_age_s: float
    min_passes: int = 2
    _first_seen: dict[str, float] = field(default_factory=dict)
    _passes: dict[str, int] = field(default_factory=dict)

    def observe(
        self,
        unclaimed: set[str],
        *,
        now: float | None = None,
    ) -> list[str]:
        """Record this pass and return the volumes now eligible for deletion."""
        now = time.monotonic() if now is None else now
        for gone in set(self._first_seen) - unclaimed:
            del self._first_seen[gone]
            self._passes.pop(gone, None)

        eligible = []
        for name in sorted(unclaimed):
            self._first_seen.setdefault(name, now)
            self._passes[name] = self._passes.get(name, 0) + 1
            old_enough = now - self._first_seen[name] >= self.min_age_s
            seen_enough = self._passes[name] >= self.min_passes
            if old_enough and seen_enough:
                eligible.append(name)
        return eligible

    def forget(self, name: str) -> None:
        self._first_seen.pop(name, None)
        self._passes.pop(name, None)


def unclaimed_volumes(
    conn: libvirt.virConnect,
    pool: libvirt.virStoragePool,
    prefix: str,
    claimed: set[str],
) -> set[str]:
    """Overlays in the pool that no domain on the host lays claim to."""
    status = inspect(pool, prefix)
    target = Path(_pool_path(pool))
    referenced = referenced_disk_paths(conn)
    return {
        name
        for name in status.overlays
        if name not in claimed and str(target / name) not in referenced
    }


def _pool_path(pool: libvirt.virStoragePool) -> str:
    path = ET.fromstring(pool.XMLDesc(0)).findtext("target/path")
    if not path:
        raise PoolError(f"storage pool {pool.name()!r} has no target path")
    return path
