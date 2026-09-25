"""Command line entry points: ``init-pool``, ``check``, ``export`` and ``serve``."""

from __future__ import annotations

import argparse
import contextlib
import logging
import os
import sys
from pathlib import Path

import libvirt

from . import config as configmod
from . import cluster, guestexec, meta, pool as poolmod
from .conn import ConnectionManager
from .libvirtctl import NodeManager

DEFAULT_CONFIG = "config.toml"
DEFAULT_POOL_ROOT = Path("/var/lib/libvirt/images")

log = logging.getLogger("k3s_kvm_demo")


def _config_path(value: str | None) -> Path:
    return Path(value or os.environ.get("K3S_DEMO_CONFIG") or DEFAULT_CONFIG)


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    # libvirt prints its own errors to stderr; we surface them ourselves.
    libvirt.registerErrorHandler(lambda _ctx, _err: None, None)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="k3s-demo", description="Booth dashboard for running k3s nodes as KVM guests."
    )
    parser.add_argument(
        "-c", "--config", help=f"path to the TOML config (default: {DEFAULT_CONFIG})"
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser(
        "init-pool",
        help="create the dedicated storage pool and mark it as owned by this demo",
    )
    init.add_argument(
        "--path",
        help=f"directory to back the pool (default: {DEFAULT_POOL_ROOT}/<pool name>)",
    )

    sub.add_parser("check", help="validate the configuration against the hypervisor")
    sub.add_parser("serve", help="run the dashboard")
    export = sub.add_parser("export", help="export a current control plane node's kubeconfig")
    export.add_argument("-o", "--output", help="write a new private file instead of stdout")
    return parser


def _fail(exc: object, code: int, prefix: str = "error") -> int:
    print(f"{prefix}: {exc}", file=sys.stderr)
    return code


def _connect(cfg: configmod.Config):
    return contextlib.closing(libvirt.open(cfg.libvirt.uri))


def cmd_init_pool(cfg: configmod.Config, path: str | None) -> int:
    target = Path(path) if path else DEFAULT_POOL_ROOT / cfg.libvirt.pool
    with _connect(cfg) as conn:
        try:
            poolmod.init_pool(conn, cfg.libvirt.pool, target)
        except poolmod.PoolError as exc:
            return _fail(exc, 1)
    print(f"pool {cfg.libvirt.pool!r} is ready at {target} and marked as this demo's")
    print(f"  marker volume: {poolmod.MARKER_VOLUME}")
    return 0


def cmd_check(cfg: configmod.Config) -> int:
    try:
        configmod.validate_export_path(cfg)
    except configmod.ConfigError as exc:
        return _fail(exc, 1)
    with _connect(cfg) as conn:
        try:
            warnings = configmod.validate_hypervisor(conn, cfg)
        except configmod.ConfigError as exc:
            return _fail(exc, 1)
    print(f"configuration at {cfg.source} parses cleanly")
    for warning in warnings:
        print(f"warning: {warning}")
    print("hypervisor prerequisites are satisfied")
    return 0


def cmd_export(cfg: configmod.Config, output: str | None) -> int:
    try:
        with contextlib.closing(ConnectionManager(cfg.libvirt.uri)) as cm:
            manager = NodeManager(cm, cfg)
            manager.check_compatible()
            manager.require_owned_pool()
            text = cluster.export_kubeconfig(
                manager.list_nodes(),
                lambda uuid: guestexec.QemuAgent(
                    cm, uuid, timeout_s=cfg.observation.qga_timeout_s
                ),
            )
        if output is None:
            sys.stdout.write(text)
        else:
            fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(text)
    except FileExistsError:
        return _fail(f"output path already exists: {output}", 1)
    except (cluster.KubeconfigExportError, OSError) as exc:
        return _fail(exc, 1)
    return 0


def cmd_serve(cfg: configmod.Config) -> int:
    import uvicorn

    from .app import build_state, create_app

    configmod.validate_export_path(cfg)
    with _connect(cfg) as conn:
        for warning in configmod.validate_hypervisor(conn, cfg):
            log.warning("%s", warning)

    app = create_app(cfg, lambda: build_state(cfg))
    uvicorn.run(app, host=cfg.server.bind, port=cfg.server.port, log_level="info")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    _setup_logging(args.verbose)

    try:
        cfg = configmod.load(_config_path(args.config))
        if args.command == "init-pool":
            return cmd_init_pool(cfg, args.path)
        if args.command == "check":
            return cmd_check(cfg)
        if args.command == "export":
            return cmd_export(cfg, args.output)
        return cmd_serve(cfg)
    except configmod.ConfigError as exc:
        return _fail(exc, 2)
    except (meta.CompatibilityError, poolmod.PoolError) as exc:
        return _fail(exc, 2)
    except libvirt.libvirtError as exc:
        return _fail(exc, 4, prefix="libvirt error")


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
