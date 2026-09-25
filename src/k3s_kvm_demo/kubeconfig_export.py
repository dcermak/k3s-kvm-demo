"""Keep a private kubeconfig file current while the dashboard runs."""

from __future__ import annotations

import logging
import os
import tempfile
import threading
import time
from pathlib import Path

from . import cluster, guestexec
from .config import Config
from .libvirtctl import NodeManager

log = logging.getLogger(__name__)


class KubeconfigExporter:
    def __init__(self, manager: NodeManager, cfg: Config) -> None:
        self.manager = manager
        self.cfg = cfg
        self.path = cfg.kubeconfig_export.path
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._last_error: str | None = None

    def start(self) -> None:
        with self._lock:
            if self._thread is not None or self._stop.is_set():
                return
            self.path.unlink(missing_ok=True)
            self._thread = threading.Thread(
                target=self._loop, name="kubeconfig-export", daemon=True,
            )
            self._thread.start()

    def shutdown(self) -> bool:
        with self._lock:
            self._stop.set()
            thread = self._thread
            if thread is not None:
                self._remove()
        if thread is not None:
            thread.join(self.cfg.maintenance.shutdown_grace_s)
        return thread is None or not thread.is_alive()

    def _loop(self) -> None:
        while not self._stop.is_set():
            started = time.monotonic()
            self.refresh()
            elapsed = time.monotonic() - started
            self._stop.wait(max(0, self.cfg.kubeconfig_export.interval_s - elapsed))

    def refresh(self) -> None:
        if self._stop.is_set():
            return
        try:
            text = cluster.export_kubeconfig(
                self.manager.list_nodes(),
                lambda uuid: guestexec.QemuAgent(
                    self.manager.cm, uuid, timeout_s=self.cfg.observation.qga_timeout_s
                ),
                poll_interval_s=0.1,
            )
            with self._lock:
                if self._stop.is_set():
                    return
                self._write(text)
                self._last_error = None
        except Exception as exc:  # noqa: BLE001 - retry without a credential-bearing traceback
            # Raw guest-agent/libvirt errors can contain credential output.
            reason = (
                str(exc) if isinstance(exc, (cluster.KubeconfigExportError, OSError))
                else type(exc).__name__
            )
            with self._lock:
                if self._stop.is_set():
                    return
                if reason != self._last_error:
                    log.warning("kubeconfig export unavailable: %s", reason)
                    self._last_error = reason
                self._remove()

    def _write(self, text: str) -> None:
        content = text.encode("utf-8")
        if self.path.exists() and self.path.read_bytes() == content:
            self.path.chmod(0o600)
            return
        fd, temporary = tempfile.mkstemp(prefix=f".{self.path.name}-", dir=self.path.parent)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(content)
            os.replace(temporary, self.path)
        finally:
            Path(temporary).unlink(missing_ok=True)

    def _remove(self) -> None:
        try:
            self.path.unlink(missing_ok=True)
        except OSError as exc:
            log.warning("could not remove kubeconfig %s: %s", self.path, exc)
