"""The provisioning state machine and reconciliation loop.

Durable state lives in libvirt metadata; this module owns only what is
genuinely ephemeral — progress text, the captured firstboot log, and the
cancellation flag for a running job.  After a restart those are gone, and
:meth:`Provisioner.reconcile` rebuilds the picture from metadata plus what the
guest agent reports, rather than assuming anything.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from . import guestexec, k3sconf, libvirtctl, meta
from .config import Config
from .guestexec import Agent
from .libvirtctl import Action, DeployRefused, NodeManager
from .workers import WorkerPool

log = logging.getLogger(__name__)

AgentFactory = Callable[[str, int], Agent]

RECONCILE_INTERVAL_S = 15.0


def redact(text: str, secret: str) -> str:
    """Blank a secret out of captured guest output."""
    if not secret:
        return text
    return text.replace(secret, "***")


def tail(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return "…" + text[-limit:]


@dataclass
class Job:
    """In-flight provisioning work for one node."""

    uuid: str
    generation: int
    progress: str = "queued"
    log: str | None = None
    cancel: threading.Event = field(default_factory=threading.Event)


@dataclass
class Observation:
    """What we last managed to observe about a node, outside libvirt."""

    service: str = libvirtctl.SERVICE_UNKNOWN
    progress: str | None = None
    log: str | None = None
    probe_failures: int = 0


class Provisioner:
    def __init__(
        self,
        manager: NodeManager,
        cfg: Config,
        *,
        pool: WorkerPool | None = None,
        agent_factory: AgentFactory | None = None,
        maintenance: Callable[[], None] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        now: Callable[[], float] = time.monotonic,
        wait: Callable[[threading.Event, float], bool] | None = None,
    ) -> None:
        self.manager = manager
        self.cfg = cfg
        self.pool = pool or WorkerPool()
        self._agent_factory = agent_factory or self._default_agent
        self._maintenance = maintenance
        self._sleep = sleep
        self._now = now
        # Injectable so backoff waits do not slow the test suite down, and so
        # a kill still interrupts them in production.
        self._wait = wait or (lambda event, timeout: event.wait(timeout))

        self._lock = threading.Lock()
        self._jobs: dict[str, Job] = {}
        self._observations: dict[str, Observation] = {}
        self._reconciler: threading.Thread | None = None
        self._stop = threading.Event()

    def _default_agent(self, uuid: str, timeout_s: int) -> Agent:
        return guestexec.QemuAgent(self.manager.cm, uuid, timeout_s=timeout_s)

    def agent_for(self, uuid: str) -> Agent:
        """A guest agent for *uuid*, honouring the configured QGA timeout."""
        return self._agent_factory(uuid, self.cfg.firstboot.qga_timeout_s)

    # -- lifecycle ---------------------------------------------------------

    def start(self, *, reconcile_interval_s: float = RECONCILE_INTERVAL_S) -> None:
        self.pool.start()
        self.reconcile()
        self._reconciler = threading.Thread(
            target=self._reconcile_loop,
            args=(reconcile_interval_s,),
            name="reconcile",
            daemon=True,
        )
        self._reconciler.start()

    def shutdown(self) -> bool:
        self._stop.set()
        with self._lock:
            for job in self._jobs.values():
                job.cancel.set()
        return self.pool.shutdown(self.cfg.maintenance.shutdown_grace_s)

    def _reconcile_loop(self, interval_s: float) -> None:
        while not self._stop.wait(interval_s):
            try:
                self.reconcile()
            except Exception:
                log.exception("reconciliation pass failed")

    # -- observations ------------------------------------------------------

    def decorate(self, nodes: list[libvirtctl.Node]) -> list[libvirtctl.Node]:
        """Attach ephemeral progress/log/service to freshly read nodes."""
        with self._lock:
            live = {node.uuid for node in nodes}
            for stale in set(self._observations) - live:
                del self._observations[stale]
            snapshot = {
                uuid: (obs.service, obs.progress, obs.log)
                for uuid, obs in self._observations.items()
            }
            jobs = {uuid: job.progress for uuid, job in self._jobs.items()}

        decorated = []
        for node in nodes:
            service, progress, log_text = snapshot.get(
                node.uuid, (libvirtctl.SERVICE_UNKNOWN, None, None)
            )
            decorated.append(
                libvirtctl.with_observations(
                    node,
                    service=service,
                    progress=jobs.get(node.uuid, progress),
                    log_text=log_text,
                )
            )
        return decorated

    def _observe(self, uuid: str, **changes: object) -> None:
        with self._lock:
            obs = self._observations.setdefault(uuid, Observation())
            for key, value in changes.items():
                setattr(obs, key, value)

    def _set_progress(self, uuid: str, text: str) -> None:
        with self._lock:
            job = self._jobs.get(uuid)
            if job is not None:
                job.progress = text
        self._observe(uuid, progress=text)

    # -- job bookkeeping ---------------------------------------------------

    def _claim(self, uuid: str, generation: int) -> Job | None:
        """Register a job unless one is already running for this node."""
        with self._lock:
            if uuid in self._jobs:
                return None
            job = Job(uuid=uuid, generation=generation)
            self._jobs[uuid] = job
            return job

    def _release(self, uuid: str) -> None:
        with self._lock:
            self._jobs.pop(uuid, None)

    def cancel(self, uuid: str) -> None:
        with self._lock:
            job = self._jobs.get(uuid)
        if job is not None:
            job.cancel.set()

    @property
    def in_flight(self) -> int:
        with self._lock:
            return len(self._jobs)

    # -- entry points ------------------------------------------------------

    def submit(self, node: libvirtctl.Node, server_url: str | None) -> None:
        """Queue provisioning for a newly created node."""
        job = self._claim(node.uuid, node.generation)
        if job is None:
            return
        try:
            self.pool.submit(
                lambda: self._run(job, node.name, node.role, node.bootstrap, server_url)
            )
        except Exception:
            self._release(node.uuid)
            raise

    def reconcile(self) -> None:
        """Bring every managed node to a defined outcome."""
        nodes = self.manager.list_nodes()
        for node in nodes:
            decision = libvirtctl.decide(node.state, node.power, known_state=node.known_state)
            try:
                self._apply(node, nodes, decision)
            except Exception:
                log.exception("reconciling %s failed", node.name)

        if self._maintenance is not None:
            try:
                self._maintenance()
            except Exception:
                log.exception("maintenance pass failed")

    def _apply(
        self,
        node: libvirtctl.Node,
        nodes: list[libvirtctl.Node],
        decision: libvirtctl.Decision,
    ) -> None:
        if decision.action is Action.NONE:
            return

        if decision.action is Action.FAIL:
            self.manager.update_state(
                node.uuid, node.generation, meta.FAILED, error=decision.reason
            )
            return

        if decision.action is Action.DELETE:
            job = self._claim(node.uuid, node.generation)
            if job is None:
                return
            log.info("completing interrupted %s of %s", node.state, node.name)
            self._submit_or_release(job, lambda: self._finish_delete(job))
            return

        if decision.action is Action.VERIFY:
            job = self._claim(node.uuid, node.generation)
            if job is None:
                return
            self._submit_or_release(job, lambda: self._verify(job))
            return

        # AWAIT_AGENT / CONFIGURE: resume provisioning. Firstboot is
        # idempotent, so re-running it from the top is always safe.
        try:
            bootstrap, server_url = (
                (True, None)
                if node.bootstrap
                else self.manager.plan([n for n in nodes if n.uuid != node.uuid], node.role)
            )
        except DeployRefused as exc:
            self._set_progress(node.uuid, f"waiting to resume: {exc}")
            return

        job = self._claim(node.uuid, node.generation)
        if job is None:
            return
        self._submit_or_release(
            job, lambda: self._run(job, node.name, node.role, bootstrap, server_url)
        )

    def _submit_or_release(self, job: Job, task: Callable[[], None]) -> None:
        try:
            self.pool.submit(task)
        except Exception:
            self._release(job.uuid)
            log.debug("could not queue work for %s", job.uuid, exc_info=True)

    # -- workers -----------------------------------------------------------

    def _finish_delete(self, job: Job) -> None:
        try:
            self.manager.delete(job.uuid)
        except libvirtctl.NodeNotFound:
            pass
        finally:
            self._release(job.uuid)
            with self._lock:
                self._observations.pop(job.uuid, None)

    def _verify(self, job: Job) -> None:
        """Probe the k3s unit once; the result is an observation, not state."""
        try:
            agent = self._agent_factory(job.uuid, self.cfg.firstboot.qga_timeout_s)
            result = self._probe(agent, job)
        except (guestexec.AgentUnavailable, guestexec.AgentError, guestexec.ExecTimeout):
            with self._lock:
                obs = self._observations.setdefault(job.uuid, Observation())
                obs.probe_failures += 1
                failures = obs.probe_failures
                obs.service = libvirtctl.SERVICE_UNKNOWN
                limit = self.cfg.maintenance.service_probe_attempts
                obs.progress = (
                    f"service state unknown — probe gave up after {failures} attempts"
                    if failures >= limit
                    else f"service state unknown — probing ({failures}/{limit})"
                )
        except Exception:
            log.exception("verifying %s failed", job.uuid)
        else:
            self._observe(
                job.uuid,
                service=libvirtctl.SERVICE_ACTIVE if result else libvirtctl.SERVICE_INACTIVE,
                probe_failures=0,
                progress=None,
            )
        finally:
            self._release(job.uuid)

    def _probe(self, agent: Agent, job: Job) -> bool:
        result = guestexec.run(
            agent,
            "/bin/sh",
            ["-c", k3sconf.PROBE_COMMAND],
            timeout_s=self.cfg.firstboot.qga_timeout_s * 3,
            interval_s=1.0,
            sleep=self._sleep,
            now=self._now,
            should_stop=job.cancel.is_set,
        )
        return result.ok

    def _run(
        self,
        job: Job,
        name: str,
        role: str,
        bootstrap: bool,
        server_url: str | None,
    ) -> None:
        try:
            self._provision(job, name, role, bootstrap, server_url)
        except Exception:
            log.exception("provisioning %s failed", name)
            self._fail(job, "internal error while provisioning; see the service log")
        finally:
            self._release(job.uuid)

    def _fail(self, job: Job, reason: str) -> None:
        self.manager.update_state(job.uuid, job.generation, meta.FAILED, error=reason)
        self._observe(job.uuid, progress=None)

    def _provision(
        self,
        job: Job,
        name: str,
        role: str,
        bootstrap: bool,
        server_url: str | None,
    ) -> None:
        cfg = self.cfg
        agent = self._agent_factory(job.uuid, cfg.firstboot.qga_timeout_s)

        self._set_progress(job.uuid, "waiting for the guest agent")
        reachable = guestexec.wait_for_agent(
            agent,
            timeout_s=cfg.firstboot.boot_timeout_s,
            sleep=self._sleep,
            now=self._now,
            should_stop=job.cancel.is_set,
        )
        if job.cancel.is_set():
            return
        if not reachable:
            self._fail(
                job,
                f"the guest agent did not respond within {cfg.firstboot.boot_timeout_s}s; "
                "is qemu-guest-agent enabled in the base image?",
            )
            return

        if not self.manager.update_state(job.uuid, job.generation, meta.CONFIGURING):
            return
        self._set_progress(job.uuid, "running firstboot")

        script = k3sconf.render_firstboot(
            cfg.firstboot.script,
            node_name=name,
            role=role,
            token=cfg.cluster.token,
            is_bootstrap=bootstrap,
            server_url=server_url,
            tls_san=cfg.cluster.tls_san,
            service_wait_s=max(30, cfg.firstboot.exec_timeout_s - 60),
        )
        marker = f"{k3sconf.PROCESS_MARKER}-{job.uuid.replace('-', '')}"
        deadline = self._now() + cfg.firstboot.exec_timeout_s

        for attempt, backoff in enumerate(self._backoff(), start=1):
            if job.cancel.is_set():
                return
            remaining = deadline - self._now()
            if remaining <= 0:
                break

            try:
                result = guestexec.run(
                    agent,
                    "/bin/sh",
                    ["-s", marker],
                    input_data=script,
                    timeout_s=remaining,
                    sleep=self._sleep,
                    now=self._now,
                    should_stop=job.cancel.is_set,
                )
            except guestexec.ExecTimeout:
                if job.cancel.is_set():
                    return
                self._kill_marker(agent, marker)
                self._fail(
                    job,
                    f"firstboot did not finish within {cfg.firstboot.exec_timeout_s}s; "
                    "the guest process may still be running — kill the node",
                )
                return
            except guestexec.AgentUnavailable as exc:
                self._fail(job, f"the guest agent stopped responding: {exc}")
                return

            self._record_log(job, result)

            if result.ok:
                self.manager.update_state(job.uuid, job.generation, meta.CONFIGURED)
                self._observe(job.uuid, progress=None, service=libvirtctl.SERVICE_ACTIVE)
                return

            if result.exitcode != k3sconf.EX_TEMPFAIL:
                self._fail(job, f"firstboot {result.describe()}: {self._reason(result)}")
                return

            # Exit 75 means a previous run still holds the guest-side lock.
            # That process has already exited from our point of view, so the
            # only way forward is to re-execute after a pause — but first check
            # whether that other run in fact finished the job.
            self._set_progress(
                job.uuid, f"another firstboot run is in progress (attempt {attempt})"
            )
            try:
                if self._probe(agent, job):
                    self.manager.update_state(job.uuid, job.generation, meta.CONFIGURED)
                    self._observe(job.uuid, progress=None, service=libvirtctl.SERVICE_ACTIVE)
                    return
            except (guestexec.AgentUnavailable, guestexec.AgentError, guestexec.ExecTimeout):
                log.debug("probe during backoff failed for %s", name, exc_info=True)

            if self._wait(job.cancel, min(backoff, max(0.0, deadline - self._now()))):
                return

        self._fail(
            job,
            "another firstboot run never completed; the guest-side lock was still held "
            f"after {cfg.firstboot.exec_timeout_s}s",
        )

    def _backoff(self):
        schedule = self.cfg.firstboot.retry_backoff_s
        yield from schedule
        while True:
            yield schedule[-1]

    def _record_log(self, job: Job, result: guestexec.ExecResult) -> None:
        text = "\n".join(part for part in (result.stdout, result.stderr) if part)
        text = tail(redact(text, self.cfg.cluster.token), self.cfg.firstboot.log_tail_bytes)
        with self._lock:
            job.log = text or None
        self._observe(job.uuid, log=text or None)

    def _reason(self, result: guestexec.ExecResult) -> str:
        text = redact(result.stderr or result.stdout, self.cfg.cluster.token).strip()
        last = text.splitlines()[-1] if text else "no output"
        return last

    def _kill_marker(self, agent: Agent, marker: str) -> None:
        """Best effort: the guest agent has no kill primitive of its own."""
        try:
            agent.start("/usr/bin/pkill", ["-f", marker])
        except Exception:
            log.debug("could not signal %s in the guest", marker, exc_info=True)
