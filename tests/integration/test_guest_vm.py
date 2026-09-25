"""Explicitly enabled v2 image proof against local QEMU/KVM."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import time
import xml.etree.ElementTree as ET
from dataclasses import asdict
from pathlib import Path
from uuid import uuid4

import pytest

from k3s_kvm_demo import cli, domxml, guestexec, meta, pool

pytestmark = pytest.mark.skipif(
    os.environ.get("K3S_DEMO_VM_TESTS") != "1",
    reason="real VMs require K3S_DEMO_VM_TESTS=1; see tests/integration/Readme.md",
)

PROBE = """import ipaddress
import os
import socket
import stat
import tempfile
import time

assert os.getuid() == os.getgid() == 10001, "probe must run as UID/GID 10001"
for path in ("/", "/tmp", "/etc/hosts", "/etc/resolv.conf"):
    print(path, oct(stat.S_IMODE(os.stat(path).st_mode)), flush=True)
for path in ("/etc/hosts", "/etc/resolv.conf"):
    with open(path) as stream:
        assert stream.read().strip(), path + " is empty"
with tempfile.TemporaryDirectory(dir="/tmp") as directory:
    path = directory + "/probe"
    with open(path, "w") as stream:
        stream.write("nonroot-write")
    with open(path) as stream:
        assert stream.read() == "nonroot-write"
assert not os.path.exists(directory), "temporary file cleanup failed"
local = {entry[4][0] for entry in socket.getaddrinfo("localhost", 9898)}
assert local and all(ipaddress.ip_address(ip).is_loopback for ip in local), local
print("filesystem and localhost checks passed", flush=True)
expected = os.environ["API_SERVICE_IP"]
deadline = time.monotonic() + 60
while True:
    try:
        addresses = {entry[4][0] for entry in socket.getaddrinfo(
            "kubernetes.default.svc.cluster.local", 443)}
        assert expected in addresses, (expected, addresses)
        break
    except (OSError, AssertionError) as error:
        if time.monotonic() >= deadline:
            raise
        print("waiting for cluster DNS:", error, flush=True)
        time.sleep(2)
print("nonroot-probe-ok", flush=True)
"""


def kubectl(kubeconfig, *args, manifest=None, timeout=30):
    result = subprocess.run(
        ["kubectl", "--kubeconfig", str(kubeconfig), "--request-timeout=15s", *args],
        input=json.dumps(manifest) if manifest is not None else None,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


def verify_nonroot_workloads(manager, nodes, kubeconfig, probe_image):
    namespace = "nonroot-" + uuid4().hex[:12]
    pod_names = ["probe-server", "probe-agent"]
    namespace_created = False
    passed = False
    runtime_masks = {}
    try:
        for node in nodes:
            result = guestexec.run(
                guestexec.QemuAgent(manager.cm, node.uuid, timeout_s=2),
                "/usr/bin/timeout",
                ["10", "/bin/sh", "-ec", 'pids=$(pgrep -x containerd); '
                 'for pid in $pids; do '
                 'printf "pid=%s " "$pid"; grep "^Umask:" "/proc/$pid/status"; done'],
                timeout_s=15,
            )
            assert result.ok, result.stderr
            runtime_masks[node.name] = result.stdout
            print(f"{node.name} containerd: {result.stdout}", flush=True)

        service = json.loads(kubectl(kubeconfig, "get", "service", "kubernetes", "-o", "json"))
        kubectl(kubeconfig, "create", "namespace", namespace)
        namespace_created = True
        for node, name in zip(nodes, pod_names, strict=True):
            kubectl(kubeconfig, "create", "-f", "-", manifest={
                "apiVersion": "v1", "kind": "Pod",
                "metadata": {"name": name, "namespace": namespace},
                "spec": {
                    "nodeName": node.name,
                    "restartPolicy": "Never",
                    "activeDeadlineSeconds": 240,
                    "automountServiceAccountToken": False,
                    "securityContext": {
                        "runAsUser": 10001, "runAsGroup": 10001, "runAsNonRoot": True,
                        "seccompProfile": {"type": "RuntimeDefault"},
                    },
                    "containers": [{
                        "name": "probe",
                        "image": probe_image,
                        "imagePullPolicy": "IfNotPresent",
                        "command": ["python3", "-I", "-u", "-c", PROBE],
                        "env": [{"name": "API_SERVICE_IP", "value": service["spec"]["clusterIP"]}],
                        "securityContext": {
                            "allowPrivilegeEscalation": False, "capabilities": {"drop": ["ALL"]},
                        },
                    }],
                },
            })

        deadline = time.monotonic() + 300
        while time.monotonic() < deadline:
            pods = json.loads(kubectl(kubeconfig, "get", "pods", "-n", namespace, "-o", "json"))[
                "items"
            ]
            for pod in pods:
                status = pod.get("status", {})
                assert status.get("phase") != "Failed", (pod["metadata"]["name"], status)
            if len(pods) == 2 and all(pod["status"].get("phase") == "Succeeded" for pod in pods):
                break
            time.sleep(2)
        else:
            pytest.fail("non-root probes did not succeed on both nodes within 300 s; "
                        "see diagnostics")

        for pod in pods:
            image_id = pod["status"]["containerStatuses"][0]["imageID"]
            print(f"{pod['metadata']['name']} imageID: {image_id}", flush=True)
            output = kubectl(kubeconfig, "logs", "-n", namespace, pod["metadata"]["name"])
            print(f"{pod['metadata']['name']}:\n{output}", flush=True)
            assert "nonroot-probe-ok" in output
        kubectl(kubeconfig, "rollout", "status", "deployment/coredns", "-n", "kube-system",
                "--timeout=60s", timeout=90)
        # Check runtime state after functional checks so it cannot replace them.
        for name, output in runtime_masks.items():
            masks = re.findall(r"Umask:\s+([0-7]+)", output)
            assert masks and all(int(mask, 8) == 0o022 for mask in masks), (name, output)
        passed = True
    finally:
        if not passed:
            print(f"containerd umasks: {runtime_masks}", flush=True)
            commands = [
                ("get", "pods", "-A", "-o", "wide"),
                ("get", "deployment/coredns", "-n", "kube-system", "-o", "wide"),
                ("describe", "pods", "-n", "kube-system", "-l", "k8s-app=kube-dns"),
                ("logs", "-n", "kube-system", "-l", "k8s-app=kube-dns", "--tail=100"),
            ]
            if namespace_created:
                commands.extend([
                    ("describe", "pods", "-n", namespace),
                    ("get", "events", "-n", namespace, "--sort-by=.metadata.creationTimestamp"),
                    *(("logs", "-n", namespace, name, "--tail=100") for name in pod_names),
                ])
            for args in commands:
                try:
                    print(f"{args}:\n{kubectl(kubeconfig, *args)}", flush=True)
                except (AssertionError, OSError, subprocess.TimeoutExpired) as error:
                    print(f"diagnostic unavailable for {args}: {error}", flush=True)
        if namespace_created:
            try:
                kubectl(kubeconfig, "delete", "namespace", namespace, "--wait=false")
            except (AssertionError, OSError, subprocess.TimeoutExpired) as error:
                if passed:
                    raise
                print(f"namespace cleanup failed (VM teardown will follow): {error}", flush=True)


def test_server_and_agent_boot_from_seed(vm_cluster, tmp_path):
    manager, observer, probe_image = vm_cluster
    created = []
    for role in (meta.ROLE_SERVER, meta.ROLE_AGENT):
        node = manager.create(role)
        created.append(node)
        print(f"VM test node: name={node.name} uuid={node.uuid}", flush=True)
        deadline = time.monotonic() + 600
        while time.monotonic() < deadline:
            observer.reconcile()
            current = manager.get(node.name)
            assert current.state != meta.FAILED, f"{current.name}: {current.error}"
            if current.state == meta.CONFIGURED and current.ip and observer.join_ready(current):
                break
            time.sleep(manager.cfg.observation.interval_s)
        else:
            pytest.fail(f"guest did not become configured and active within 600 s: {current}")

        wrapper = Path(__file__).resolve().parents[2] / "image/k3s-demo-guest"
        expected_hash = hashlib.sha256(wrapper.read_bytes()).hexdigest()
        result = guestexec.run(
            guestexec.QemuAgent(manager.cm, node.uuid, timeout_s=2),
            "/usr/bin/timeout",
            ["10", "sha256sum", "/usr/local/libexec/k3s-demo-guest"],
            timeout_s=15,
        )
        assert result.ok, result.stderr
        print(f"{node.name} installed wrapper: {result.stdout.strip()}", flush=True)
        assert result.stdout.split()[0] == expected_hash, (
            f"{node.name}: stale guest wrapper; rebuild K3S_DEMO_TEST_IMAGE from this working tree"
        )

        with manager.cm.lease() as (conn, _):
            dom = conn.lookupByUUIDString(node.uuid)
            disks = ET.fromstring(dom.XMLDesc(0)).findall("./devices/disk")
            assert len(disks) == 2
            seed_disk = next(disk for disk in disks if disk.find("readonly") is not None)
            assert seed_disk.find("target").get("bus") == "virtio"
            assert seed_disk.find("source").get("file") == str(
                domxml.pool_target_path(conn.storagePoolLookupByName(manager.cfg.libvirt.pool))
                / node.seed_volume
            )

    # Actual Kubernetes evidence, separate from systemd's historical started marker.
    agent = guestexec.QemuAgent(manager.cm, created[0].uuid, timeout_s=2)
    deadline = time.monotonic() + 600
    expected_names = {node.name for node in created}
    while time.monotonic() < deadline:
        result = guestexec.run(
            agent,
            "/usr/bin/env",
            [
                "k3s",
                "kubectl",
                "--kubeconfig",
                "/etc/rancher/k3s/k3s.yaml",
                "--request-timeout=10s",
                "get",
                "nodes",
                "-o",
                "json",
            ],
            timeout_s=20,
        )
        if result.ok:
            nodes = json.loads(result.stdout)["items"]
            ready = {
                node["metadata"]["name"]
                for node in nodes
                if any(
                    condition["type"] == "Ready" and condition["status"] == "True"
                    for condition in node.get("status", {}).get("conditions", [])
                )
            }
            if ready == expected_names:
                break
        time.sleep(2)
    else:
        pytest.fail("both test nodes did not become Kubernetes Ready within 600 s")

    # Cross the host/guest boundary using the same CLI an operator runs.
    config_path = tmp_path / "export.toml"
    fd = os.open(config_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        for section, fields in asdict(manager.cfg).items():
            if section == "source":
                continue
            stream.write(f"[{section}]\n")
            for key, value in fields.items():
                stream.write(f"{key} = {json.dumps(value, default=str)}\n")
    exported = tmp_path / "k3s-demo.yaml"
    assert cli.main(["-c", str(config_path), "export", "-o", str(exported)]) == 0
    assert exported.stat().st_mode & 0o777 == 0o600
    nodes = json.loads(kubectl(exported, "get", "nodes", "-o", "json"))["items"]
    assert {node["metadata"]["name"] for node in nodes} == expected_names

    verify_nonroot_workloads(manager, created, exported, probe_image)

    assert manager.reset() == 2
    assert manager.list_nodes() == []
    with manager.cm.lease() as (conn, _):
        remaining = conn.storagePoolLookupByName(manager.cfg.libvirt.pool).listAllVolumes(0)
        assert {volume.name() for volume in remaining} == {pool.MARKER_VOLUME}
