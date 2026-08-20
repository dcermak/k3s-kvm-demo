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

import libvirt
from fastapi import Depends, FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import cluster, guestexec, libvirtctl, meta, pool as poolmod, stats
from .config import Config
from .conn import ConnectionManager, SingletonLock
from .libvirtctl import DeployRefused, NodeManager, NodeNotFound
from .provision import Provisioner
from .workers import NotAccepting, QueueFull

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
class Quorum:
    servers: int
    running: int
    needed: int
    warning: str | None = None


def assess_quorum(nodes: list[libvirtctl.Node]) -> Quorum:
    """Advisory only.

    Derived from the VMs this app manages, which cannot see etcd members left
    behind by earlier kills, so it can understate the real member count.  The
    template says so; nothing is ever refused on the strength of it.
    """
    servers = [node for node in nodes if node.is_server]
    running = [node for node in servers if node.running]
    needed = len(servers) // 2 + 1 if servers else 0

    warning = None
    if servers and len(running) < needed:
        warning = (
            f"{len(running)} of {len(servers)} control plane nodes are running; "
            f"etcd needs {needed} for quorum. The cluster is down until enough return."
        )
    elif len(servers) > 1 and len(servers) % 2 == 0:
        warning = (
            f"{len(servers)} control plane nodes is an even number; etcd tolerates no "
            "more failures than an odd count one lower. Add or remove one."
        )
    return Quorum(servers=len(servers), running=len(running), needed=needed, warning=warning)


@dataclass
class AppState:
    cfg: Config
    cm: ConnectionManager
    manager: NodeManager
    provisioner: Provisioner
    singleton: SingletonLock | None = None
    orphans: poolmod.OrphanTracker | None = None
    flashes: Flashes = field(default_factory=Flashes)

    def startup(self) -> None:
        self.cm.open()
        if not self.cfg.libvirt.is_test_driver:
            self.cm.read(
                lambda conn: poolmod.require_owned(
                    conn.storagePoolLookupByName(self.cfg.libvirt.pool),
                    self.cfg.vm.name_prefix,
                )
            )
        self.provisioner.start()

    def shutdown(self) -> None:
        drained = self.provisioner.shutdown()
        if drained:
            self.cm.close()
        else:
            # A worker is still inside a libvirt or guest-agent call; closing
            # the connection under it would be a use-after-free. Daemon threads
            # mean the process still exits promptly.
            log.warning("shutting down with work in flight; leaving the connection open")
        if self.singleton is not None:
            self.singleton.release()

    def snapshot(self) -> list[libvirtctl.Node]:
        nodes = self.manager.list_nodes()
        nodes = self.provisioner.decorate(nodes)
        return stats.enrich(self.cm, nodes)

    def reap_orphans(self) -> list[str]:
        """Delete overlays nothing claims, when configuration allows it."""
        if self.orphans is None:
            return []
        try:
            unclaimed = self.manager.unclaimed_volumes()
        except libvirt.libvirtError:
            log.debug("could not list unclaimed volumes", exc_info=True)
            return []
        eligible = self.orphans.observe(unclaimed)
        if not eligible:
            return []
        if not self.cfg.maintenance.reap_orphans:
            log.warning(
                "unclaimed overlays in pool %s: %s (maintenance.reap_orphans is off; "
                "remove them with: virsh -c %s vol-delete --pool %s <name>)",
                self.cfg.libvirt.pool,
                ", ".join(eligible),
                self.cfg.libvirt.uri,
                self.cfg.libvirt.pool,
            )
            return []
        removed = []
        for volume in eligible:
            try:
                self.manager.delete_volume(volume)
            except (libvirt.libvirtError, ValueError):
                log.exception("could not delete orphan volume %s", volume)
            else:
                self.orphans.forget(volume)
                removed.append(volume)
        if removed:
            log.warning("deleted orphan overlay(s): %s", ", ".join(removed))
        return removed


def build_state(cfg: Config) -> AppState:
    singleton = SingletonLock()
    singleton.acquire()
    cm = ConnectionManager(cfg.libvirt.uri)
    manager = NodeManager(cm, cfg)
    state = AppState(
        cfg=cfg,
        cm=cm,
        manager=manager,
        provisioner=None,  # type: ignore[arg-type]
        singleton=singleton,
        orphans=poolmod.OrphanTracker(min_age_s=cfg.maintenance.orphan_min_age_s),
    )
    state.provisioner = Provisioner(manager, cfg, maintenance=state.reap_orphans)
    return state


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
        state.startup()
        try:
            yield
        finally:
            state.shutdown()

    app = FastAPI(title="k3s KVM demo", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")

    @app.middleware("http")
    async def guard_unsafe_methods(request: Request, call_next):
        if request.method not in SAFE_METHODS and not _host_allowed(cfg, request):
            return PlainTextResponse("cross-origin request rejected", status_code=403)
        return await call_next(request)

    def grid(request: Request, state: AppState) -> HTMLResponse:
        nodes = state.snapshot()
        return TEMPLATES.TemplateResponse(
            request=request,
            name="_grid.html",
            context={
                "nodes": nodes,
                "quorum": assess_quorum(nodes),
                "flashes": state.flashes.current(),
                "cfg": state.cfg,
                "at_capacity": len(nodes) >= state.cfg.vm.max_nodes,
            },
        )

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request, state: AppState = Depends(get_state)) -> HTMLResponse:
        nodes = state.snapshot()
        return TEMPLATES.TemplateResponse(
            request=request,
            name="index.html",
            context={
                "nodes": nodes,
                "quorum": assess_quorum(nodes),
                "flashes": state.flashes.current(),
                "cfg": state.cfg,
                "at_capacity": len(nodes) >= state.cfg.vm.max_nodes,
            },
        )

    @app.get("/nodes", response_class=HTMLResponse)
    def list_nodes(request: Request, state: AppState = Depends(get_state)) -> HTMLResponse:
        return grid(request, state)

    @app.post("/deploy/{role}", response_class=HTMLResponse)
    def deploy(request: Request, role: str, state: AppState = Depends(get_state)) -> HTMLResponse:
        if role not in meta.ROLES:
            state.flashes.add("error", f"unknown role {role!r}")
            return grid(request, state)
        try:
            node, server_url = state.manager.create(role)
        except (DeployRefused, poolmod.PoolError) as exc:
            state.flashes.add("error", str(exc))
            return grid(request, state)
        except libvirt.libvirtError as exc:
            log.exception("deploy failed")
            state.flashes.add("error", f"libvirt refused to create the node: {exc}")
            return grid(request, state)

        try:
            state.provisioner.submit(node, server_url)
        except (QueueFull, NotAccepting) as exc:
            state.flashes.add("error", f"{exc}; the node will be picked up shortly")
        else:
            label = "control plane node" if role == meta.ROLE_SERVER else "node"
            state.flashes.add("info", f"deploying {label} {node.short_name}")
        return grid(request, state)

    @app.post("/nodes/{name}/kill", response_class=HTMLResponse)
    def kill(request: Request, name: str, state: AppState = Depends(get_state)):
        try:
            node = state.manager.get(name)
        except NodeNotFound:
            return PlainTextResponse(f"no node named {name}", status_code=404)
        state.provisioner.cancel(node.uuid)
        try:
            state.manager.delete_by_name(name)
        except NodeNotFound:
            return PlainTextResponse(f"no node named {name}", status_code=404)
        except libvirt.libvirtError as exc:
            log.exception("killing %s failed", name)
            state.flashes.add("error", f"could not fully remove {node.short_name}: {exc}")
        else:
            state.flashes.add("info", f"killed {node.short_name}")
        return grid(request, state)

    @app.post("/reset", response_class=HTMLResponse)
    def reset(request: Request, state: AppState = Depends(get_state)) -> HTMLResponse:
        for node in state.snapshot():
            state.provisioner.cancel(node.uuid)
        try:
            removed = state.manager.reset()
        except libvirt.libvirtError as exc:
            log.exception("reset failed")
            state.flashes.add("error", f"reset did not complete: {exc}")
        else:
            state.flashes.add("info", f"removed {removed} node(s)")
        return grid(request, state)

    @app.post("/prune-nodes", response_class=HTMLResponse)
    def prune(request: Request, state: AppState = Depends(get_state)) -> HTMLResponse:
        nodes = state.snapshot()
        try:
            server = cluster.pick_server(nodes)
            agent = state.provisioner.agent_for(server.uuid)
            result = cluster.prune_nodes(state.cfg, nodes, agent)
        except (cluster.NoServerAvailable, guestexec.AgentUnavailable, guestexec.AgentError) as exc:
            state.flashes.add("error", str(exc))
            return grid(request, state)
        except guestexec.ExecTimeout as exc:
            state.flashes.add("error", f"kubectl did not answer in time: {exc}")
            return grid(request, state)

        state.flashes.add("info" if not result.failed else "error", result.summary())
        for name, reason in result.failed:
            state.flashes.add("error", f"{name}: {reason}")
        return grid(request, state)

    @app.get("/healthz")
    def healthz(state: AppState = Depends(get_state)) -> JSONResponse:
        payload: dict[str, object] = {
            "uri": state.cfg.libvirt.uri,
            "connection_epoch": state.cm.epoch,
            "singleton_lock": state.singleton is not None,
            "in_flight": state.provisioner.in_flight,
            "queue_load": state.provisioner.pool.load,
        }
        try:
            nodes = state.manager.list_nodes()
            payload["nodes"] = len(nodes)
            payload["states"] = sorted({node.state for node in nodes})
        except libvirt.libvirtError as exc:
            payload["error"] = str(exc)
            return JSONResponse(payload, status_code=503)

        try:
            status = state.cm.read(
                lambda conn: poolmod.inspect(
                    conn.storagePoolLookupByName(state.cfg.libvirt.pool),
                    state.cfg.vm.name_prefix,
                )
            )
            payload["pool_marker"] = status.marker
            payload["unclassified_volumes"] = list(status.unknown)
            payload["orphan_candidates"] = sorted(state.manager.unclaimed_volumes())
        except libvirt.libvirtError as exc:
            payload["pool_error"] = str(exc)
        return JSONResponse(payload)

    return app
