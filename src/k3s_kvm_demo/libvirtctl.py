"""Node lifecycle: discovery, the create saga, idempotent deletion.

Every destructive call re-reads the domain's own metadata immediately before
acting, and the volume it deletes is always the one that metadata names — never
one derived from a URL or a domain name.

The domain is defined *before* its volume exists and undefined *after* that
volume is gone, so the durable ownership record strictly brackets the volume's
life.  Any create step that fails hands off to the same idempotent delete saga,
which first observes what the hypervisor actually has: a call may well have
succeeded server-side and then raised client-side, and tearing down a running
domain's disk without destroying the domain first would be a disaster.
"""

from __future__ import annotations

import enum
import logging
import threading
from dataclasses import dataclass, replace

import libvirt

from . import domxml, meta, names, pool as poolmod
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
    known_state: bool
    # observed
    power: str
    ip: str | None
    vcpus: int
    memory_mb: int
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
    AWAIT_AGENT = "await-agent"
    CONFIGURE = "configure"
    VERIFY = "verify"
    FAIL = "fail"


@dataclass(frozen=True, slots=True)
class Decision:
    action: Action
    reason: str | None = None


def decide(state: str, power: str, *, known_state: bool = True) -> Decision:
    """The complete state x power table.

    Deliberately total: an undefined combination is how nodes get stranded.
    """
    if not known_state:
        # Written by a newer version. Show it, leave it alone, allow Kill.
        return Decision(Action.NONE, "unrecognised state")

    if state in meta.INCOMPLETE:
        return Decision(Action.DELETE, f"{state} was interrupted")

    running = power == POWER_RUNNING

    if state == meta.BOOTING:
        if running:
            return Decision(Action.AWAIT_AGENT)
        return Decision(Action.FAIL, "stopped before first boot completed")

    if state == meta.CONFIGURING:
        if running:
            return Decision(Action.CONFIGURE)
        return Decision(Action.FAIL, "stopped during configuration")

    if state == meta.CONFIGURED:
        # A power cycle must never rewrite how provisioning ended; a stopped
        # node stays 'configured' and simply reads as shut off.
        return Decision(Action.VERIFY) if running else Decision(Action.NONE)

    return Decision(Action.NONE)


def power_name(dom: libvirt.virDomain) -> str:
    try:
        return _POWER_NAMES.get(dom.state()[0], "unknown")
    except libvirt.libvirtError:
        return "unknown"


def guest_ip(dom: libvirt.virDomain) -> str | None:
    """Best-effort IPv4 for a running domain: guest agent first, then lease."""
    for source in (
        libvirt.VIR_DOMAIN_INTERFACE_ADDRESSES_SRC_AGENT,
        libvirt.VIR_DOMAIN_INTERFACE_ADDRESSES_SRC_LEASE,
    ):
        try:
            interfaces = dom.interfaceAddresses(source) or {}
        except libvirt.libvirtError:
            continue
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

    def _node_from(self, dom: libvirt.virDomain, node_meta: meta.NodeMeta) -> Node:
        power = power_name(dom)
        vcpus = self.cfg.vm.vcpus
        memory_mb = self.cfg.vm.memory_mb
        try:
            _, max_mem_kib, _, nr_vcpu, _ = dom.info()
            if nr_vcpu:
                vcpus = nr_vcpu
            if max_mem_kib:
                memory_mb = max_mem_kib // 1024
        except libvirt.libvirtError:
            pass
        return Node(
            name=dom.name(),
            uuid=dom.UUIDString(),
            role=node_meta.role,
            bootstrap=node_meta.bootstrap,
            volume=node_meta.volume,
            created=node_meta.created,
            generation=node_meta.generation,
            state=node_meta.state,
            error=node_meta.error,
            known_state=node_meta.is_known_state,
            power=power,
            ip=guest_ip(dom) if power == POWER_RUNNING else None,
            vcpus=vcpus,
            memory_mb=memory_mb,
        )

    def _managed(self, conn: libvirt.virConnect) -> list[tuple[libvirt.virDomain, meta.NodeMeta]]:
        found = []
        for dom in conn.listAllDomains(0):
            node_meta = meta.try_read(dom)
            if node_meta is not None:
                found.append((dom, node_meta))
        return found

    # -- reads -------------------------------------------------------------

    def list_nodes(self) -> list[Node]:
        def run(conn: libvirt.virConnect) -> list[Node]:
            return [self._node_from(dom, m) for dom, m in self._managed(conn)]

        nodes = self.cm.read(run)
        nodes.sort(key=lambda n: (n.created, n.name))
        return nodes

    def get(self, name: str) -> Node:
        for node in self.list_nodes():
            if node.name == name:
                return node
        raise NodeNotFound(name)

    def read_meta(self, uuid: str) -> meta.NodeMeta | None:
        def run(conn: libvirt.virConnect) -> meta.NodeMeta | None:
            try:
                dom = conn.lookupByUUIDString(uuid)
            except libvirt.libvirtError as exc:
                if exc.get_error_code() == libvirt.VIR_ERR_NO_DOMAIN:
                    return None
                raise
            return meta.try_read(dom)

        return self.cm.read(run)

    # -- durable writes ----------------------------------------------------

    def update_state(
        self, uuid: str, generation: int, state: str, *, error: str | None = None
    ) -> bool:
        """Advance a node's durable state, unless it moved on without us.

        Returns False when the write was skipped: the node is gone, is no
        longer ours, is being deleted, or the caller's generation is stale.
        This is what stops a cancelled worker waking up and overwriting a node
        that has since been killed and replaced.
        """

        def run(conn: libvirt.virConnect) -> bool:
            try:
                dom = conn.lookupByUUIDString(uuid)
            except libvirt.libvirtError as exc:
                if exc.get_error_code() == libvirt.VIR_ERR_NO_DOMAIN:
                    return False
                raise
            current = meta.try_read(dom)
            if current is None:
                return False
            if current.generation != generation or current.state == meta.DELETING:
                return False
            meta.write(dom, current.advanced(state, error=error))
            return True

        return self.cm.call(run)

    # -- deploy ------------------------------------------------------------

    def plan(self, nodes: list[Node], role: str) -> tuple[bool, str | None]:
        """Elect bootstrap, or resolve the URL a new node should join."""
        if role not in meta.ROLES:
            raise DeployRefused(f"unknown role {role!r}")

        servers = [n for n in nodes if n.is_server]
        if role == meta.ROLE_SERVER and not servers:
            return True, None

        usable = [
            n for n in servers if n.state == meta.CONFIGURED and n.power == POWER_RUNNING and n.ip
        ]
        if not usable:
            raise DeployRefused(
                "no control plane node has finished provisioning yet — wait for the "
                "first one to report 'configured' before adding more nodes"
            )
        oldest = min(usable, key=lambda n: (n.created, n.name))
        return False, oldest.api_url

    def create(self, role: str) -> tuple[Node, str | None]:
        """Create and start a node.  Returns the node and its join URL."""
        with self.lock:
            return self.cm.call(lambda conn: self._create(conn, role))

    def _create(self, conn: libvirt.virConnect, role: str) -> tuple[Node, str | None]:
        cfg = self.cfg
        existing = [self._node_from(dom, m) for dom, m in self._managed(conn)]
        if len(existing) >= cfg.vm.max_nodes:
            raise DeployRefused(f"the node limit of {cfg.vm.max_nodes} is reached; kill one first")

        bootstrap, server_url = self.plan(existing, role)

        storage = self._pool(conn)
        poolmod.require_owned(storage, cfg.vm.name_prefix)

        taken = {dom.name() for dom in conn.listAllDomains(0)}
        name, uuid = names.allocate(cfg.vm.name_prefix, taken)
        volume = names.volume_name(name)
        disk_path = domxml.volume_path(storage, volume)

        node_meta = meta.NodeMeta(
            role=role,
            bootstrap=bootstrap,
            volume=volume,
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
        )

        try:
            # Define first: ownership must predate the volume it authorises us
            # to delete. libvirt does not require the disk to exist yet.
            dom = conn.defineXML(domain_xml)
            storage.createXML(domxml.build_volume_xml(cfg, volume=volume), 0)
            dom.create()
            node_meta = node_meta.advanced(meta.BOOTING)
            meta.write(dom, node_meta)
        except Exception:
            log.exception("creating %s failed; rolling back", name)
            try:
                self._delete(conn, uuid, expected_volume=volume)
            except Exception:
                log.exception("rollback of %s did not fully succeed", name)
            raise

        return self._node_from(dom, node_meta), server_url

    # -- delete ------------------------------------------------------------

    def delete_by_name(self, name: str) -> None:
        pattern = names.name_pattern(self.cfg.vm.name_prefix)
        if not pattern.match(name):
            # Reject before any lookup: a name we could not have issued must
            # never reach a destructive call.
            raise NodeNotFound(name)
        with self.lock:
            uuid = self.cm.read(lambda conn: self._uuid_for(conn, name))
            self.cm.call(lambda conn: self._delete(conn, uuid))

    def _uuid_for(self, conn: libvirt.virConnect, name: str) -> str:
        try:
            dom = conn.lookupByName(name)
        except libvirt.libvirtError as exc:
            if exc.get_error_code() == libvirt.VIR_ERR_NO_DOMAIN:
                raise NodeNotFound(name) from exc
            raise
        if meta.try_read(dom) is None:
            raise NodeNotFound(name)
        return dom.UUIDString()

    def delete(self, uuid: str) -> None:
        with self.lock:
            self.cm.call(lambda conn: self._delete(conn, uuid))

    def _delete(
        self,
        conn: libvirt.virConnect,
        uuid: str,
        *,
        expected_volume: str | None = None,
    ) -> None:
        """Idempotent teardown.  Safe to call twice, or on a half-built node."""
        try:
            dom = conn.lookupByUUIDString(uuid)
        except libvirt.libvirtError as exc:
            if exc.get_error_code() != libvirt.VIR_ERR_NO_DOMAIN:
                raise
            dom = None

        volume = expected_volume
        if dom is not None:
            node_meta = meta.try_read(dom)
            if node_meta is None:
                if expected_volume is None:
                    # Not ours. Refuse rather than guess.
                    raise NodeNotFound(uuid)
            else:
                volume = node_meta.volume
                # Bump the generation before anything destructive so a worker
                # that wakes up later cannot write over the replacement.
                try:
                    meta.write(dom, node_meta.bumped().advanced(meta.DELETING))
                except libvirt.libvirtError:
                    log.warning("could not mark %s as deleting", uuid, exc_info=True)

            if dom.isActive():
                try:
                    dom.destroy()
                except libvirt.libvirtError as exc:
                    if exc.get_error_code() != libvirt.VIR_ERR_OPERATION_INVALID:
                        raise

        # Volume before undefine: if this fails the domain stays defined in
        # 'deleting', ownership intact, and reconciliation retries.
        if volume:
            self._delete_volume(conn, volume)

        if dom is not None:
            self._undefine(dom)

    def _delete_volume(self, conn: libvirt.virConnect, volume: str) -> None:
        try:
            storage = self._pool(conn)
            storage.storageVolLookupByName(volume).delete(0)
        except libvirt.libvirtError as exc:
            if exc.get_error_code() in (
                libvirt.VIR_ERR_NO_STORAGE_VOL,
                libvirt.VIR_ERR_NO_STORAGE_POOL,
            ):
                return
            raise

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
                uuids = [dom.UUIDString() for dom, _ in self._managed(conn)]
                for uuid in uuids:
                    self._delete(conn, uuid)
                return len(uuids)

            return self.cm.call(run)

    # -- orphans -----------------------------------------------------------

    def unclaimed_volumes(self) -> set[str]:
        def run(conn: libvirt.virConnect) -> set[str]:
            claimed = {m.volume for _, m in self._managed(conn)}
            return poolmod.unclaimed_volumes(
                conn, self._pool(conn), self.cfg.vm.name_prefix, claimed
            )

        return self.cm.read(run)

    def delete_volume(self, volume: str) -> None:
        if not names.volume_pattern(self.cfg.vm.name_prefix).match(volume):
            raise ValueError(f"{volume!r} is not an overlay this deployment owns")
        with self.lock:
            self.cm.call(lambda conn: self._delete_volume(conn, volume))


def with_observations(
    node: Node,
    *,
    service: str | None = None,
    progress: str | None = None,
    log_text: str | None = None,
) -> Node:
    """Attach in-memory observations to a node for rendering."""
    return replace(
        node,
        service=service if service is not None else node.service,
        progress=progress if progress is not None else node.progress,
        log=log_text if log_text is not None else node.log,
    )
