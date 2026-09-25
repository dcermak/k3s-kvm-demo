"""Real libvirt fixtures and scripted guest responses shared by export tests."""

import copy
import dataclasses
import json
import xml.etree.ElementTree as ET

import libvirt
import pytest
import yaml

from k3s_kvm_demo import guestexec, meta

from .conftest import TEST_POOL, FakeAgent, Invocation, guest_ready, observe, result

SECRET = "EXPORT-PRIVATE-KEY-SENTINEL"
KEY_DATA = SECRET + ' </textarea><script>secret()</script> & "quoted"'
DOCUMENT = {
    "apiVersion": "v1",
    "kind": "Config",
    "preferences": {"colors": True},
    "current-context": "selected-context",
    "clusters": [
        {"name": "untouched", "cluster": {"server": "https://other.example:7443"}},
        {
            "name": "selected-cluster",
            "cluster": {
                "server": "https://127.0.0.1:6443",
                "certificate-authority-data": "Y2EtZGF0YQ==",
                "tls-server-name": "k3s.example",
            },
        },
    ],
    "contexts": [
        {"name": "other-context", "context": {"cluster": "untouched", "user": "other"}},
        {
            "name": "selected-context",
            "context": {"cluster": "selected-cluster", "user": "admin", "namespace": "demo"},
        },
    ],
    "users": [
        {"name": "other", "user": {"token": "untouched-token"}},
        {
            "name": "admin",
            "user": {"client-certificate-data": "Y2VydA==", "client-key-data": KEY_DATA},
        },
    ],
    "extensions": [{"name": "preserve-me", "extension": {"nested": [1, "two", False]}}],
}
YAML = yaml.safe_dump(DOCUMENT, sort_keys=False)
FAILURES = [
    "missing-server", "failed-read", "empty", "malformed", "missing-context",
    "truncated", "agent-error",
]


@pytest.fixture
def config_values(config_values, tmp_path):
    values = copy.deepcopy(config_values)
    values["vm"]["max_nodes"] = 8
    return values


@pytest.fixture
def config_file(config_values, tmp_path):
    path = tmp_path / "export.toml"
    # These fixture values are strings, numbers and lists, valid in JSON and TOML.
    path.write_text(
        "\n".join(
            f"[{section}]\n"
            + "\n".join(f"{key} = {json.dumps(value)}" for key, value in fields.items())
            for section, fields in config_values.items()
        ),
        encoding="utf-8",
    )
    return path


@pytest.fixture
def export_agent(monkeypatch, cfg):
    agent = FakeAgent(default=result(stdout=YAML))

    def factory(cm, uuid, *, timeout_s):
        assert cm.uri == "test:///default"
        assert timeout_s == cfg.observation.qga_timeout_s
        return agent.for_node(uuid, timeout_s)

    monkeypatch.setattr(guestexec, "QemuAgent", factory)
    return agent


@pytest.fixture
def candidate_cluster(deployed_node, manager, observer, agent, conn, request):
    worker = manager.create(meta.ROLE_AGENT)
    stopped = manager.create(meta.ROLE_SERVER)
    booting = manager.create(meta.ROLE_SERVER)
    ipless = manager.create(meta.ROLE_SERVER)
    alternate = manager.create(meta.ROLE_SERVER)
    guest_ready(agent)
    observe(observer)

    eligible = sorted([deployed_node, alternate], key=lambda node: node.name)
    by_name = getattr(request, "param", "age") == "name"
    selected = eligible[0] if by_name else eligible[1]
    for node in (worker, stopped, booting, ipless, *eligible):
        dom = conn.lookupByUUIDString(node.uuid)
        created = "2024-01-01T00:00:00+00:00"
        if node in eligible:
            # Age ordering deliberately disagrees with lexicographic name ordering.
            day = 2 if by_name or node == selected else 3
            created = f"2024-01-0{day}T00:00:00+00:00"
        record = meta.read(dom)
        meta.write(
            dom,
            dataclasses.replace(
                record, created=created, state=meta.BOOTING if node == booting else record.state
            ),
        )

    conn.lookupByUUIDString(stopped.uuid).destroy()
    dom = conn.lookupByUUIDString(ipless.uuid)
    root = ET.fromstring(dom.XMLDesc(libvirt.VIR_DOMAIN_XML_INACTIVE))
    devices = root.find("devices")
    for interface in devices.findall("interface"):
        devices.remove(interface)
    dom.destroy()
    conn.defineXML(ET.tostring(root, encoding="unicode")).create()

    nodes = {node.uuid: node for node in manager.list_nodes()}
    assert len(nodes) == 6
    assert nodes[worker.uuid].state == meta.CONFIGURED and not nodes[worker.uuid].is_server
    assert not nodes[stopped.uuid].running
    assert nodes[booting.uuid].state == meta.BOOTING and nodes[booting.uuid].ip
    assert nodes[ipless.uuid].running and nodes[ipless.uuid].ip is None
    assert all(
        nodes[node.uuid].running
        and nodes[node.uuid].ip
        and nodes[node.uuid].state == meta.CONFIGURED
        for node in eligible
    )
    return nodes[selected.uuid]


def expected_document(node):
    expected = copy.deepcopy(DOCUMENT)
    expected["clusters"][1]["cluster"]["server"] = node.api_url
    return expected


def assert_guest_read(agent, node):
    assert agent.uuid == node.uuid
    assert agent.calls == [
        Invocation("/usr/bin/timeout", ["10", "/bin/cat", "/etc/rancher/k3s/k3s.yaml"], None)
    ]


def inventory(conn):
    """Include live and persistent metadata, power state, and volume definitions."""
    return (
        {
            dom.UUIDString(): (
                dom.XMLDesc(0),
                dom.XMLDesc(libvirt.VIR_DOMAIN_XML_INACTIVE),
                dom.isActive(),
            )
            for dom in conn.listAllDomains(0)
        },
        {
            volume.name(): volume.XMLDesc(0)
            for volume in conn.storagePoolLookupByName(TEST_POOL).listAllVolumes(0)
        },
    )


def script_failure(case, agent, node, conn):
    if case == "missing-server":
        conn.lookupByUUIDString(node.uuid).destroy()
        return "no running, configured control plane node"
    if case == "failed-read":
        agent.default = result(1, stdout=KEY_DATA, stderr=KEY_DATA)
        return "could not read kubeconfig"
    if case == "malformed":
        agent.default = result(stdout=f"client-key-data: [{KEY_DATA}\n")
        return "invalid kubeconfig"
    if case == "empty":
        agent.default = result(stdout="\n")
        return "empty or truncated kubeconfig"
    if case == "missing-context":
        document = copy.deepcopy(DOCUMENT)
        document["current-context"] = "absent"
        agent.default = result(stdout=yaml.safe_dump(document))
        return "invalid kubeconfig"
    if case == "truncated":
        # Still valid YAML, so this must be rejected before parsing can accept it.
        agent.default = result(stdout=YAML + "\n# " + guestexec.TRUNCATION_NOTE)
        return "truncated kubeconfig"
    assert case == "agent-error"

    def fail():
        raise guestexec.AgentError(KEY_DATA)

    agent.responses = [(lambda _path, _args: True, fail)]
    return "could not read kubeconfig"
