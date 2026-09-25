"""HTTP surface.

Every mutating route re-renders the same node grid, so there is exactly one
render path and no out-of-band swaps to keep in step.

The API creates and destroys VMs and runs root commands inside guests with no
authentication, so it is loopback-only (enforced in ``config``) and every
unsafe method is gated on ``Host``/``Origin``/``Sec-Fetch-Site``.  Binding to
loopback alone would not stop a page on another origin POSTing here.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

import libvirt
from fastapi import Depends, FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import cluster, guestexec, libvirtctl, meta, pool as poolmod, seed, stats
from .config import Config, validate_export_path
from .conn import ConnectionManager
from .kubeconfig_export import KubeconfigExporter
from .libvirtctl import DeployRefused, NodeManager, NodeNotFound
from .observer import Observer

log = logging.getLogger(__name__)

HERE = Path(__file__).parent
TEMPLATES = Jinja2Templates(directory=str(HERE / "templates"))

FLASH_TTL_S = 12.0
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


@dataclass
class Flash:
    level: str
    text: str
    expires_at: float
    id: str = field(default_factory=lambda: uuid4().hex)


class Flashes:
    """Short-lived banner messages.

    The grid is re-polled every couple of seconds, so a message rendered once
    would vanish before anyone could read it; these persist for a few seconds
    across polls instead.
    """

    def __init__(self, ttl_s: float = FLASH_TTL_S, clock: Callable[[], float] = time.monotonic):
        self._ttl = ttl_s
        self._clock = clock
        self._items: list[Flash] = []

    def add(self, level: str, text: str) -> None:
        self._items.append(Flash(level, text, self._clock() + self._ttl))

    def current(self) -> list[Flash]:
        now = self._clock()
        self._items = [item for item in self._items if item.expires_at > now]
        return list(self._items)


@dataclass
class AppState:
    cfg: Config
    cm: ConnectionManager
    manager: NodeManager
    observer: Observer
    exporter: KubeconfigExporter
    flashes: Flashes = field(default_factory=Flashes)

    def startup(self) -> None:
        validate_export_path(self.cfg)
        self.cm.open()
        self.manager.check_compatible()
        if not self.cfg.libvirt.is_test_driver:
            self.manager.require_owned_pool()
        self.observer.start()
        self.exporter.start()

    def shutdown(self) -> None:
        export_drained = self.exporter.shutdown()
        drained = self.observer.shutdown()
        drained = drained and export_drained
        if drained:
            self.cm.close()
        else:
            # A worker is still inside a libvirt or guest-agent call; closing
            # the connection under it would be a use-after-free. Daemon threads
            # mean the process still exits promptly.
            log.warning("shutting down with work in flight; leaving the connection open")

    def snapshot(self) -> list[libvirtctl.Node]:
        nodes = self.manager.list_nodes()
        nodes = self.observer.decorate(nodes)
        return stats.enrich(self.cm, nodes)


def build_state(cfg: Config) -> AppState:
    validate_export_path(cfg)
    cm = ConnectionManager(cfg.libvirt.uri)
    manager = NodeManager(cm, cfg)
    return AppState(
        cfg=cfg,
        cm=cm,
        manager=manager,
        observer=Observer(manager, cfg),
        exporter=KubeconfigExporter(manager, cfg),
    )


def get_state(request: Request) -> AppState:
    return request.app.state.k3s


def _host_allowed(cfg: Config, request: Request) -> bool:
    host = request.headers.get("host")
    if host is None or host not in cfg.server.allowed_hosts:
        return False
    origin = request.headers.get("origin")
    if origin is not None:
        parsed = urlsplit(origin)
        if parsed.netloc not in cfg.server.allowed_hosts:
            return False
    fetch_site = request.headers.get("sec-fetch-site")
    return fetch_site in (None, "same-origin", "none")


def create_app(cfg: Config, state_factory: Callable[[], AppState] | None = None) -> FastAPI:
    factory = state_factory or (lambda: build_state(cfg))

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        state = factory()
        app.state.k3s = state
        try:
            state.startup()
            yield
        finally:
            state.shutdown()

    app = FastAPI(title="k3s KVM demo", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")

    @app.exception_handler(meta.CompatibilityError)
    async def incompatible(request: Request, exc: meta.CompatibilityError):
        return PlainTextResponse(str(exc), status_code=409)

    @app.middleware("http")
    async def guard_unsafe_methods(request: Request, call_next):
        if request.method not in SAFE_METHODS and not _host_allowed(cfg, request):
            return PlainTextResponse("cross-origin request rejected", status_code=403)
        return await call_next(request)

    def render(
        request: Request,
        state: AppState,
        name: str = "_grid.html",
        nodes: list[libvirtctl.Node] | None = None,
    ) -> HTMLResponse:
        """The one render path.  *nodes* reuses a listing the caller already has."""
        if nodes is None:
            nodes = state.snapshot()
        return TEMPLATES.TemplateResponse(
            request=request,
            name=name,
            context={
                "nodes": nodes,
                "quorum": cluster.assess_quorum(nodes),
                "flashes": state.flashes.current(),
                "cfg": state.cfg,
                "at_capacity": len(nodes) >= state.cfg.vm.max_nodes,
            },
        )

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request, state: AppState = Depends(get_state)) -> HTMLResponse:
        return render(request, state, "index.html")

    @app.get("/nodes", response_class=HTMLResponse)
    def list_nodes(request: Request, state: AppState = Depends(get_state)) -> HTMLResponse:
        return render(request, state)

    @app.post("/kubeconfig/{action}")
    def kubeconfig(request: Request, action: str, state: AppState = Depends(get_state)):
        headers = {"Cache-Control": "no-store"}
        if action not in {"download", "copy"}:
            return PlainTextResponse("unknown export action", status_code=404, headers=headers)
        text, error = None, None
        try:
            text = cluster.export_kubeconfig(
                state.manager.list_nodes(),
                lambda uuid: guestexec.QemuAgent(
                    state.cm, uuid, timeout_s=state.cfg.observation.qga_timeout_s
                ),
            )
        except cluster.KubeconfigExportError as exc:
            error = str(exc)
        except (libvirt.libvirtError, meta.CompatibilityError, poolmod.PoolError):
            error = "could not discover control plane nodes"
        if action == "download" and error is None:
            return PlainTextResponse(
                text,
                media_type="application/yaml",
                headers={**headers, "Content-Disposition": 'attachment; filename="k3s-demo.yaml"'},
            )
        return TEMPLATES.TemplateResponse(
            request=request,
            name="_kubeconfig.html",
            context={"kubeconfig": text, "error": error, "download": action == "download"},
            status_code=503 if action == "download" else 200,
            headers=headers,
        )

    @app.post("/deploy/{role}", response_class=HTMLResponse)
    def deploy(request: Request, role: str, state: AppState = Depends(get_state)) -> HTMLResponse:
        if role not in meta.ROLES:
            state.flashes.add("error", f"unknown role {role!r}")
            return render(request, state)
        try:
            node = state.manager.create(role)
        except (DeployRefused, poolmod.PoolError, seed.SeedError) as exc:
            state.flashes.add("error", str(exc))
            return render(request, state)
        except libvirt.libvirtError as exc:
            log.exception("deploy failed")
            state.flashes.add("error", f"libvirt refused to create the node: {exc}")
            return render(request, state)

        label = "control plane node" if role == meta.ROLE_SERVER else "node"
        state.flashes.add("info", f"deploying {label} {node.short_name}")
        return render(request, state)

    @app.post("/nodes/{name}/kill", response_class=HTMLResponse)
    def kill(request: Request, name: str, state: AppState = Depends(get_state)):
        try:
            node = state.manager.get(name)
        except NodeNotFound:
            return PlainTextResponse(f"no node named {name}", status_code=404)
        try:
            state.manager.delete(node.uuid)
        except NodeNotFound:
            return PlainTextResponse(f"no node named {name}", status_code=404)
        except (libvirt.libvirtError, poolmod.PoolError) as exc:
            log.exception("killing %s failed", name)
            state.flashes.add("error", f"could not fully remove {node.short_name}: {exc}")
        else:
            state.flashes.add("info", f"killed {node.short_name}")
        return render(request, state)

    @app.post("/reset", response_class=HTMLResponse)
    def reset(request: Request, state: AppState = Depends(get_state)) -> HTMLResponse:
        try:
            removed = state.manager.reset()
        except (libvirt.libvirtError, poolmod.PoolError) as exc:
            log.exception("reset failed")
            state.flashes.add("error", f"reset did not complete: {exc}")
        else:
            state.flashes.add("info", f"removed {removed} node(s)")
        return render(request, state)

    @app.post("/prune-nodes", response_class=HTMLResponse)
    def prune(request: Request, state: AppState = Depends(get_state)) -> HTMLResponse:
        # Pruning touches Kubernetes objects, not libvirt, so this listing is
        # still accurate afterwards and is reused for the response.
        nodes = state.snapshot()
        try:
            result = cluster.prune_nodes(
                state.cfg,
                nodes,
                lambda uuid: guestexec.QemuAgent(
                    state.cm, uuid, timeout_s=state.cfg.observation.qga_timeout_s
                ),
                manager=state.manager,
            )
        except (cluster.NoServerAvailable, guestexec.AgentUnavailable, guestexec.AgentError) as exc:
            state.flashes.add("error", str(exc))
            return render(request, state, nodes=nodes)
        except guestexec.ExecTimeout as exc:
            state.flashes.add("error", f"kubectl did not answer in time: {exc}")
            return render(request, state, nodes=nodes)

        state.flashes.add("info" if not result.failed else "error", result.summary())
        for name, reason in result.failed:
            state.flashes.add("error", f"{name}: {reason}")
        return render(request, state, nodes=nodes)

    @app.get("/healthz")
    def healthz(state: AppState = Depends(get_state)) -> JSONResponse:
        payload: dict[str, object] = {
            "uri": state.cfg.libvirt.uri,
            "connection_epoch": state.cm.epoch,
            "observation_stale_after_s": state.cfg.observation.stale_after_s,
        }
        try:
            # Metadata only: the counts here do not need power state or an
            # address for every node.
            states = state.manager.states()
            payload["nodes"] = len(states)
            payload["states"] = sorted(set(states))
        except libvirt.libvirtError as exc:
            payload["error"] = str(exc)
            return JSONResponse(payload, status_code=503)

        try:
            status = state.manager.pool_status()
            payload["pool_marker"] = status.marker
            payload["unclassified_volumes"] = list(status.unknown)
            payload["orphan_candidates"] = sorted(state.manager.unclaimed_volumes(status))
        except (libvirt.libvirtError, poolmod.PoolError) as exc:
            payload["pool_error"] = str(exc)
        return JSONResponse(payload)

    return app
