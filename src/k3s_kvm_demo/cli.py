"""Command line entry points: ``init-pool``, ``check`` and ``serve``."""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

import libvirt

from . import config as configmod
from . import pool as poolmod
from .conn import AlreadyRunning

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
    return parser


def cmd_init_pool(cfg: configmod.Config, path: str | None) -> int:
    target = Path(path) if path else DEFAULT_POOL_ROOT / cfg.libvirt.pool
    conn = libvirt.open(cfg.libvirt.uri)
    try:
        storage = poolmod.init_pool(conn, cfg.libvirt.pool, target)
    except poolmod.PoolError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        conn.close()
    print(f"pool {cfg.libvirt.pool!r} is ready at {target} and marked as this demo's")
    print(f"  marker volume: {poolmod.MARKER_VOLUME}")
    del storage
    return 0


def cmd_check(cfg: configmod.Config) -> int:
    print(f"configuration at {cfg.source} parses cleanly")
    conn = libvirt.open(cfg.libvirt.uri)
    try:
        warnings = configmod.validate_hypervisor(conn, cfg)
    except configmod.ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        conn.close()
    for warning in warnings:
        print(f"warning: {warning}")
    print("hypervisor prerequisites are satisfied")
    return 0


def cmd_serve(cfg: configmod.Config) -> int:
    import uvicorn

    from .app import build_state, create_app

    # Fail before uvicorn binds anything if another instance is already running.
    conn = libvirt.open(cfg.libvirt.uri)
    try:
        for warning in configmod.validate_hypervisor(conn, cfg):
            log.warning("%s", warning)
    finally:
        conn.close()

    app = create_app(cfg, lambda: build_state(cfg))
    uvicorn.run(app, host=cfg.server.bind, port=cfg.server.port, log_level="info")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    _setup_logging(args.verbose)

    try:
        cfg = configmod.load(_config_path(args.config))
    except configmod.ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    try:
        if args.command == "init-pool":
            return cmd_init_pool(cfg, args.path)
        if args.command == "check":
            return cmd_check(cfg)
        return cmd_serve(cfg)
    except AlreadyRunning as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 3
    except configmod.ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except libvirt.libvirtError as exc:
        print(f"libvirt error: {exc}", file=sys.stderr)
        return 4


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
