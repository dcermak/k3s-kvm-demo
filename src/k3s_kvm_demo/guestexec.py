"""Guest command execution. Launch and reaping calls are never retried.

The guest agent has no kill primitive. Callers needing a guest-side deadline
must launch their command under a timeout; ``run`` bounds only the host wait.
"""

from __future__ import annotations

import base64
import json
import logging
import re
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


def _missing_pid(exc: libvirt.libvirtError) -> bool:
    """Recognize a definitive QGA rejection, not a generic transport failure."""
    message = str(exc)
    return bool(
        exc.get_error_code() == libvirt.VIR_ERR_INTERNAL_ERROR
        and "guest agent command failed:" in message.lower()
        and re.search(
            r"\bPID(?:\s+['\"]?\d+['\"]?)?\s+(?:not found|does not exist)\b",
            message,
            re.IGNORECASE,
        )
    )


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
    """Guest-agent operations used by observation and maintenance."""

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
            try:
                return libvirt_qemu.qemuAgentCommand(dom, request, self._timeout_s, 0)
            except libvirt.libvirtError as exc:
                # libvirt may turn a JSON QGA error into VIR_ERR_INTERNAL_ERROR.
                # Only an explicit missing pid permits abandoning this status job.
                if payload.get("execute") == "guest-exec-status" and _missing_pid(exc):
                    raise AgentError(str(exc)) from exc
                raise

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
    deadline = now() + timeout_s
    if should_stop():
        raise ExecTimeout("cancelled before starting the guest process")
    if now() >= deadline:
        raise ExecTimeout("guest process budget expired before launch")
    pid = agent.start(path, args, input_data=input_data)
    while True:
        if should_stop():
            raise ExecTimeout("cancelled while waiting for the guest process")
        if now() >= deadline:
            raise ExecTimeout(f"guest process {pid} did not finish within {timeout_s:.0f}s")
        result = agent.poll(pid)
        if should_stop():
            raise ExecTimeout("cancelled while waiting for the guest process")
        if now() >= deadline:
            raise ExecTimeout(f"guest process {pid} did not finish within {timeout_s:.0f}s")
        if result is not None:
            return result
        sleep(min(interval_s, max(0.0, deadline - now())))
