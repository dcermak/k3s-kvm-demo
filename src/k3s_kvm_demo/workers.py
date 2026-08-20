"""A small pool of daemon worker threads.

``ThreadPoolExecutor`` cannot give a bounded shutdown: its workers are
non-daemon and the interpreter joins them at exit, so a process that calls
``shutdown(wait=False, cancel_futures=True)`` while a five second task is
running still takes five seconds to die.  Daemon threads exit immediately,
which is what lets :meth:`WorkerPool.shutdown` honour a real deadline.

Abrupt death is safe here because every durable fact lives in libvirt metadata
and every write is guarded by a generation check.
"""

from __future__ import annotations

import contextlib
import logging
import queue
import threading
import time
from collections.abc import Callable

log = logging.getLogger(__name__)

_STOP = object()


class QueueFull(Exception):
    """The work queue is at capacity."""


class NotAccepting(Exception):
    """The pool is shutting down."""


class WorkerPool:
    def __init__(self, size: int = 2, queue_size: int = 4, name: str = "provision") -> None:
        self._queue: queue.Queue = queue.Queue(maxsize=queue_size)
        self._threads = [
            threading.Thread(target=self._loop, name=f"{name}-{i}", daemon=True)
            for i in range(size)
        ]
        self._running = 0
        self._state_lock = threading.Lock()
        self._accepting = False
        self.stop_event = threading.Event()

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        self._accepting = True
        for thread in self._threads:
            thread.start()

    def shutdown(self, grace_s: float) -> bool:
        """Stop accepting, ask workers to finish, and wait at most *grace_s*.

        Returns True when every worker finished.  False means a straggler is
        still inside a call, and the caller must not close the libvirt
        connection underneath it.
        """
        self._accepting = False
        self.stop_event.set()
        self._drain()
        for _ in self._threads:
            # Drained just above, so this cannot realistically be full.
            with contextlib.suppress(queue.Full):
                self._queue.put_nowait(_STOP)

        deadline = time.monotonic() + grace_s
        stragglers = []
        for thread in self._threads:
            thread.join(max(0.0, deadline - time.monotonic()))
            if thread.is_alive():
                stragglers.append(thread.name)
        if stragglers:
            log.warning(
                "worker(s) %s still running after %.0fs grace; exiting anyway",
                ", ".join(stragglers),
                grace_s,
            )
            return False
        return True

    def _drain(self) -> None:
        while True:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                return
            self._queue.task_done()

    # -- submission --------------------------------------------------------

    def submit(self, fn: Callable[[], None]) -> None:
        if not self._accepting:
            raise NotAccepting("the worker pool is shutting down")
        try:
            self._queue.put_nowait(fn)
        except queue.Full:
            raise QueueFull(
                "too many nodes are being provisioned at once; wait for one to finish"
            ) from None

    @property
    def load(self) -> int:
        """Queued plus currently executing tasks."""
        with self._state_lock:
            return self._queue.qsize() + self._running

    # -- worker ------------------------------------------------------------

    def _loop(self) -> None:
        while True:
            task = self._queue.get()
            try:
                if task is _STOP:
                    return
                with self._state_lock:
                    self._running += 1
                try:
                    task()
                except Exception:
                    log.exception("provisioning task failed")
                finally:
                    with self._state_lock:
                        self._running -= 1
            finally:
                self._queue.task_done()
