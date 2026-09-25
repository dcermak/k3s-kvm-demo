"""Setup and cleanup for the opt-in KVM test."""

from __future__ import annotations

import fcntl
import os
import secrets
import shutil
import subprocess
import tempfile
import xml.etree.ElementTree as ET
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

import pytest

from k3s_kvm_demo import config, domxml, pool
from k3s_kvm_demo.conn import ConnectionManager
from k3s_kvm_demo.libvirtctl import NodeManager
from k3s_kvm_demo.observer import Observer


@dataclass
class PoolDirectory:
    path: Path
    preserve: bool = False


def _label_directory(path):
    # Apply labels in permissive mode too, before enforcement is restored.
    if Path("/sys/fs/selinux/enforce").exists():
        try:
            context = os.getxattr("/var/lib/libvirt/images", "security.selinux")
            os.setxattr(path, "security.selinux", context)
        except OSError as error:
            raise ValueError(f"Cannot apply the libvirt storage SELinux label to {path}") from error


@contextmanager
def _pool_directory(path):
    created = path is None
    if created:
        path = Path(tempfile.mkdtemp(prefix="k3sit-", dir="/var/tmp")).resolve()
    elif not path.is_absolute() or not path.is_dir() or path.resolve() != path:
        pytest.fail("K3S_DEMO_TEST_POOL_PATH must be an existing absolute, symlink-free directory")
    directory = PoolDirectory(path)
    fd = None
    try:
        if created:
            path.chmod(0o755)
            _label_directory(path)
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if any(path.iterdir()):
            pytest.fail("K3S_DEMO_TEST_POOL_PATH must be empty; nothing was removed")
        yield directory
    finally:
        try:
            if directory.preserve:
                print(f"Preserving VM test pool directory: {path}", flush=True)
            elif created:
                path.rmdir()
        finally:
            if fd is not None:
                os.close(fd)


@contextmanager
def _staged_image(source, pool_directory):
    directory = Path(tempfile.mkdtemp(prefix="k3sit-", dir="/var/tmp")).resolve()
    image = directory / "k3s-image.qcow2"
    try:
        directory.chmod(0o755)
        _label_directory(directory)
        # A separate inode prevents libvirt chown/relabel from affecting the source.
        subprocess.run(
            ["cp", "--reflink=auto", "--sparse=always", "--", str(source), str(image)],
            check=True,
        )
        image.chmod(0o644)
        print(f"VM test backing image: source={source} copy={image}", flush=True)
        yield image
    finally:
        if pool_directory.preserve:
            print(f"Preserving VM test backing image: {image}", flush=True)
        else:
            image.unlink(missing_ok=True)
            directory.rmdir()


@pytest.fixture
def vm_cluster():
    if shutil.which("kubectl") is None:
        pytest.fail("real-VM export verification requires kubectl on the host PATH")

    def setting(key, default=None):
        value = os.environ.get(key, default)
        if value is not None and not value.strip():
            pytest.fail(f"{key} must not be empty")
        return value

    root = Path(__file__).resolve().parents[2]
    source = Path(setting("K3S_DEMO_TEST_IMAGE", str(root / "k3s-image.qcow2")))
    if not source.is_absolute() or not source.is_file():
        pytest.fail(f"K3S_DEMO_TEST_IMAGE must be an existing absolute image path: {source}. "
                    "Build the guest as described in image/Readme.md.")
    pool_path = setting("K3S_DEMO_TEST_POOL_PATH")
    network_name = setting("K3S_DEMO_TEST_NETWORK", config.LibvirtConfig.network)
    probe_image = setting(
        "K3S_DEMO_TEST_PROBE_IMAGE", "registry.opensuse.org/opensuse/bci/python:3.13"
    )
    if any(character.isspace() for character in probe_image):
        pytest.fail("K3S_DEMO_TEST_PROBE_IMAGE must be an image reference without whitespace")

    with (
        _pool_directory(Path(pool_path) if pool_path is not None else None) as directory,
        _staged_image(source.resolve(), directory) as image,
    ):
        target = directory.path
        resource_paths = (target, image.parent)
        if image.is_relative_to(target):
            pytest.fail("the backing image must be outside the test pool")
        scope = "k3sit-" + uuid4().hex[:12]
        cfg = config.from_mapping({
            "libvirt": {"uri": "qemu:///system", "pool": scope, "network": network_name},
            "vm": {
                "base_image": str(image), "name_prefix": scope, "max_nodes": 2,
                "disk_gb": 24, "memory_mb": 4096, "vcpus": 4,
            },
            "cluster": {"token": secrets.token_hex(32)},
        })
        cm = ConnectionManager(cfg.libvirt.uri)
        storage = None
        storage_uuid = None
        manager = NodeManager(cm, cfg)
        observer = Observer(manager, cfg)
        try:
            conn = cm.open()
            assert conn.getType() == "QEMU", "VM tests require the QEMU driver"
            manager.check_compatible()
            assert manager.domain_type(conn) == domxml.TYPE_KVM, "VM tests require KVM"
            network = conn.networkLookupByName(cfg.libvirt.network)
            assert network.isActive(), f"test network {network_name!r} must already be active"
            for existing in conn.listAllStoragePools(0):
                assert existing.name() != scope, "unexpected test pool name collision"
                path = ET.fromstring(existing.XMLDesc(0)).findtext("./target/path")
                if path:
                    existing_target = Path(path).resolve()
                    assert not any(
                        resource.is_relative_to(existing_target)
                        or existing_target.is_relative_to(resource)
                        for resource in resource_paths
                    ), f"test storage overlaps existing pool {existing.name()}"
            assert not any(
                Path(path).is_relative_to(resource)
                for path in pool.referenced_disk_paths(conn)
                for resource in resource_paths
            ), "an existing domain references the test storage"
            assert not any(dom.name().startswith(scope + "-") for dom in conn.listAllDomains(0))

            print(f"VM test resources: pool={scope} prefix={scope} path={target}", flush=True)
            print(f"VM test network={network_name} probe={probe_image}", flush=True)
            # Preserve both directories if definition succeeds but its reply is lost.
            directory.preserve = True
            storage = conn.storagePoolDefineXML(pool.build_pool_xml(scope, target), 0)
            storage_uuid = storage.UUIDString()
            print(f"VM test pool UUID: {storage_uuid}", flush=True)
            storage.create(0)
            storage.refresh(0)
            assert not storage.listAllVolumes(0)
            storage.createXML(pool.build_marker_xml(), 0)
            config.validate_hypervisor(conn, cfg)
            yield manager, observer, probe_image
        finally:
            try:
                assert observer.shutdown(), "observer is still running; preserve test resources"
                if storage is not None:
                    assert storage.UUIDString() == storage_uuid
                    assert domxml.pool_target_path(storage) == target
                    manager.reset()
                    assert not manager.list_nodes()
                    with cm.lease() as (conn, _):
                        assert not any(
                            Path(path).is_relative_to(resource)
                            for path in pool.referenced_disk_paths(conn)
                            for resource in resource_paths
                        ), "a domain still references the test storage; preserving it"
                    if storage.isActive():
                        storage.refresh(0)
                        remaining = {volume.name() for volume in storage.listAllVolumes(0)}
                        assert remaining <= {pool.MARKER_VOLUME}, (
                            f"unexpected volumes remain in {scope}; preserving pool: {remaining}"
                        )
                        if pool.MARKER_VOLUME in remaining:
                            storage.storageVolLookupByName(pool.MARKER_VOLUME).delete(0)
                        storage.destroy()
                    storage.undefine()
                    assert not any(target.iterdir()), "test directory is not empty after cleanup"
                    directory.preserve = False
            finally:
                cm.close()


@pytest.fixture(autouse=True)
def libvirt_clean():
    """Override tests/conftest.py: never open or wipe the default test pool."""
    yield
