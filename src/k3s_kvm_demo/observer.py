"""Observe guest-owned preparation without running provisioning commands."""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from uuid import UUID

from . import guestexec, libvirtctl, meta
from .config import Config
from .guestexec import Agent

log = logging.getLogger(__name__)

AgentFactory = Callable[[str, int], Agent]
GUEST_PATH = "/usr/local/libexec/k3s-demo-guest"
MAX_STATUS_BYTES = 1024
MAX_CALLS_PER_PASS = 8
STATUS_LIFETIME_S = 4
ERROR_CODES = frozenset({"none", "prepare-failed", "k3s-failed"})
SYSTEMD_STATES = frozenset(
    {
        "active",
        "reloading",
        "inactive",
        "failed",
        "activating",
        "deactivating",
        "maintenance",
        "refreshing",
        "unknown",
    }
)


@dataclass(frozen=True)
class GuestStatus:
    protocol: int
    node_uuid: str
    prepared: bool
    started: bool
    prepare_state: str
    k3s_state: str
    error_code: str


def parse_guest_status(text: str) -> GuestStatus:
    """Parse the bounded, seven-line protocol; reject unknown or duplicate fields."""
    if not isinstance(text, str) or len(text) > MAX_STATUS_BYTES or not text.isascii():
        raise ValueError("invalid guest status size or encoding")
    fields = {}
    for line in text.removesuffix("\n").split("\n"):
        key, separator, value = line.partition("=")
        if not separator or key in fields:
            raise ValueError("invalid or duplicate guest status field")
        fields[key] = value
    if fields.keys() != GuestStatus.__dataclass_fields__.keys():
        raise ValueError("unexpected guest status fields")
    if fields["protocol"] != "1" or any(
        fields[k] not in {"0", "1"} for k in ("prepared", "started")
    ):
        raise ValueError("invalid guest status protocol or flag")
    if any(fields[k] not in SYSTEMD_STATES for k in ("prepare_state", "k3s_state")):
        raise ValueError("invalid systemd state")
    if fields["error_code"] not in ERROR_CODES:
        raise ValueError("invalid guest error code")
    identity = fields["node_uuid"]
    if identity:
        identity = str(UUID(identity))
    elif fields["prepared"] != "0" or fields["started"] != "0":
        raise ValueError("guest success without an identity")
    return GuestStatus(
        protocol=1,
        node_uuid=identity,
        prepared=fields["prepared"] == "1",
        started=fields["started"] == "1",
        prepare_state=fields["prepare_state"],
        k3s_state=fields["k3s_state"],
        error_code=fields["error_code"],
    )


@dataclass(frozen=True)
class Observation:
    generation: int
    at: float
    status: GuestStatus | None = None
    progress: str | None = None


@dataclass(frozen=True)
class Pending:
    generation: int
    at: float
    agent: Agent
    pid: int | None


class Observer:
    def __init__(
        self,
        manager: libvirtctl.NodeManager,
        cfg: Config,
        *,
        agent_factory: AgentFactory | None = None,
        now: Callable[[], float] = time.monotonic,
    ) -> None:
        self.manager = manager
        self.cfg = cfg
        self._agent_factory = agent_factory or (
            lambda uuid, timeout: guestexec.QemuAgent(manager.cm, uuid, timeout_s=timeout)
        )
        self._now = now
        self._observations: dict[str, Observation] = {}
        self._pending: dict[str, Pending] = {}
        self._cursor = 0
        self._lock = threading.Lock()
        self._reconcile_lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        manager.join_ready = self.join_ready

    def start(self) -> None:
        with self._lock:
            if self._thread is not None or self._stop.is_set():
                return
            self._thread = threading.Thread(target=self._loop, name="observer", daemon=True)
            self._thread.start()

    def shutdown(self) -> bool:
        self._stop.set()
        with self._lock:
            thread = self._thread
        if thread is not None:
            thread.join(self.cfg.maintenance.shutdown_grace_s)
        return thread is None or not thread.is_alive()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.reconcile()
            except Exception:
                log.exception("observation pass failed")
            self._stop.wait(self.cfg.observation.interval_s)

    def _recent(self, node: libvirtctl.Node) -> Observation | None:
        with self._lock:
            observation = self._observations.get(node.uuid)
        if (
            node.running
            and node.state in meta.STATES - meta.INCOMPLETE
            and observation is not None
            and observation.generation == node.generation
            and 0 <= self._now() - observation.at < self.cfg.observation.stale_after_s
        ):
            return observation
        return None

    def join_ready(self, node: libvirtctl.Node) -> bool:
        observation = self._recent(node)
        status = observation.status if observation else None
        return bool(
            status
            and status.node_uuid == str(UUID(node.uuid))
            and status.started
            and status.prepared
            and status.prepare_state == "active"
            and status.error_code == "none"
            and status.k3s_state == "active"
        )

    def decorate(self, nodes: list[libvirtctl.Node]) -> list[libvirtctl.Node]:
        decorated = []
        for node in nodes:
            observation = self._recent(node)
            status = observation.status if observation else None
            service = libvirtctl.SERVICE_UNKNOWN
            if status is not None and status.node_uuid and status.k3s_state != "unknown":
                service = (
                    libvirtctl.SERVICE_ACTIVE
                    if status.k3s_state == "active"
                    else libvirtctl.SERVICE_INACTIVE
                )
            decorated.append(
                replace(
                    node,
                    service=service,
                    progress=observation.progress if observation else None,
                )
            )
        return decorated

    def _record(self, node: libvirtctl.Node, observation: Observation) -> None:
        with self._lock:
            self._observations[node.uuid] = observation

    def reconcile(self) -> None:
        """Scan all nodes, issuing at most eight QGA calls in rotating order."""
        with self._reconcile_lock:
            if self._stop.is_set():
                return
            nodes = self.manager.list_nodes()
            live = {node.uuid for node in nodes}
            with self._lock:
                for uuid in self._observations.keys() - live:
                    del self._observations[uuid]
            for uuid in self._pending.keys() - live:
                del self._pending[uuid]
            if not nodes:
                return
            offset = self._cursor % len(nodes)
            calls = 0
            for index in range(len(nodes)):
                if self._stop.is_set():
                    break
                node = nodes[(offset + index) % len(nodes)]
                if not node.running or node.state not in meta.STATES - meta.INCOMPLETE:
                    self._record(node, Observation(node.generation, self._now()))
                    if node.state in meta.INCOMPLETE:
                        try:
                            self.manager.delete_if_current(node.uuid, node.generation, node.state)
                        except Exception:
                            log.exception("could not clean up incomplete node %s", node.uuid)
                    continue
                if calls >= MAX_CALLS_PER_PASS:
                    continue
                calls += 1
                self._cursor = (offset + index + 1) % len(nodes)
                try:
                    self._observe(node)
                except Exception:
                    self._record(
                        node,
                        Observation(node.generation, self._now(), progress="guest status unknown"),
                    )
                    log.exception("could not observe node %s", node.uuid)

    def _observe(self, node: libvirtctl.Node) -> None:
        pending = self._pending.get(node.uuid)
        at = self._now()
        timeout = min(2, max(1, int(self.cfg.observation.qga_timeout_s)))
        if pending is None:
            agent = self._agent_factory(node.uuid, timeout)
            # A lost launch reply can hide a running command. Wait out its guest
            # timeout before submitting another; known pids are always retained.
            self._pending[node.uuid] = Pending(node.generation, at, agent, None)
            try:
                pid = agent.start(
                    "/usr/bin/timeout", ["--kill-after=1s", "3s", GUEST_PATH, "status"]
                )
            except Exception:
                self._pending[node.uuid] = Pending(node.generation, self._now(), agent, None)
                raise
            self._pending[node.uuid] = Pending(node.generation, at, agent, pid)
            return
        if pending.pid is None:
            if at - pending.at >= STATUS_LIFETIME_S:
                del self._pending[node.uuid]
            return
        try:
            result = pending.agent.poll(pending.pid)
        except guestexec.AgentError:
            del self._pending[node.uuid]
            raise
        if result is None:
            if at - pending.at >= STATUS_LIFETIME_S + timeout:
                self._record(
                    node, Observation(node.generation, at, progress="guest status timed out")
                )
            return
        del self._pending[node.uuid]
        if pending.generation != node.generation:
            return
        if self._now() - pending.at >= self.cfg.observation.stale_after_s:
            raise ValueError("guest status result is stale")
        if not result.ok or result.signal is not None:
            raise ValueError("guest status command failed")
        status = parse_guest_status(result.stdout)
        if status.node_uuid and status.node_uuid != str(UUID(node.uuid)):
            raise ValueError("guest status identity mismatch")
        error = status.error_code if status.error_code != "none" else None
        if status.prepare_state == "failed":
            error = "prepare-failed"
        progress = error
        if not status.node_uuid:
            progress = error or "waiting for guest identity"
        elif not status.prepared:
            progress = error or "waiting for guest preparation"
        elif not status.started:
            progress = error or "waiting for k3s"
        observation = Observation(node.generation, pending.at, status, progress)
        self._record(node, observation)
        if not status.node_uuid or self._now() - pending.at >= self.cfg.observation.stale_after_s:
            return
        state = node.state
        if status.started:
            state = meta.CONFIGURED
        elif node.state != meta.CONFIGURED:
            if error == "prepare-failed":
                state = meta.FAILED
            elif status.prepared:
                state = meta.CONFIGURING
        if state != node.state or error != node.error:
            self.manager.update_state(
                node.uuid, node.generation, state, error=error, expected_states={node.state}
            )
