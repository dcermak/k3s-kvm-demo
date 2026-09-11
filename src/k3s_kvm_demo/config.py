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
from dataclasses import dataclass, field
from pathlib import Path

from . import names

GIB = 1024**3


class ConfigError(Exception):
    """Configuration is unusable.  Always names the offending key."""

    def __init__(self, key: str, message: str) -> None:
        super().__init__(f"{key}: {message}")
        self.key = key


@dataclass(frozen=True)
class ServerConfig:
    bind: str = "127.0.0.1"
    port: int = 8000
    allowed_hosts: tuple[str, ...] = ("127.0.0.1:8000", "localhost:8000")
    lock_path: Path = Path("/run/k3s-kvm-demo/dashboard.lock")


@dataclass(frozen=True)
class LibvirtConfig:
    uri: str = "qemu:///system"
    pool: str = "k3s-demo"
    network: str = "default"

    @property
    def is_test_driver(self) -> bool:
        return self.uri.startswith("test:")


@dataclass(frozen=True)
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


@dataclass(frozen=True)
class ClusterConfig:
    token: str
    tls_san: tuple[str, ...] = ()


@dataclass(frozen=True)
class ObservationConfig:
    interval_s: int = 2
    qga_timeout_s: int = 2
    stale_after_s: int = 30


@dataclass(frozen=True)
class MaintenanceConfig:
    shutdown_grace_s: int = 30


@dataclass(frozen=True)
class Config:
    server: ServerConfig
    libvirt: LibvirtConfig
    vm: VMConfig
    cluster: ClusterConfig
    observation: ObservationConfig
    maintenance: MaintenanceConfig
    source: Path | None = field(default=None, compare=False)


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
    if "firstboot" in raw:
        raise ConfigError(
            "firstboot",
            "host provisioning was removed; rebuild the guest image and "
            "replace [firstboot] with [observation]",
        )
    server = _load_server(_section(raw, "server"))
    libvirt_cfg = _load_libvirt(_section(raw, "libvirt"))
    vm = _load_vm(_section(raw, "vm"))
    cluster = _load_cluster(_section(raw, "cluster"))
    observation = _load_observation(_section(raw, "observation"))
    maintenance = _load_maintenance(_section(raw, "maintenance"))
    return Config(
        server=server,
        libvirt=libvirt_cfg,
        vm=vm,
        cluster=cluster,
        observation=observation,
        maintenance=maintenance,
        source=source,
    )


def _load_server(section: dict) -> ServerConfig:
    bind = section.get("bind", ServerConfig.bind)
    if not isinstance(bind, str):
        raise ConfigError("server.bind", "must be a string")
    if not _is_loopback(bind):
        raise ConfigError(
            "server.bind",
            f"{bind!r} is not a loopback address. This dashboard destroys VMs and "
            "runs root commands inside guests over an unauthenticated API; it must "
            "not listen off-host.",
        )
    port = _positive_int(section, "server", "port", ServerConfig.port)
    if port > 65535:
        raise ConfigError("server.port", f"must be <= 65535, got {port}")
    hosts = _str_tuple(
        section,
        "server",
        "allowed_hosts",
        (f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}"),
    )
    if not hosts:
        raise ConfigError("server.allowed_hosts", "must list at least one host")
    lock_path = section.get("lock_path", str(ServerConfig.lock_path))
    if not isinstance(lock_path, str) or not Path(lock_path).is_absolute():
        raise ConfigError(
            "server.lock_path", "must be an absolute path shared by all launch methods"
        )
    return ServerConfig(bind=bind, port=port, allowed_hosts=hosts, lock_path=Path(lock_path))


def _load_libvirt(section: dict) -> LibvirtConfig:
    values = {}
    for key, default in (
        ("uri", LibvirtConfig.uri),
        ("pool", LibvirtConfig.pool),
        ("network", LibvirtConfig.network),
    ):
        value = section.get(key, default)
        if not isinstance(value, str) or not value:
            raise ConfigError(f"libvirt.{key}", "must be a non-empty string")
        values[key] = value
    return LibvirtConfig(**values)


def _load_vm(section: dict) -> VMConfig:
    base_image = Path(_required(section, "vm", "base_image")).expanduser().resolve()
    if not base_image.is_file():
        raise ConfigError("vm.base_image", f"{base_image} does not exist or is not a file")

    prefix = section.get("name_prefix", VMConfig.name_prefix)
    if not isinstance(prefix, str):
        raise ConfigError("vm.name_prefix", "must be a string")
    try:
        names.validate_prefix(prefix)
    except names.InvalidName as exc:
        raise ConfigError("vm.name_prefix", str(exc)) from exc

    disk_gb = _positive_int(section, "vm", "disk_gb", VMConfig.disk_gb)
    memory_mb = _positive_int(section, "vm", "memory_mb", VMConfig.memory_mb, minimum=512)
    vcpus = _positive_int(section, "vm", "vcpus", VMConfig.vcpus)
    max_nodes = _positive_int(section, "vm", "max_nodes", VMConfig.max_nodes)

    info = _qemu_img_info(base_image)
    if info.get("backing_file"):
        raise ConfigError("vm.base_image", "must be a standalone qcow2 without a backing file")
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
    return {
        "format": data.get("format"),
        "virtual_size": int(data.get("virtual-size", 0)),
        "backing_file": data.get("backing-filename"),
    }


def _load_cluster(section: dict) -> ClusterConfig:
    token = _required(section, "cluster", "token")
    if not isinstance(token, str) or not token.strip():
        raise ConfigError("cluster.token", "must be a non-empty string")
    tls_san = _str_tuple(section, "cluster", "tls_san", ())
    for san in tls_san:
        if not san.strip():
            raise ConfigError("cluster.tls_san", "entries must be non-empty")
    return ClusterConfig(token=token, tls_san=tls_san)


def _load_observation(section: dict) -> ObservationConfig:
    observation = ObservationConfig(
        **{
            key: _positive_int(section, "observation", key, getattr(ObservationConfig, key))
            for key in ("interval_s", "qga_timeout_s", "stale_after_s")
        }
    )
    if observation.stale_after_s <= observation.interval_s:
        raise ConfigError(
            "observation.stale_after_s",
            "must exceed interval_s so completed probes can be observed",
        )
    if observation.qga_timeout_s > 2:
        raise ConfigError(
            "observation.qga_timeout_s", "must be 1 or 2 seconds to bound observation passes"
        )
    return observation


def validate_hypervisor(conn, cfg: Config) -> list[str]:
    """Check what only a live connection can tell us.

    Hard problems raise :class:`ConfigError`; things merely worth knowing come
    back as warnings.  Skipped entirely for the test driver, which has neither
    a real pool directory nor KVM.
    """
    import xml.etree.ElementTree as ET

    import libvirt

    from . import domxml, meta, pool as poolmod

    meta.check_compatible(conn)
    if shutil.which("xorriso") is None:
        raise ConfigError("vm", "xorriso is required to generate configuration disks")

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

    target = domxml.pool_target_path(storage).resolve()
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

    _, _, available = storage.info()[1:4]
    headroom = cfg.vm.max_nodes * 4 * GIB
    if available < headroom:
        warnings.append(
            f"pool {cfg.libvirt.pool!r} has {available / GIB:.1f} GiB free; "
            f"{cfg.vm.max_nodes} nodes may want more than that as their overlays grow"
        )

    return warnings


def _load_maintenance(section: dict) -> MaintenanceConfig:
    reap = section.get("reap_orphans", False)
    if reap is not False:
        raise ConfigError(
            "maintenance.reap_orphans",
            "automatic orphan deletion was removed; inspect candidates manually",
        )
    return MaintenanceConfig(
        shutdown_grace_s=_positive_int(
            section, "maintenance", "shutdown_grace_s", MaintenanceConfig.shutdown_grace_s
        ),
    )
