"""Shared fixtures.

libvirt runs for real through ``test:///default``: it supports the whole
lifecycle we exercise — volumes with a backing store, define/start/destroy,
metadata including the LIVE vs CONFIG split, lease addresses and stats.
The guest agent and seed upload are faked; seed ISO generation is real.

The test driver shares state between connections in a process, which is what
lets a reconnect keep its domains, but it also means tests must clean up after
themselves; :func:`libvirt_clean` does that.
"""

from __future__ import annotations

import contextlib
import subprocess
import textwrap
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

import libvirt
import pytest

from k3s_kvm_demo import config as configmod
from k3s_kvm_demo import guestexec, meta, pool as poolmod, seed
from k3s_kvm_demo.conn import ConnectionManager
from k3s_kvm_demo.libvirtctl import NodeManager
from k3s_kvm_demo.observer import GUEST_PATH, Observer

TEST_URI = "test:///default"
TEST_POOL = "default-pool"
BUILTIN_DOMAIN = "test"

REPO_ROOT = Path(__file__).resolve().parents[1]

libvirt.registerErrorHandler(lambda _ctx, _err: None, None)


def fake_libvirt_error(code: int, message: str = "synthetic libvirt error") -> libvirt.libvirtError:
    """Build a libvirtError with a chosen code.

    Its real constructor reads the thread-local error from the C library, so
    the only way to script one is to fill in ``err`` directly.
    """
    exc = libvirt.libvirtError.__new__(libvirt.libvirtError)
    Exception.__init__(exc, message)
    exc.err = (code, 0, message, 0, "", None, None, 0, 0)
    return exc


def _wipe(conn: libvirt.virConnect) -> None:
    for dom in conn.listAllDomains(0):
        if dom.name() == BUILTIN_DOMAIN:
            continue
        try:
            if dom.isActive():
                dom.destroy()
            dom.undefine()
        except libvirt.libvirtError:  # pragma: no cover - best effort
            pass
    try:
        pool = conn.storagePoolLookupByName(TEST_POOL)
    except libvirt.libvirtError:  # pragma: no cover
        return
    for volume in pool.listAllVolumes(0):
        with contextlib.suppress(libvirt.libvirtError):  # pragma: no cover
            volume.delete(0)


@pytest.fixture(autouse=True)
def explicit_backing_evidence(monkeypatch):
    """Give only the test driver's built-in disk an explicit raw format."""
    real = libvirt.virDomain.XMLDesc

    def described(dom, flags=0):
        xml = real(dom, flags)
        if dom.connect().getURI() != TEST_URI or dom.name() != BUILTIN_DOMAIN:
            return xml
        root = ET.fromstring(xml)
        for disk in root.findall("./devices/disk"):
            if disk.find("driver") is None:
                ET.SubElement(disk, "driver", {"name": "qemu", "type": "raw"})
        return ET.tostring(root, encoding="unicode")

    monkeypatch.setattr(libvirt.virDomain, "XMLDesc", described)


@pytest.fixture(autouse=True)
def libvirt_clean():
    conn = libvirt.open(TEST_URI)
    _wipe(conn)
    yield
    _wipe(conn)
    conn.close()


@pytest.fixture
def conn_factory():
    """Open extra connections for assertions; they are closed at teardown."""
    opened: list[libvirt.virConnect] = []

    def factory() -> libvirt.virConnect:
        connection = libvirt.open(TEST_URI)
        opened.append(connection)
        return connection

    yield factory
    for connection in opened:
        connection.close()


@pytest.fixture
def conn(conn_factory) -> libvirt.virConnect:
    """A second connection to assert against, independent of the app's own."""
    return conn_factory()


def add_volume(conn: libvirt.virConnect, name: str) -> str:
    """Put a volume straight into the pool.  Returns its path."""
    storage = conn.storagePoolLookupByName(TEST_POOL)
    storage.createXML(
        f"<volume type='file'><name>{name}</name>"
        "<capacity unit='bytes'>1048576</capacity>"
        "<target><format type='qcow2'/></target></volume>",
        0,
    )
    return storage.storageVolLookupByName(name).path()


def volumes(conn: libvirt.virConnect, prefix: str | None = None) -> set[str]:
    """Volume names in the pool, optionally only those starting with *prefix*."""
    found = {v.name() for v in conn.storagePoolLookupByName(TEST_POOL).listAllVolumes(0)}
    return {name for name in found if prefix is None or name.startswith(prefix)}


@pytest.fixture
def base_image(tmp_path: Path) -> Path:
    """A real qcow2, because config validation shells out to qemu-img."""
    path = tmp_path / "k3s-base.qcow2"
    subprocess.run(
        ["qemu-img", "create", "-f", "qcow2", str(path), "2G"],
        check=True,
        capture_output=True,
    )
    return path


@pytest.fixture
def config_values(base_image: Path, tmp_path: Path) -> dict:
    return {
        "server": {"bind": "127.0.0.1", "port": 8000, "allowed_hosts": ["testserver"]},
        "libvirt": {"uri": TEST_URI, "pool": TEST_POOL, "network": "default"},
        "vm": {
            "base_image": str(base_image),
            "disk_gb": 4,
            "memory_mb": 1024,
            "vcpus": 2,
            "name_prefix": "k3s-node",
            "max_nodes": 4,
        },
        "cluster": {"token": "test-token", "tls_san": []},
        "kubeconfig_export": {"path": str(tmp_path / "kubeconfig.yaml"), "interval_s": 1},
        "observation": {
            "interval_s": 2,
            "qga_timeout_s": 2,
            "stale_after_s": 30,
        },
        "maintenance": {
            "shutdown_grace_s": 2,
        },
    }


@pytest.fixture
def cfg(config_values: dict) -> configmod.Config:
    return configmod.from_mapping(config_values)


@pytest.fixture
def marked_pool(cfg: configmod.Config):
    """The test driver's pool, marked as ours."""
    conn = libvirt.open(TEST_URI)
    pool = conn.storagePoolLookupByName(TEST_POOL)
    pool.createXML(poolmod.build_marker_xml(), 0)
    yield pool
    conn.close()


@pytest.fixture
def cm(cfg: configmod.Config):
    manager = ConnectionManager(cfg.libvirt.uri)
    manager.open()
    yield manager
    manager.close()


@pytest.fixture
def seed_upload(monkeypatch):
    """The test driver has no streams; leave ISO construction untouched."""
    uploads = []

    def upload(_conn, volume, artifact):
        uploads.append((volume.name(), artifact.read_bytes()))

    monkeypatch.setattr(seed, "upload_seed", upload)
    return uploads


@pytest.fixture
def manager(cm: ConnectionManager, cfg: configmod.Config, marked_pool, seed_upload) -> NodeManager:
    manager = NodeManager(cm, cfg)
    manager.join_ready = lambda node: True
    return manager


# -- fake guest agent ------------------------------------------------------


@dataclass
class Invocation:
    path: str
    args: list[str]
    input_data: str | None


@dataclass
class FakeAgent:
    """Scripted stand-in for the QEMU guest agent.

    ``responses`` maps a matcher over the argv to the result to return; the
    first match wins.  ``pings`` counts down before the agent starts answering,
    so boot-timeout paths need no real waiting.
    """

    pings_until_up: int = 0
    responses: list = field(default_factory=list)
    default: guestexec.ExecResult | None = None
    calls: list[Invocation] = field(default_factory=list)
    _pending: dict = field(default_factory=dict)
    _next_pid: int = 1000
    ping_calls: int = 0
    uuid: str = ""

    def for_node(self, uuid: str, timeout: int):
        self.uuid = uuid
        return self

    def ping(self) -> None:
        self.ping_calls += 1
        if self.ping_calls <= self.pings_until_up:
            raise guestexec.AgentUnavailable("guest agent not up yet")

    def start(self, path: str, args, *, input_data: str | None = None) -> int:
        self.calls.append(Invocation(path, list(args), input_data))
        self._next_pid += 1
        self._pending[self._next_pid] = self._resolve(path, list(args))
        return self._next_pid

    def poll(self, pid: int):
        return self._pending.pop(pid, None)

    def _resolve(self, path: str, args: list[str]):
        for matcher, result in self.responses:
            if matcher(path, args):
                return result() if callable(result) else result
        if self.default is not None:
            return self.default
        return guestexec.ExecResult(exitcode=0, signal=None, stdout="", stderr="")


def result(exitcode: int = 0, stdout: str = "", stderr: str = "") -> guestexec.ExecResult:
    return guestexec.ExecResult(exitcode=exitcode, signal=None, stdout=stdout, stderr=stderr)


def is_status(path: str, args: list[str]) -> bool:
    return path == "/usr/bin/timeout" and args[-2:] == [GUEST_PATH, "status"]


def report(node_uuid: str, **changes) -> guestexec.ExecResult:
    fields = {
        "protocol": "1",
        "node_uuid": node_uuid,
        "prepared": "1",
        "started": "1",
        "prepare_state": "active",
        "k3s_state": "active",
        "error_code": "none",
    }
    fields.update(changes)
    return result(stdout="".join(f"{key}={value}\n" for key, value in fields.items()))


def guest_ready(agent: FakeAgent) -> FakeAgent:
    """Capture the bound UUID when start resolves the pending response."""
    agent.responses = [(is_status, lambda: report(agent.uuid))]
    return agent


@pytest.fixture
def agent() -> FakeAgent:
    return FakeAgent()


def observe(observer):
    observer.reconcile()
    observer.reconcile()


@pytest.fixture
def deployed_node(manager, observer, agent):
    """One control plane node with a current successful observation."""
    guest_ready(agent)
    node = manager.create(meta.ROLE_SERVER)
    observe(observer)
    return manager.get(node.name)


@pytest.fixture
def observer(manager: NodeManager, cfg: configmod.Config, agent: FakeAgent, monkeypatch):
    observer = Observer(manager, cfg, agent_factory=agent.for_node)
    monkeypatch.setattr(observer, "start", lambda: None)
    yield observer
    observer.shutdown()


# -- fault injection -------------------------------------------------------
#
# The failure that matters is a libvirt call that succeeds on the daemon and
# then raises on the way back: the domain really is running, but the client
# never learned it.  Compensation that assumed otherwise would delete a live
# VM's disk out from under it.


BEFORE = "before"  # the call never reaches libvirt
AFTER = "after"  # libvirt did the work; the client sees an error


@dataclass
class Faults:
    define: str | None = None
    volume: str | None = None
    start: str | None = None
    volume_delete: str | None = None

    def check(self, stage: str, when: str) -> None:
        if getattr(self, stage) == when:
            raise fake_libvirt_error(libvirt.VIR_ERR_INTERNAL_ERROR, f"injected {stage} {when}")


class _Proxy:
    def __init__(self, target, faults: Faults) -> None:
        object.__setattr__(self, "_target", target)
        object.__setattr__(self, "_faults", faults)

    def __getattr__(self, name):
        return getattr(self._target, name)


class DomainProxy(_Proxy):
    def create(self):
        self._faults.check("start", BEFORE)
        result = self._target.create()
        self._faults.check("start", AFTER)
        return result


class VolumeProxy(_Proxy):
    def delete(self, flags=0):
        self._faults.check("volume_delete", BEFORE)
        result = self._target.delete(flags)
        self._faults.check("volume_delete", AFTER)
        return result


class PoolProxy(_Proxy):
    def createXML(self, xml, flags=0):
        self._faults.check("volume", BEFORE)
        volume = self._target.createXML(xml, flags)
        self._faults.check("volume", AFTER)
        return VolumeProxy(volume, self._faults)

    def storageVolLookupByName(self, name):
        return VolumeProxy(self._target.storageVolLookupByName(name), self._faults)


class ConnProxy(_Proxy):
    def defineXML(self, xml):
        self._faults.check("define", BEFORE)
        dom = self._target.defineXML(xml)
        self._faults.check("define", AFTER)
        return DomainProxy(dom, self._faults)

    def storagePoolLookupByName(self, name):
        return PoolProxy(self._target.storagePoolLookupByName(name), self._faults)

    def storagePoolLookupByUUIDString(self, uuid):
        return PoolProxy(self._target.storagePoolLookupByUUIDString(uuid), self._faults)


@pytest.fixture
def faults() -> Faults:
    return Faults()


@pytest.fixture
def faulty_manager(cfg, marked_pool, faults, seed_upload) -> NodeManager:
    connection = ConnectionManager(
        cfg.libvirt.uri, opener=lambda uri: ConnProxy(libvirt.open(uri), faults)
    )
    connection.open()
    manager = NodeManager(connection, cfg)
    manager.join_ready = lambda node: True
    yield manager
    connection.close()


@pytest.fixture
def app_state(cfg, manager, observer, monkeypatch):
    from k3s_kvm_demo.app import AppState
    from k3s_kvm_demo.kubeconfig_export import KubeconfigExporter

    exporter = KubeconfigExporter(manager, cfg)
    monkeypatch.setattr(exporter, "start", lambda: None)

    return AppState(
        cfg=cfg,
        cm=manager.cm,
        manager=manager,
        observer=observer,
        exporter=exporter,
    )


@pytest.fixture
def client(cfg, app_state):
    from fastapi.testclient import TestClient

    from k3s_kvm_demo.app import create_app

    application = create_app(cfg, lambda: app_state)
    with TestClient(application) as test_client:
        yield test_client


def make_meta(**overrides) -> meta.NodeMeta:
    connection = libvirt.open(TEST_URI)
    try:
        pool_uuid = connection.storagePoolLookupByName(TEST_POOL).UUIDString()
    finally:
        connection.close()
    values = {
        "role": meta.ROLE_AGENT,
        "bootstrap": False,
        "volume": "k3s-node-" + "0" * 32 + ".qcow2",
        "seed_volume": "k3s-node-" + "0" * 32 + ".iso",
        "pool_uuid": pool_uuid,
        "scope_prefix": "k3s-node",
        "created": meta.now(),
        "generation": 1,
        "state": meta.CREATING,
        "error": None,
    }
    values.update(overrides)
    return meta.NodeMeta(**values)


def unmanaged_domain_xml(name: str, disk: str | None = None) -> str:
    disk_xml = (
        f"<disk type='file' device='disk'><source file='{disk}'/>"
        "<target dev='vda' bus='virtio'/></disk>"
        if disk
        else ""
    )
    return textwrap.dedent(
        f"""
        <domain type='test'>
          <name>{name}</name>
          <memory unit='MiB'>256</memory>
          <vcpu>1</vcpu>
          <os><type arch='x86_64'>hvm</type></os>
          <devices>{disk_xml}</devices>
        </domain>
        """
    ).strip()
