"""Domain and volume XML.

Everything is built with ElementTree rather than string formatting, so a token,
path or hostname containing ``<``, ``&`` or a quote cannot break the document.
"""

from __future__ import annotations

import platform
import xml.etree.ElementTree as ET
from pathlib import Path

import libvirt

from . import meta
from .config import Config

TYPE_TEST = "test"
TYPE_KVM = "kvm"
TYPE_QEMU = "qemu"

GUEST_AGENT_CHANNEL = "org.qemu.guest_agent.0"


class UnsupportedHypervisor(Exception):
    """The connection cannot run the guests we need."""


def select_domain_type(conn: libvirt.virConnect, *, arch: str | None = None) -> str:
    """Decide the ``<domain type=...>`` value for this connection.

    ``conn.getType()`` cannot answer this — it returns ``QEMU`` for both KVM and
    plain TCG emulation — and ``getDomainCapabilities`` reports ``kvm`` even on
    the test driver.  The capabilities XML is the reliable source.
    """
    if conn.getType() == "TEST":
        return TYPE_TEST

    arch = arch or platform.machine()
    root = ET.fromstring(conn.getCapabilities())
    available: set[str] = set()
    for guest in root.findall("guest"):
        if (guest.findtext("os_type") or "") != "hvm":
            continue
        guest_arch = guest.find("arch")
        if guest_arch is None or guest_arch.get("name") != arch:
            continue
        available.update(d.get("type") or "" for d in guest_arch.findall("domain"))

    if TYPE_KVM in available:
        return TYPE_KVM
    if TYPE_QEMU in available:
        return TYPE_QEMU
    raise UnsupportedHypervisor(
        f"no hvm guest for arch {arch!r} offers a kvm or qemu domain type; "
        f"capabilities advertise {sorted(available) or 'nothing usable'}"
    )


def pool_target_path(pool: libvirt.virStoragePool) -> Path:
    root = ET.fromstring(pool.XMLDesc(0))
    path = root.findtext("target/path")
    if not path:
        raise UnsupportedHypervisor(f"storage pool {pool.name()!r} has no target path")
    return Path(path)


def volume_path(pool: libvirt.virStoragePool, volume: str) -> Path:
    """Where a volume *will* live, computable before it is created."""
    return pool_target_path(pool) / volume


def build_volume_xml(cfg: Config, *, volume: str) -> str:
    root = ET.Element("volume", {"type": "file"})
    ET.SubElement(root, "name").text = volume
    ET.SubElement(root, "capacity", {"unit": "bytes"}).text = str(cfg.vm.disk_bytes)
    ET.SubElement(root, "allocation", {"unit": "bytes"}).text = "0"

    target = ET.SubElement(root, "target")
    ET.SubElement(target, "format", {"type": "qcow2"})

    # The backing format must be explicit: libvirt refuses to probe it, and a
    # missing <format> here is the classic cause of an unbootable overlay.
    backing = ET.SubElement(root, "backingStore")
    ET.SubElement(backing, "path").text = str(cfg.vm.base_image)
    ET.SubElement(backing, "format", {"type": "qcow2"})

    return ET.tostring(root, encoding="unicode")


def build_seed_volume_xml(*, volume: str, capacity: int) -> str:
    root = ET.Element("volume", {"type": "file"})
    ET.SubElement(root, "name").text = volume
    ET.SubElement(root, "capacity", {"unit": "bytes"}).text = str(capacity)
    target = ET.SubElement(root, "target")
    ET.SubElement(target, "format", {"type": "raw"})
    return ET.tostring(root, encoding="unicode")


def build_domain_xml(
    cfg: Config,
    *,
    name: str,
    uuid: str,
    node_meta: meta.NodeMeta,
    domain_type: str,
    disk_path: Path,
    seed_path: Path,
    arch: str | None = None,
) -> str:
    arch = arch or ("x86_64" if domain_type == TYPE_TEST else platform.machine())
    emulated = domain_type != TYPE_TEST

    root = ET.Element("domain", {"type": domain_type})
    ET.SubElement(root, "name").text = name
    ET.SubElement(root, "uuid").text = uuid

    ET.SubElement(root, "metadata").append(meta.build_element(node_meta, qualified=True))

    ET.SubElement(root, "memory", {"unit": "MiB"}).text = str(cfg.vm.memory_mb)
    ET.SubElement(root, "currentMemory", {"unit": "MiB"}).text = str(cfg.vm.memory_mb)
    ET.SubElement(root, "vcpu", {"placement": "static"}).text = str(cfg.vm.vcpus)

    os_el = ET.SubElement(root, "os")
    os_attrs = {"arch": arch}
    if emulated:
        os_attrs["machine"] = "q35"
    ET.SubElement(os_el, "type", os_attrs).text = "hvm"
    ET.SubElement(os_el, "boot", {"dev": "hd"})

    if emulated:
        features = ET.SubElement(root, "features")
        ET.SubElement(features, "acpi")
        ET.SubElement(features, "apic")
        ET.SubElement(root, "cpu", {"mode": "host-passthrough", "check": "none"})

    # A killed node must stay dead; nothing here should reboot itself.
    ET.SubElement(root, "on_poweroff").text = "destroy"
    ET.SubElement(root, "on_reboot").text = "restart"
    ET.SubElement(root, "on_crash").text = "destroy"

    devices = ET.SubElement(root, "devices")

    disk = ET.SubElement(devices, "disk", {"type": "file", "device": "disk"})
    ET.SubElement(disk, "driver", {"name": "qemu", "type": "qcow2"})
    ET.SubElement(disk, "source", {"file": str(disk_path)})
    ET.SubElement(disk, "target", {"dev": "vda", "bus": "virtio"})
    # Config validation requires a standalone base image, so the chain ends here.
    backing = ET.SubElement(disk, "backingStore", {"type": "file", "index": "1"})
    ET.SubElement(backing, "format", {"type": "qcow2"})
    ET.SubElement(backing, "source", {"file": str(cfg.vm.base_image)})
    ET.SubElement(backing, "backingStore")

    seed = ET.SubElement(devices, "disk", {"type": "file", "device": "disk"})
    ET.SubElement(seed, "driver", {"name": "qemu", "type": "raw"})
    ET.SubElement(seed, "source", {"file": str(seed_path)})
    ET.SubElement(seed, "target", {"dev": "vdb", "bus": "virtio"})
    ET.SubElement(seed, "readonly")

    iface = ET.SubElement(devices, "interface", {"type": "network"})
    ET.SubElement(iface, "source", {"network": cfg.libvirt.network})
    ET.SubElement(iface, "model", {"type": "virtio"})

    # Used for observation only; provisioning belongs to the guest.
    channel = ET.SubElement(devices, "channel", {"type": "unix"})
    ET.SubElement(channel, "target", {"type": "virtio", "name": GUEST_AGENT_CHANNEL})

    if emulated:
        serial = ET.SubElement(devices, "serial", {"type": "pty"})
        ET.SubElement(serial, "target", {"port": "0"})
        console = ET.SubElement(devices, "console", {"type": "pty"})
        ET.SubElement(console, "target", {"type": "serial", "port": "0"})

        rng = ET.SubElement(devices, "rng", {"model": "virtio"})
        backend = ET.SubElement(rng, "backend", {"model": "random"})
        backend.text = "/dev/urandom"

    # Needed for the memory figures stats.py will report later.
    ET.SubElement(devices, "memballoon", {"model": "virtio"})

    return ET.tostring(root, encoding="unicode")
