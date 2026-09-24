"""Singleton process lock and a leased libvirt connection.

Two problems this solves.

*Reconnecting safely.*  A libvirt connection must never be closed while another
thread is inside a call on it.  Callers therefore take a short-lived *lease*;
reconnect waits for outstanding leases to drain and, if they do not drain in
time, abandons the old connection instead of closing it.  A leaked socket for
the remaining life of the process is far cheaper than a use-after-free.

*Not serialising on the connection.*  The mutex here guards only the lease
bookkeeping, never the libvirt call itself, so a ten-second guest-agent call
cannot block the two-second poller.
"""

from __future__ import annotations

import errno
import fcntl
import logging
import os
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import TypeVar

import libvirt

log = logging.getLogger(__name__)

T = TypeVar("T")

#: Errors that mean "this connection is gone", as opposed to "this request was
#: bad".  Only these trigger a reconnect.
RECONNECT_CODES = frozenset(
    {
        libvirt.VIR_ERR_SYSTEM_ERROR,
        libvirt.VIR_ERR_INVALID_CONN,
        libvirt.VIR_ERR_NO_CONNECT,
    }
)


def is_connection_lost(exc: libvirt.libvirtError) -> bool:
    return exc.get_error_code() in RECONNECT_CODES


class AlreadyRunning(Exception):
    """Another instance holds the singleton lock."""


def default_lock_path() -> Path:
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if runtime:
        return Path(runtime) / "k3s-kvm-demo.lock"
    return Path(f"/tmp/k3s-kvm-demo-{os.getuid()}.lock")


class SingletonLock:
    """Whole-process advisory lock.

    The job table, provisioning generations and reconciliation are per-process,
    so a second instance would corrupt them.  Checking an environment variable
    would not stop a stray manual launch; an ``flock`` held for the process
    lifetime does.
    """

    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path) if path is not None else default_lock_path()
        self._fd: int | None = None

    def acquire(self) -> None:
        if self._fd is not None:
            return
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(fd)
            if exc.errno in (errno.EACCES, errno.EAGAIN):
                raise AlreadyRunning(
                    f"another k3s-kvm-demo instance holds {self.path}. "
                    "Only one process may manage the demo cluster."
                ) from exc
            raise
        os.truncate(fd, 0)
        os.write(fd, f"{os.getpid()}\n".encode())
        self._fd = fd

    def release(self) -> None:
        if self._fd is None:
            return
        try:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
        finally:
            os.close(self._fd)
            self._fd = None

    def __enter__(self) -> SingletonLock:
        self.acquire()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.release()


class ConnectionManager:
    """Hands out refcounted leases on a libvirt connection."""

    def __init__(
        self,
        uri: str,
        *,
        opener: Callable[[str], libvirt.virConnect] = libvirt.open,
        drain_timeout_s: float = 5.0,
    ) -> None:
        self.uri = uri
        self._opener = opener
        self._drain_timeout_s = drain_timeout_s
        self._cond = threading.Condition(threading.Lock())
        self._conn: libvirt.virConnect | None = None
        self._epoch = 0
        self._refs = 0
        self._reconnecting = False
        self._closed = False
        self._held = threading.local()

    # -- lifecycle ---------------------------------------------------------

    def open(self) -> libvirt.virConnect:
        with self._cond:
            if self._conn is None:
                self._conn = self._opener(self.uri)
            return self._conn

    def close(self) -> bool:
        """Close the connection unless a lease is outstanding.

        Returns True if it was actually closed.  During shutdown a straggling
        worker means we deliberately leak rather than close underneath it.
        """
        with self._cond:
            self._closed = True
            if self._refs > 0:
                log.warning(
                    "leaving libvirt connection open: %d call(s) still in flight", self._refs
                )
                return False
            conn, self._conn = self._conn, None
        if conn is not None:
            try:
                conn.close()
            except libvirt.libvirtError:
                log.debug("error closing libvirt connection", exc_info=True)
        return True

    @property
    def epoch(self) -> int:
        with self._cond:
            return self._epoch

    # -- leasing -----------------------------------------------------------

    @contextmanager
    def lease(self) -> Iterator[tuple[libvirt.virConnect, int]]:
        with self._cond:
            if self._closed:
                raise RuntimeError("connection manager is closed")
            while self._reconnecting:
                self._cond.wait()
            if self._conn is None:
                self._conn = self._opener(self.uri)
            conn, epoch = self._conn, self._epoch
            self._refs += 1
            self._held.depth = getattr(self._held, "depth", 0) + 1
        try:
            yield conn, epoch
        finally:
            with self._cond:
                self._refs -= 1
                self._held.depth -= 1
                self._cond.notify_all()

    def reconnect(self, stale_epoch: int) -> None:
        """Replace the connection, unless someone already replaced *stale_epoch*."""
        if getattr(self._held, "depth", 0) > 0:
            raise RuntimeError("reconnect() called while this thread holds a lease")

        with self._cond:
            if self._closed or self._epoch != stale_epoch:
                return
            self._reconnecting = True
            deadline = time.monotonic() + self._drain_timeout_s
            while self._refs > 0:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._cond.wait(timeout=remaining)
            abandoned = self._refs > 0
            old = self._conn
            try:
                self._conn = self._opener(self.uri)
                self._epoch += 1
            finally:
                self._reconnecting = False
                self._cond.notify_all()

        if old is None:
            return
        if abandoned:
            log.warning(
                "abandoning previous libvirt connection: calls still in flight at reconnect"
            )
            return
        try:
            old.close()
        except libvirt.libvirtError:
            log.debug("error closing replaced libvirt connection", exc_info=True)

    # -- call helpers ------------------------------------------------------

    def read(self, fn: Callable[[libvirt.virConnect], T]) -> T:
        """Run a read-only operation, retrying once across a reconnect."""
        epoch: int | None = None
        try:
            with self.lease() as (conn, current):
                epoch = current
                return fn(conn)
        except libvirt.libvirtError as exc:
            if epoch is None or not is_connection_lost(exc):
                raise
            log.warning("libvirt connection lost during read, reconnecting: %s", exc)

        self.reconnect(epoch)
        with self.lease() as (conn, _):
            return fn(conn)

    def call(self, fn: Callable[[libvirt.virConnect], T]) -> T:
        """Run a mutating operation.  Never retried.

        Retrying an ambiguous mutation could duplicate it, so a lost connection
        surfaces as an error and reconciliation re-derives the truth.  The next
        read repairs the connection.
        """
        with self.lease() as (conn, _):
            return fn(conn)
