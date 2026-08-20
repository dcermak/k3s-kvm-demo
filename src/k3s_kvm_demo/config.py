"""Configuration loading and validation.

Validation is split in two.  :func:`load` checks everything that can be decided
from the file and the local filesystem.  :func:`validate_hypervisor` checks the
things that need a live libvirt connection, and is skipped for ``test:`` URIs.
"""

from __future__ import annotations

import ipaddress
import json
import shutil
import subprocess
import tomllib
from dataclasses import MISSING, dataclass, field, fields
from pathlib import Path

from . import names

GIB = 1024**3


class ConfigError(Exception):
    """Configuration is unusable.  Always names the offending key."""

    def __init__(self, key: str, message: str) -> None:
        super().__init__(f"{key}: {message}")
        self.key = key


@dataclass(frozen=True, slots=True)
class ServerConfig:
    bind: str = "127.0.0.1"
    port: int = 8000
    allowed_hosts: tuple[str, ...] = ("127.0.0.1:8000", "localhost:8000")


@dataclass(frozen=True, slots=True)
class LibvirtConfig:
    uri: str = "qemu:///system"
    pool: str = "k3s-demo"
    network: str = "default"

    @property
    def is_test_driver(self) -> bool:
        return self.uri.startswith("test:")


@dataclass(frozen=True, slots=True)
class VMConfig:
    base_image: Path
    disk_gb: int = 20
    memory_mb: int = 2048
    vcpus: int = 2
    name_prefix: str = "k3s-node"
    max_nodes: int = 8

    @property
    def disk_bytes(self) -> int:
        return self.disk_gb * GIB


@dataclass(frozen=True, slots=True)
class ClusterConfig:
    token: str
    tls_san: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class FirstbootConfig:
    script: Path
    boot_timeout_s: int = 180
    exec_timeout_s: int = 300
    qga_timeout_s: int = 10
    retry_backoff_s: tuple[int, ...] = (2, 5, 10, 30)
    log_tail_bytes: int = 8192


@dataclass(frozen=True, slots=True)
class MaintenanceConfig:
    reap_orphans: bool = False
    orphan_min_age_s: int = 600
    shutdown_grace_s: int = 30
    service_probe_attempts: int = 20


@dataclass(frozen=True, slots=True)
class Config:
    server: ServerConfig
    libvirt: LibvirtConfig
    vm: VMConfig
    cluster: ClusterConfig
    firstboot: FirstbootConfig
    maintenance: MaintenanceConfig
    source: Path | None = field(default=None, compare=False)


def _defaults(cls: type) -> dict:
    """Field defaults by name.

    ``slots=True`` replaces the class attributes with slot descriptors, so
    ``VMConfig.disk_gb`` is not the default — the field metadata is.
    """
    return {f.name: f.default for f in fields(cls) if f.default is not MISSING}


D_SERVER = _defaults(ServerConfig)
D_LIBVIRT = _defaults(LibvirtConfig)
D_VM = _defaults(VMConfig)
D_FIRSTBOOT = _defaults(FirstbootConfig)
D_MAINTENANCE = _defaults(MaintenanceConfig)


def _section(raw: dict, name: str) -> dict:
    value = raw.get(name, {})
    if not isinstance(value, dict):
        raise ConfigError(name, "must be a table")
    return value


def _required(section: dict, table: str, key: str):
    if key not in section:
        raise ConfigError(f"{table}.{key}", "is required")
    return section[key]


def _positive_int(section: dict, table: str, key: str, default: int, minimum: int = 1) -> int:
    value = section.get(key, default)
    if not isinstance(value, int) or isinstance(value, bool):
        raise ConfigError(f"{table}.{key}", f"must be an integer, got {value!r}")
    if value < minimum:
        raise ConfigError(f"{table}.{key}", f"must be >= {minimum}, got {value}")
    return value


def _str_tuple(section: dict, table: str, key: str, default: tuple[str, ...]) -> tuple[str, ...]:
    value = section.get(key, default)
    if isinstance(value, tuple):
        return value
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ConfigError(f"{table}.{key}", "must be a list of strings")
    return tuple(value)


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def load(path: str | Path) -> Config:
    """Read and statically validate the TOML configuration at *path*."""
    path = Path(path)
    try:
        raw = tomllib.loads(path.read_text())
    except FileNotFoundError as exc:
        raise ConfigError("<file>", f"{path} does not exist") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError("<file>", f"{path} is not valid TOML: {exc}") from exc
    return from_mapping(raw, source=path)


def from_mapping(raw: dict, *, source: Path | None = None) -> Config:
    server = _load_server(_section(raw, "server"))
    libvirt_cfg = _load_libvirt(_section(raw, "libvirt"))
    vm = _load_vm(_section(raw, "vm"))
    cluster = _load_cluster(_section(raw, "cluster"))
    firstboot = _load_firstboot(_section(raw, "firstboot"), source)
    maintenance = _load_maintenance(_section(raw, "maintenance"))
    return Config(
        server=server,
        libvirt=libvirt_cfg,
        vm=vm,
        cluster=cluster,
        firstboot=firstboot,
        maintenance=maintenance,
        source=source,
    )


def _load_server(section: dict) -> ServerConfig:
    bind = section.get("bind", D_SERVER["bind"])
    if not isinstance(bind, str):
        raise ConfigError("server.bind", "must be a string")
    if not _is_loopback(bind):
        raise ConfigError(
            "server.bind",
            f"{bind!r} is not a loopback address. This dashboard destroys VMs and "
            "runs root commands inside guests over an unauthenticated API; it must "
            "not listen off-host.",
        )
    port = _positive_int(section, "server", "port", D_SERVER["port"])
    if port > 65535:
        raise ConfigError("server.port", f"must be <= 65535, got {port}")
    hosts = _str_tuple(section, "server", "allowed_hosts", D_SERVER["allowed_hosts"])
    if not hosts:
        raise ConfigError("server.allowed_hosts", "must list at least one host")
    return ServerConfig(bind=bind, port=port, allowed_hosts=hosts)


def _load_libvirt(section: dict) -> LibvirtConfig:
    values = {}
    for key, default in (
        ("uri", D_LIBVIRT["uri"]),
        ("pool", D_LIBVIRT["pool"]),
        ("network", D_LIBVIRT["network"]),
    ):
        value = section.get(key, default)
        if not isinstance(value, str) or not value:
            raise ConfigError(f"libvirt.{key}", "must be a non-empty string")
        values[key] = value
    return LibvirtConfig(**values)


def _load_vm(section: dict) -> VMConfig:
    base_image = Path(_required(section, "vm", "base_image")).expanduser()
    if not base_image.is_file():
        raise ConfigError("vm.base_image", f"{base_image} does not exist or is not a file")

    prefix = section.get("name_prefix", D_VM["name_prefix"])
    if not isinstance(prefix, str):
        raise ConfigError("vm.name_prefix", "must be a string")
    try:
        names.validate_prefix(prefix)
    except names.InvalidName as exc:
        raise ConfigError("vm.name_prefix", str(exc)) from exc

    disk_gb = _positive_int(section, "vm", "disk_gb", D_VM["disk_gb"])
    memory_mb = _positive_int(section, "vm", "memory_mb", D_VM["memory_mb"], minimum=512)
    vcpus = _positive_int(section, "vm", "vcpus", D_VM["vcpus"])
    max_nodes = _positive_int(section, "vm", "max_nodes", D_VM["max_nodes"])

    info = _qemu_img_info(base_image)
    if info["format"] != "qcow2":
        raise ConfigError(
            "vm.base_image", f"must be a qcow2 image, qemu-img reports {info['format']!r}"
        )
    if disk_gb * GIB < info["virtual_size"]:
        raise ConfigError(
            "vm.disk_gb",
            f"{disk_gb} GiB is smaller than the base image's virtual size of "
            f"{info['virtual_size'] / GIB:.1f} GiB; the overlay would truncate it",
        )

    return VMConfig(
        base_image=base_image,
        disk_gb=disk_gb,
        memory_mb=memory_mb,
        vcpus=vcpus,
        name_prefix=prefix,
        max_nodes=max_nodes,
    )


def _qemu_img_info(image: Path) -> dict:
    binary = shutil.which("qemu-img")
    if binary is None:
        raise ConfigError("vm.base_image", "qemu-img is not installed, cannot inspect the image")
    try:
        out = subprocess.run(
            [binary, "info", "--output=json", str(image)],
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        ).stdout
    except subprocess.CalledProcessError as exc:
        raise ConfigError(
            "vm.base_image", f"qemu-img could not read it: {exc.stderr.strip()}"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise ConfigError("vm.base_image", "qemu-img timed out inspecting it") from exc
    data = json.loads(out)
    return {"format": data.get("format"), "virtual_size": int(data.get("virtual-size", 0))}


def _load_cluster(section: dict) -> ClusterConfig:
    token = _required(section, "cluster", "token")
    if not isinstance(token, str) or not token.strip():
        raise ConfigError("cluster.token", "must be a non-empty string")
    tls_san = _str_tuple(section, "cluster", "tls_san", ())
    for san in tls_san:
        if not san.strip():
            raise ConfigError("cluster.tls_san", "entries must be non-empty")
    return ClusterConfig(token=token, tls_san=tls_san)


def _load_firstboot(section: dict, source: Path | None) -> FirstbootConfig:
    script = Path(_required(section, "firstboot", "script")).expanduser()
    if not script.is_absolute() and source is not None:
        script = (source.parent / script).resolve()
    if not script.is_file():
        raise ConfigError("firstboot.script", f"{script} does not exist or is not a file")

    def timeout(key: str) -> int:
        return _positive_int(section, "firstboot", key, D_FIRSTBOOT[key])

    boot_timeout_s = timeout("boot_timeout_s")
    exec_timeout_s = timeout("exec_timeout_s")
    qga_timeout_s = timeout("qga_timeout_s")
    log_tail_bytes = timeout("log_tail_bytes")

    backoff = section.get("retry_backoff_s", list(D_FIRSTBOOT["retry_backoff_s"]))
    if not isinstance(backoff, (list, tuple)) or not backoff:
        raise ConfigError("firstboot.retry_backoff_s", "must be a non-empty list of integers")
    for entry in backoff:
        if not isinstance(entry, int) or isinstance(entry, bool) or entry < 1:
            raise ConfigError(
                "firstboot.retry_backoff_s",
                f"entries must be positive integers, got {entry!r}",
            )

    return FirstbootConfig(
        script=script,
        boot_timeout_s=boot_timeout_s,
        exec_timeout_s=exec_timeout_s,
        qga_timeout_s=qga_timeout_s,
        retry_backoff_s=tuple(backoff),
        log_tail_bytes=log_tail_bytes,
    )


def validate_hypervisor(conn, cfg: Config) -> list[str]:
    """Check what only a live connection can tell us.

    Hard problems raise :class:`ConfigError`; things merely worth knowing come
    back as warnings.  Skipped entirely for the test driver, which has neither
    a real pool directory nor KVM.
    """
    import os
    import xml.etree.ElementTree as ET

    import libvirt

    from . import domxml, pool as poolmod

    if cfg.libvirt.is_test_driver:
        return []

    warnings: list[str] = []

    try:
        storage = conn.storagePoolLookupByName(cfg.libvirt.pool)
    except libvirt.libvirtError as exc:
        raise ConfigError(
            "libvirt.pool",
            f"{cfg.libvirt.pool!r} does not exist. Run 'k3s-demo init-pool' to create it.",
        ) from exc
    if not storage.isActive():
        raise ConfigError("libvirt.pool", f"{cfg.libvirt.pool!r} is not active")
    pool_root = ET.fromstring(storage.XMLDesc(0))
    if pool_root.get("type") != "dir":
        raise ConfigError("libvirt.pool", f"{cfg.libvirt.pool!r} must be a directory pool")

    try:
        poolmod.require_owned(storage, cfg.vm.name_prefix)
    except poolmod.PoolError as exc:
        raise ConfigError("libvirt.pool", str(exc)) from exc

    target = domxml.pool_target_path(storage)
    if target in cfg.vm.base_image.resolve().parents:
        raise ConfigError(
            "vm.base_image",
            f"must not live inside the demo pool at {target}; the pool holds only "
            "overlays this app may delete",
        )

    try:
        network = conn.networkLookupByName(cfg.libvirt.network)
    except libvirt.libvirtError as exc:
        raise ConfigError("libvirt.network", f"{cfg.libvirt.network!r} does not exist") from exc
    if not network.isActive():
        raise ConfigError("libvirt.network", f"{cfg.libvirt.network!r} is not active")

    try:
        domain_type = domxml.select_domain_type(conn)
    except domxml.UnsupportedHypervisor as exc:
        raise ConfigError("libvirt.uri", str(exc)) from exc
    if domain_type != domxml.TYPE_KVM:
        warnings.append(
            "KVM is unavailable; guests would run under TCG emulation and be far too "
            "slow for a live demo"
        )
    elif not os.access("/dev/kvm", os.R_OK | os.W_OK):
        warnings.append("/dev/kvm is not readable and writable by this user")

    _, _, available = storage.info()[1:4]
    headroom = cfg.vm.max_nodes * 4 * GIB
    if available < headroom:
        warnings.append(
            f"pool {cfg.libvirt.pool!r} has {available / GIB:.1f} GiB free; "
            f"{cfg.vm.max_nodes} nodes may want more than that as their overlays grow"
        )

    return warnings


def _load_maintenance(section: dict) -> MaintenanceConfig:
    reap = section.get("reap_orphans", D_MAINTENANCE["reap_orphans"])
    if not isinstance(reap, bool):
        raise ConfigError("maintenance.reap_orphans", "must be a boolean")
    return MaintenanceConfig(
        reap_orphans=reap,
        # Zero is meaningful here: it drops the age requirement, leaving the
        # two-pass rule as the only guard.
        orphan_min_age_s=_positive_int(
            section,
            "maintenance",
            "orphan_min_age_s",
            D_MAINTENANCE["orphan_min_age_s"],
            minimum=0,
        ),
        shutdown_grace_s=_positive_int(
            section, "maintenance", "shutdown_grace_s", D_MAINTENANCE["shutdown_grace_s"]
        ),
        service_probe_attempts=_positive_int(
            section,
            "maintenance",
            "service_probe_attempts",
            D_MAINTENANCE["service_probe_attempts"],
        ),
    )
