"""Scoped node lifecycle with durable two-volume ownership and launch intent."""

from __future__ import annotations

import enum
import logging
import posixpath
import tempfile
import threading
import xml.etree.ElementTree as ET
from collections.abc import Callable, Collection
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

import libvirt

from . import domxml, k3sconf, meta, names, pool as poolmod, seed
from .config import Config
from .conn import ConnectionManager

log = logging.getLogger(__name__)

#: Only these mean "the driver does not know this flag"; anything else from
#: undefineFlags is a real failure and must not be papered over.
UNSUPPORTED_FLAG_CODES = frozenset(
    {
        libvirt.VIR_ERR_NO_SUPPORT,
        libvirt.VIR_ERR_INVALID_ARG,
        libvirt.VIR_ERR_ARGUMENT_UNSUPPORTED,
    }
)

UNDEFINE_FLAGS = (
    libvirt.VIR_DOMAIN_UNDEFINE_NVRAM
    | libvirt.VIR_DOMAIN_UNDEFINE_MANAGED_SAVE
    | libvirt.VIR_DOMAIN_UNDEFINE_SNAPSHOTS_METADATA
)

POWER_RUNNING = "running"
POWER_SHUT_OFF = "shut off"

_POWER_NAMES = {
    libvirt.VIR_DOMAIN_NOSTATE: "unknown",
    libvirt.VIR_DOMAIN_RUNNING: POWER_RUNNING,
    libvirt.VIR_DOMAIN_BLOCKED: POWER_RUNNING,
    libvirt.VIR_DOMAIN_PAUSED: "paused",
    libvirt.VIR_DOMAIN_SHUTDOWN: "shutting down",
    libvirt.VIR_DOMAIN_SHUTOFF: POWER_SHUT_OFF,
    libvirt.VIR_DOMAIN_CRASHED: "crashed",
    libvirt.VIR_DOMAIN_PMSUSPENDED: "suspended",
}

SERVICE_ACTIVE = "active"
SERVICE_INACTIVE = "inactive"
SERVICE_UNKNOWN = "unknown"

K3S_API_PORT = 6443

#: One bulk call replaces per-domain ``state()`` and ``info()``.  Verified on
#: both drivers: ``qemu:///system`` fills ``vcpu.current`` and
#: ``balloon.maximum`` with exactly what ``info()`` reports, and both drivers
#: include shut-off domains under these flags.  ``test:///default`` returns
#: only ``state.*``, which the configured-value fallback in ``_node_from``
#: already covers.
DOMAIN_STATS = (
    libvirt.VIR_DOMAIN_STATS_STATE
    | libvirt.VIR_DOMAIN_STATS_VCPU
    | libvirt.VIR_DOMAIN_STATS_BALLOON
)
ALL_DOMAINS = (
    libvirt.VIR_CONNECT_GET_ALL_DOMAINS_STATS_ACTIVE
    | libvirt.VIR_CONNECT_GET_ALL_DOMAINS_STATS_INACTIVE
)


class DeployRefused(Exception):
    """A deploy cannot proceed right now, for a reason worth showing a human."""


class NodeNotFound(Exception):
    """No node of ours goes by that name."""


@dataclass(frozen=True, slots=True)
class Node:
    """A managed VM: durable facts, then observed ones."""

    name: str
    uuid: str
    # durable
    role: str
    bootstrap: bool
    volume: str
    created: str
    generation: int
    state: str
    error: str | None
    # observed
    power: str
    ip: str | None
    vcpus: int
    memory_mb: int
    seed_volume: str = ""
    pool_uuid: str = ""
    service: str = SERVICE_UNKNOWN
    progress: str | None = None
    log: str | None = None
    # reserved for stats.enrich()
    cpu_percent: float | None = None
    mem_percent: float | None = None
    kubelet: str | None = None

    @property
    def short_name(self) -> str:
        prefix, _, identifier = self.name.rpartition("-")
        return f"{prefix}-{identifier[:8]}" if prefix else self.name

    @property
    def known_state(self) -> bool:
        """False for a state written by some future version of this app."""
        return self.state in meta.STATES

    @property
    def tone(self) -> str:
        return meta.tone(self.state)

    @property
    def is_server(self) -> bool:
        return self.role == meta.ROLE_SERVER

    @property
    def running(self) -> bool:
        return self.power == POWER_RUNNING

    @property
    def api_url(self) -> str | None:
        return f"https://{self.ip}:{K3S_API_PORT}" if self.ip else None


class Action(enum.Enum):
    """What reconciliation should do about a node."""

    NONE = "none"
    DELETE = "delete"
    VERIFY = "verify"


@dataclass(frozen=True, slots=True)
class Decision:
    action: Action
    reason: str | None = None


def decide(state: str, power: str) -> Decision:
    """The complete state x power table.

    Deliberately total: an undefined combination is how nodes get stranded.
    """
    if state not in meta.STATES:
        # Written by a newer version. Show it, leave it alone, allow Kill.
        return Decision(Action.NONE, "unrecognised state")

    if state in meta.INCOMPLETE:
        return Decision(Action.DELETE, f"{state} was interrupted")

    if state in {meta.BOOTING, meta.CONFIGURING, meta.CONFIGURED} and power == POWER_RUNNING:
        return Decision(Action.VERIFY)

    return Decision(Action.NONE)


def power_name(stats: dict) -> str:
    return _POWER_NAMES.get(stats.get("state.state"), "unknown")


def guest_ip(dom: libvirt.virDomain) -> str | None:
    """Best-effort IPv4 from DHCP leases; never wait on the guest agent."""
    try:
        interfaces = dom.interfaceAddresses(libvirt.VIR_DOMAIN_INTERFACE_ADDRESSES_SRC_LEASE) or {}
    except libvirt.libvirtError:
        return None
    for name, entry in interfaces.items():
        if name == "lo":
            continue
        for address in entry.get("addrs") or ():
            if address.get("type") == libvirt.VIR_IP_ADDR_TYPE_IPV4:
                return address.get("addr")
    return None


class NodeManager:
    """All libvirt mutations, serialised against one another."""

    def __init__(self, cm: ConnectionManager, cfg: Config) -> None:
        self.cm = cm
        self.cfg = cfg
        self.lock = threading.RLock()
        self.join_ready: Callable[[Node], bool] = lambda node: False
        self._domain_type: str | None = None

    # -- helpers -----------------------------------------------------------

    def domain_type(self, conn: libvirt.virConnect) -> str:
        if self._domain_type is None:
            self._domain_type = domxml.select_domain_type(conn)
            if self._domain_type == domxml.TYPE_QEMU:
                log.warning(
                    "KVM is unavailable; guests will run under TCG emulation and "
                    "will be far too slow for a live demo"
                )
        return self._domain_type

    def _pool(self, conn: libvirt.virConnect) -> libvirt.virStoragePool:
        return conn.storagePoolLookupByName(self.cfg.libvirt.pool)

    def _node_from(self, dom: libvirt.virDomain, node_meta: meta.NodeMeta, stats: dict) -> Node:
        power = power_name(stats)
        # A driver that reports no vcpu/balloon figures (the test one does not)
        # leaves the values we asked for at definition time, which are right.
        max_mem_kib = stats.get("balloon.maximum")
        return Node(
            name=dom.name(),
            uuid=dom.UUIDString(),
            role=node_meta.role,
            bootstrap=node_meta.bootstrap,
            volume=node_meta.volume,
            seed_volume=node_meta.seed_volume,
            pool_uuid=node_meta.pool_uuid,
            created=node_meta.created,
            generation=node_meta.generation,
            state=node_meta.state,
            error=node_meta.error,
            power=power,
            ip=guest_ip(dom) if power == POWER_RUNNING else None,
            vcpus=stats.get("vcpu.current") or self.cfg.vm.vcpus,
            memory_mb=max_mem_kib // 1024 if max_mem_kib else self.cfg.vm.memory_mb,
        )

    @staticmethod
    def _domains(conn: libvirt.virConnect) -> list[tuple[libvirt.virDomain, dict]]:
        """Every domain on the host, with its stats, in one round-trip."""
        return conn.getAllDomainStats(DOMAIN_STATS, ALL_DOMAINS)

    def _managed(
        self,
        conn: libvirt.virConnect,
        domains: list[tuple[libvirt.virDomain, dict]] | None = None,
    ) -> list[tuple[libvirt.virDomain, meta.NodeMeta, dict]]:
        """The domains carrying our metadata, out of *domains* or the host's."""
        found = []
        pool_uuid = self._pool(conn).UUIDString()
        for dom, stats in self._domains(conn) if domains is None else domains:
            node_meta = meta.try_read(dom)
            if (
                node_meta is not None
                and node_meta.pool_uuid == pool_uuid
                and node_meta.scope_prefix == self.cfg.vm.name_prefix
            ):
                found.append((dom, node_meta, stats))
        return found

    @staticmethod
    def _lookup(conn: libvirt.virConnect, uuid: str) -> libvirt.virDomain | None:
        """The domain, or None if it is already gone."""
        try:
            return conn.lookupByUUIDString(uuid)
        except libvirt.libvirtError as exc:
            if exc.get_error_code() == libvirt.VIR_ERR_NO_DOMAIN:
                return None
            raise

    # -- reads -------------------------------------------------------------

    def check_compatible(self) -> None:
        with self.lock:
            self.cm.read(meta.check_compatible)

    def _in_scope(self, conn: libvirt.virConnect, record: meta.NodeMeta) -> bool:
        return (
            record.pool_uuid == self._pool(conn).UUIDString()
            and record.scope_prefix == self.cfg.vm.name_prefix
        )

    def list_nodes(self) -> list[Node]:
        def run(conn: libvirt.virConnect) -> list[Node]:
            return [self._node_from(dom, m, stats) for dom, m, stats in self._managed(conn)]

        nodes = self.cm.read(run)
        nodes.sort(key=lambda n: (n.created, n.name))
        return nodes

    def states(self) -> list[str]:
        """Durable states of every managed node, from metadata alone."""
        return self.cm.read(lambda conn: [m.state for _, m, _ in self._managed(conn)])

    def get(self, name: str) -> Node:
        """One node by name, without sweeping the host.

        The name is checked against the pattern this deployment issues before
        any lookup, so a name we could not have created never resolves.
        """
        if not names.name_pattern(self.cfg.vm.name_prefix).match(name):
            raise NodeNotFound(name)

        def run(conn: libvirt.virConnect) -> Node:
            try:
                dom = conn.lookupByName(name)
            except libvirt.libvirtError as exc:
                if exc.get_error_code() == libvirt.VIR_ERR_NO_DOMAIN:
                    raise NodeNotFound(name) from exc
                raise
            node_meta = meta.try_read(dom)
            if node_meta is None or not self._in_scope(conn, node_meta):
                raise NodeNotFound(name)
            ((_, stats),) = conn.domainListGetStats([dom], DOMAIN_STATS, 0)
            return self._node_from(dom, node_meta, stats)

        return self.cm.read(run)

    # -- durable writes ----------------------------------------------------

    def update_state(
        self,
        uuid: str,
        generation: int,
        state: str,
        *,
        error: str | None = None,
        expected_states: Collection[str] | None = None,
    ) -> bool:
        """Advance a node's durable state, unless it moved on without us.

        Returns False when the write was skipped: the node is gone, is no
        longer ours, is being deleted, or the caller's generation is stale.
        This is what stops a cancelled worker waking up and overwriting a node
        that has since been killed and replaced.
        """

        def run(conn: libvirt.virConnect) -> bool:
            meta.check_compatible(conn)
            dom = self._lookup(conn, uuid)
            if dom is None:
                return False
            current = meta.try_read(dom)
            if current is None or not self._in_scope(conn, current):
                return False
            if current.generation != generation or current.state == meta.DELETING:
                return False
            if expected_states is not None and current.state not in expected_states:
                return False
            if state == meta.CREATING and current.state != meta.CREATING:
                return False
            if current.state == meta.CONFIGURED and state in {
                meta.BOOTING,
                meta.CONFIGURING,
                meta.FAILED,
            }:
                return False
            if state not in meta.STATES:
                raise ValueError(f"unknown state {state!r}")
            meta.write(dom, current.advanced(state, error=error))
            return True

        with self.lock:
            return self.cm.call(run)

    # -- launch recovery ---------------------------------------------------

    def start_if_current(self, uuid: str, generation: int, expected_state: str) -> bool:
        """Start a retained shut-off guest without changing its provisioning history."""
        def run(conn: libvirt.virConnect) -> bool:
            meta.check_compatible(conn)
            dom = self._lookup(conn, uuid)
            if dom is None:
                return False
            current = meta.try_read(dom)
            if (
                current is None
                or not self._in_scope(conn, current)
                or current.generation != generation
                or current.state != expected_state
                or current.state not in {meta.BOOTING, meta.CONFIGURING, meta.CONFIGURED}
            ):
                return False
            expected_name = names.node_name(current.scope_prefix, UUID(uuid).hex)
            if dom.name() != expected_name or (current.volume, current.seed_volume) != (
                names.volume_name(expected_name), names.seed_volume_name(expected_name)
            ):
                raise poolmod.PoolError("metadata claims do not match domain identity")
            if dom.state()[0] != libvirt.VIR_DOMAIN_SHUTOFF:
                return False
            dom.create()
            return True

        with self.lock:
            return self.cm.call(run)

    # -- deploy ------------------------------------------------------------

    def plan(self, nodes: list[Node], role: str) -> tuple[bool, str | None]:
        """Elect bootstrap, or resolve the URL a new node should join."""
        if role not in meta.ROLES:
            raise DeployRefused(f"unknown role {role!r}")

        servers = [n for n in nodes if n.is_server]
        if role == meta.ROLE_SERVER and not nodes:
            return True, None
        if nodes and not servers:
            raise DeployRefused(
                "workers remain without a control plane; reset before bootstrapping"
            )

        usable = [
            n
            for n in servers
            if n.state == meta.CONFIGURED and n.running and n.ip and self.join_ready(n)
        ]
        if not usable:
            raise DeployRefused(
                "no control plane node has finished provisioning with fresh readiness evidence; "
                "wait for a configured, running server to pass its readiness probe"
            )
        oldest = min(usable, key=lambda n: (n.created, n.name))
        return False, oldest.api_url

    def create(self, role: str) -> Node:
        """Create and launch once; all subsequent guest work is observed only."""
        with self.lock:
            return self.cm.call(lambda conn: self._create(conn, role))

    def _create(self, conn: libvirt.virConnect, role: str) -> Node:
        meta.check_compatible(conn)
        cfg = self.cfg
        domains = self._domains(conn)
        existing = [
            self._node_from(dom, m, stats) for dom, m, stats in self._managed(conn, domains)
        ]
        if len(existing) >= cfg.vm.max_nodes:
            raise DeployRefused(f"the node limit of {cfg.vm.max_nodes} is reached; kill one first")

        bootstrap, server_url = self.plan(existing, role)

        storage = self._pool(conn)
        poolmod.require_owned(storage, cfg.vm.name_prefix)

        taken = {dom.name() for dom, _ in domains}
        name, uuid = names.allocate(cfg.vm.name_prefix, taken)
        volume = names.volume_name(name)
        seed_volume = names.seed_volume_name(name)
        disk_path = domxml.volume_path(storage, volume)

        node_meta = meta.NodeMeta(
            role=role,
            bootstrap=bootstrap,
            volume=volume,
            seed_volume=seed_volume,
            pool_uuid=storage.UUIDString(),
            scope_prefix=cfg.vm.name_prefix,
            created=meta.now(),
            generation=1,
            state=meta.CREATING,
        )
        domain_xml = domxml.build_domain_xml(
            cfg,
            name=name,
            uuid=uuid,
            node_meta=node_meta,
            domain_type=self.domain_type(conn),
            disk_path=disk_path,
            seed_path=domxml.volume_path(storage, seed_volume),
        )

        config_bytes = k3sconf.config_yaml(
            node_name=name,
            role=role,
            token=cfg.cluster.token,
            is_bootstrap=bootstrap,
            server_url=server_url,
            tls_san=cfg.cluster.tls_san,
        ).encode()
        payload = seed.build_seed_payload(uuid, name, role, config_bytes)
        with tempfile.TemporaryDirectory(prefix="k3s-seed-") as temp:
            artifact = Path(temp) / seed_volume
            seed.build_seed_iso(payload, artifact)
            # Reject collisions before entering compensation: none of these resources is ours.
            present = {v.name() for v in storage.listAllVolumes(0)}
            if {volume, seed_volume} & present or self._lookup(conn, uuid) is not None:
                raise poolmod.PoolError("allocated domain or volumes already exist")
            launch_ready = False
            try:
                dom = conn.defineXML(domain_xml)
                storage.createXML(domxml.build_volume_xml(cfg, volume=volume), 0)
                seed_vol = storage.createXML(
                    domxml.build_seed_volume_xml(
                        volume=seed_volume,
                        capacity=artifact.stat().st_size,
                    ),
                    0,
                )
                seed.upload_seed(conn, seed_vol, artifact)
                node_meta = node_meta.advanced(meta.BOOTING)
                meta.write(dom, node_meta)
                launch_ready = True
                dom.create()
            except Exception:
                if not launch_ready:
                    try:
                        self._delete_if_current(conn, uuid, 1, meta.CREATING)
                    except Exception:
                        log.exception("preserving uncertain or partially cleaned node %s", name)
                raise

        # No stats call: create() has just succeeded, so the domain is running,
        # and its vcpu/memory are the ones we defined it with a moment ago.
        return self._node_from(dom, node_meta, {"state.state": libvirt.VIR_DOMAIN_RUNNING})

    # -- delete ------------------------------------------------------------

    def delete_by_name(self, name: str) -> None:
        # get() rejects a name this deployment could not have issued before any
        # lookup, so one is never resolved into a destructive call.
        uuid = self.get(name).uuid
        with self.lock:
            self.cm.call(lambda conn: self._delete(conn, uuid))

    def delete(self, uuid: str) -> None:
        with self.lock:
            self.cm.call(lambda conn: self._delete(conn, uuid))

    def delete_if_current(self, uuid: str, generation: int, expected_state: str) -> bool:
        with self.lock:
            return self.cm.call(
                lambda conn: self._delete_if_current(conn, uuid, generation, expected_state)
            )

    def _delete_if_current(
        self,
        conn: libvirt.virConnect,
        uuid: str,
        generation: int,
        expected_state: str,
    ) -> bool:
        meta.check_compatible(conn)
        dom = self._lookup(conn, uuid)
        if dom is None:
            return False
        current = meta.try_read(dom)
        if (
            current is None
            or not self._in_scope(conn, current)
            or current.generation != generation
            or current.state != expected_state
        ):
            return False
        self._delete(conn, uuid)
        return True

    def _delete(
        self,
        conn: libvirt.virConnect,
        uuid: str,
    ) -> None:
        """Idempotent teardown.  Safe to call twice, or on a half-built node."""
        meta.check_compatible(conn)
        dom = self._lookup(conn, uuid)
        if dom is None:
            return
        node_meta = meta.try_read(dom)
        if node_meta is None or not self._in_scope(conn, node_meta):
            raise NodeNotFound(uuid)
        storage = conn.storagePoolLookupByUUIDString(node_meta.pool_uuid)
        claims = (node_meta.volume, node_meta.seed_volume)
        expected_name = names.node_name(node_meta.scope_prefix, UUID(uuid).hex)
        if dom.name() != expected_name or claims != (
            names.volume_name(expected_name),
            names.seed_volume_name(expected_name),
        ):
            raise poolmod.PoolError("metadata claims do not match domain identity")
        paths = {posixpath.normpath(str(domxml.volume_path(storage, name))) for name in claims}
        if posixpath.normpath(str(self.cfg.vm.base_image)) in paths:
            raise poolmod.PoolError("a claim names the base image")
        flags = [libvirt.VIR_DOMAIN_XML_INACTIVE]
        if dom.isActive():
            flags.append(0)
        for flag in flags:
            root = ET.fromstring(dom.XMLDesc(flag))
            sources = root.findall("./devices/disk/source")
            if (
                len(root.findall("./devices/disk")) != 2
                or len(sources) != 2
                or {poolmod.source_path(conn, s) for s in sources} != paths
            ):
                raise poolmod.PoolError("domain disks do not match its two volume claims")
        if paths & poolmod.referenced_disk_paths(
            conn,
            exclude_uuid=uuid,
            strict_uuids={dom.UUIDString() for dom, _, _ in self._managed(conn)},
        ):
            raise poolmod.PoolError("another domain references a claimed volume")
        for name in claims:
            try:
                vol = storage.storageVolLookupByName(name)
            except libvirt.libvirtError as exc:
                if exc.get_error_code() != libvirt.VIR_ERR_NO_STORAGE_VOL:
                    raise
            else:
                if posixpath.normpath(vol.path()) != posixpath.normpath(
                    str(domxml.volume_path(storage, name))
                ):
                    raise poolmod.PoolError("volume path does not match its claim")

        if node_meta.state != meta.DELETING:
            meta.write(dom, node_meta.bumped().advanced(meta.DELETING))
        # No destructive action follows an ambiguous intent write.
        confirmed = meta.read(dom)
        if confirmed.state != meta.DELETING or confirmed != (
            node_meta
            if node_meta.state == meta.DELETING
            else node_meta.bumped().advanced(meta.DELETING)
        ):
            raise poolmod.PoolError("deletion intent could not be confirmed")
        if dom.isActive():
            dom.destroy()
        if dom.isActive():
            raise poolmod.PoolError("domain is still active after destroy")
        # Cooperating administrators must not attach disks while deletion runs.
        if paths & poolmod.referenced_disk_paths(
            conn,
            exclude_uuid=uuid,
            strict_uuids={dom.UUIDString() for dom, _, _ in self._managed(conn)},
        ):
            raise poolmod.PoolError("another domain references a claimed volume")
        for name in claims:
            try:
                storage.storageVolLookupByName(name).delete(0)
            except libvirt.libvirtError as exc:
                if exc.get_error_code() != libvirt.VIR_ERR_NO_STORAGE_VOL:
                    raise
        for name in claims:
            try:
                storage.storageVolLookupByName(name)
            except libvirt.libvirtError as exc:
                if exc.get_error_code() != libvirt.VIR_ERR_NO_STORAGE_VOL:
                    raise
            else:
                raise poolmod.PoolError("claimed volume still exists after deletion")
        self._undefine(dom)

    @staticmethod
    def _undefine(dom: libvirt.virDomain) -> None:
        try:
            dom.undefineFlags(UNDEFINE_FLAGS)
        except libvirt.libvirtError as exc:
            if exc.get_error_code() not in UNSUPPORTED_FLAG_CODES:
                raise
            log.debug("driver rejected undefine flags, falling back: %s", exc)
            dom.undefine()

    def reset(self) -> int:
        """Delete every managed node.  Returns how many were removed."""
        with self.lock:

            def run(conn: libvirt.virConnect) -> int:
                meta.check_compatible(conn)
                uuids = [dom.UUIDString() for dom, _, _ in self._managed(conn)]
                for uuid in uuids:
                    self._delete(conn, uuid)
                return len(uuids)

            return self.cm.call(run)

    # -- orphans -----------------------------------------------------------

    def require_owned_pool(self) -> poolmod.PoolStatus:
        """Refuse to run against a pool that is not exclusively ours."""
        return self.cm.read(
            lambda conn: poolmod.require_owned(self._pool(conn), self.cfg.vm.name_prefix)
        )

    def pool_status(self) -> poolmod.PoolStatus:
        return self.cm.read(lambda conn: poolmod.inspect(self._pool(conn), self.cfg.vm.name_prefix))

    def unclaimed_volumes(self, status: poolmod.PoolStatus | None = None) -> set[str]:
        """Overlays no managed domain claims.  Reuses *status* if given."""

        def run(conn: libvirt.virConnect) -> set[str]:
            storage = self._pool(conn)
            current = (
                poolmod.inspect(storage, self.cfg.vm.name_prefix) if status is None else status
            )
            claimed = {v for _, m, _ in self._managed(conn) for v in (m.volume, m.seed_volume)}
            return poolmod.unclaimed_volumes(conn, storage, current, claimed)

        return self.cm.read(run)
