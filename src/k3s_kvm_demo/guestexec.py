"""Running commands in a guest through the QEMU guest agent.

``provision.py`` depends on the :class:`Agent` protocol rather than on libvirt,
so its state machine is testable without a VM.

Two properties of the agent shape the API.  ``guest-exec-status`` *reaps* the
process once it has exited, so polling is not idempotent and must never be
retried automatically.  And the agent has no kill primitive, which is why the
script is started under a marker that ``pkill -f`` can find.
"""

from __future__ import annotations

import base64
import json
import logging
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Protocol

import libvirt
import libvirt_qemu

from .conn import ConnectionManager

log = logging.getLogger(__name__)

TRUNCATION_NOTE = "…[truncated by guest agent]"

#: The agent is present but not answering (yet).  Retryable while booting.
AGENT_DOWN_CODES = frozenset(
    {
        libvirt.VIR_ERR_AGENT_UNRESPONSIVE,
        libvirt.VIR_ERR_OPERATION_INVALID,
        libvirt.VIR_ERR_OPERATION_TIMEOUT,
    }
)


class AgentUnavailable(Exception):
    """The guest agent did not answer."""


class AgentError(Exception):
    """The guest agent answered, but with an error."""


class ExecTimeout(Exception):
    """A guest command outlived its budget."""


@dataclass(frozen=True, slots=True)
class ExecResult:
    exitcode: int | None
    signal: int | None
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.exitcode == 0

    def describe(self) -> str:
        if self.signal is not None:
            return f"killed by signal {self.signal}"
        return f"exit {self.exitcode}"


class Agent(Protocol):
    """The narrow slice of guest-agent behaviour provisioning needs."""

    def ping(self) -> None:
        """Raise :class:`AgentUnavailable` if the guest agent is not answering."""

    def start(self, path: str, args: Sequence[str], *, input_data: str | None = None) -> int:
        """Launch a process and return its guest pid."""

    def poll(self, pid: int) -> ExecResult | None:
        """Return the result once the process has exited, else None.

        Reaps the process, so a given pid yields a result at most once.
        """


def _decode(payload: str | None, truncated: bool) -> str:
    if not payload:
        return TRUNCATION_NOTE if truncated else ""
    text = base64.b64decode(payload).decode("utf-8", errors="replace")
    return f"{text}{TRUNCATION_NOTE}" if truncated else text


class QemuAgent:
    """:class:`Agent` backed by ``virDomainQemuAgentCommand``."""

    def __init__(self, cm: ConnectionManager, uuid: str, *, timeout_s: int = 10) -> None:
        self._cm = cm
        self._uuid = uuid
        self._timeout_s = timeout_s

    def _command(self, payload: dict, *, retry: bool) -> dict:
        request = json.dumps(payload)

        def run(conn: libvirt.virConnect) -> str:
            dom = conn.lookupByUUIDString(self._uuid)
            return libvirt_qemu.qemuAgentCommand(dom, request, self._timeout_s, 0)

        runner = self._cm.read if retry else self._cm.call
        try:
            raw = runner(run)
        except libvirt.libvirtError as exc:
            if exc.get_error_code() in AGENT_DOWN_CODES:
                raise AgentUnavailable(str(exc)) from exc
            raise
        try:
            response = json.loads(raw)
        except (TypeError, ValueError) as exc:
            raise AgentError(f"guest agent returned non-JSON: {raw!r}") from exc
        if "error" in response:
            raise AgentError(f"guest agent error: {response['error']}")
        return response.get("return") or {}

    def ping(self) -> None:
        self._command({"execute": "guest-ping"}, retry=True)

    def start(self, path: str, args: Sequence[str], *, input_data: str | None = None) -> int:
        arguments: dict[str, object] = {
            "path": path,
            "arg": list(args),
            "capture-output": True,
        }
        if input_data is not None:
            arguments["input-data"] = base64.b64encode(input_data.encode()).decode("ascii")
        result = self._command({"execute": "guest-exec", "arguments": arguments}, retry=False)
        pid = result.get("pid")
        if not isinstance(pid, int):
            raise AgentError(f"guest-exec returned no pid: {result!r}")
        return pid

    def poll(self, pid: int) -> ExecResult | None:
        # Never retried: guest-exec-status reaps, so a repeat after a lost
        # connection would lose the result instead of returning it twice.
        result = self._command(
            {"execute": "guest-exec-status", "arguments": {"pid": pid}}, retry=False
        )
        if not result.get("exited"):
            return None
        return ExecResult(
            exitcode=result.get("exitcode"),
            signal=result.get("signal"),
            stdout=_decode(result.get("out-data"), bool(result.get("out-truncated"))),
            stderr=_decode(result.get("err-data"), bool(result.get("err-truncated"))),
        )


def wait_for_agent(
    agent: Agent,
    *,
    timeout_s: float,
    interval_s: float = 2.0,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], float] = time.monotonic,
    should_stop: Callable[[], bool] = lambda: False,
) -> bool:
    """Poll ``guest-ping`` until it answers.  False on timeout or cancellation."""
    deadline = now() + timeout_s
    while not should_stop():
        try:
            agent.ping()
        except AgentUnavailable:
            pass
        else:
            return True
        if now() >= deadline:
            return False
        sleep(interval_s)
    return False


def run(
    agent: Agent,
    path: str,
    args: Sequence[str],
    *,
    input_data: str | None = None,
    timeout_s: float,
    interval_s: float = 2.0,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], float] = time.monotonic,
    should_stop: Callable[[], bool] = lambda: False,
) -> ExecResult:
    """Start a guest process and wait for it, within *timeout_s*."""
    pid = agent.start(path, args, input_data=input_data)
    deadline = now() + timeout_s
    while True:
        if should_stop():
            raise ExecTimeout("cancelled while waiting for the guest process")
        result = agent.poll(pid)
        if result is not None:
            return result
        if now() >= deadline:
            raise ExecTimeout(f"guest process {pid} did not finish within {timeout_s:.0f}s")
        sleep(interval_s)
