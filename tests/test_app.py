"""The HTTP surface.

Every mutating route re-renders the same grid, so most of these assertions are
about what the operator sees: a refusal has to arrive as a readable message,
not a 500.
"""

from __future__ import annotations

import base64
import dataclasses
import re
import subprocess
import sys
import textwrap
import xml.etree.ElementTree as ET

import libvirt
import pytest

from k3s_kvm_demo import guestexec, libvirtctl, meta
from k3s_kvm_demo.app import Flashes
from k3s_kvm_demo.observer import GUEST_PATH, Observer

from .conftest import (
    REPO_ROOT,
    add_volume,
    guest_ready,
    is_status,
    observe,
    report,
    unmanaged_domain_xml,
    volumes,
)

#: An overlay in the pool that no domain claims.
ORPHAN = "k3s-node-" + "8" * 32 + ".qcow2"


@pytest.fixture(autouse=True)
def guest_succeeds(agent):
    guest_ready(agent)


def test_the_page_renders_with_an_empty_grid(client):
    page = client.get("/")
    assert page.status_code == 200
    assert 'id="grid"' in page.text
    assert 'hx-trigger="every 2s"' in page.text
    assert "Deploy Control Plane node" in page.text
    assert "No nodes yet" in page.text
    assert '<script src="/static/notifications.js" defer></script>' in page.text


def test_static_assets_are_served_locally(client):
    script = client.get("/static/htmx.min.js")
    assert script.status_code == 200
    assert b"htmx" in script.content
    assert client.get("/static/app.css").status_code == 200
    assert client.get("/static/notifications.js").status_code == 200


@pytest.mark.parametrize("level", ["info", "error"])
def test_notifications_have_stable_ids_and_still_expire(client, app_state, level):
    now = 0.0
    app_state.flashes = Flashes(clock=lambda: now)
    app_state.flashes.add(level, "same message")
    first = client.get("/nodes").text
    ids = re.findall(r'data-notification-id="([^"]+)"', first)
    assert len(ids) == 1
    assert '<button type="button" class="flash-dismiss" data-dismiss-notification' in first
    assert 'aria-label="Dismiss notification">&times;</button>' in first

    now = 2.0
    assert re.findall(r'data-notification-id="([^"]+)"', client.get("/nodes").text) == ids
    app_state.flashes.add(level, "same message")
    repeated = re.findall(r'data-notification-id="([^"]+)"', client.get("/nodes").text)
    assert len(repeated) == 2
    assert repeated[0] == ids[0]
    assert repeated[1] != ids[0], "a later identical message must not inherit dismissal"

    now = 12.0
    assert re.findall(r'data-notification-id="([^"]+)"', client.get("/nodes").text) == repeated[1:]
    now = 14.0
    assert 'data-notification-id=' not in client.get("/nodes").text


def test_deploying_a_control_plane_node_shows_it_in_the_grid(client, manager):
    response = client.post("/deploy/server")
    assert response.status_code == 200

    node = manager.list_nodes()[0]
    assert node.role == meta.ROLE_SERVER
    assert node.bootstrap is True
    assert node.short_name in response.text
    assert node.name in response.text  # the full name is kept in a title
    assert node.state == meta.BOOTING


def test_the_grid_partial_is_what_mutations_return(client):
    client.post("/deploy/server")
    listing = client.get("/nodes")
    assert listing.status_code == 200
    assert listing.text.lstrip().startswith('<div id="grid"')
    assert "<html" not in listing.text


def test_an_agent_before_any_server_is_a_message_not_a_crash(client, manager):
    response = client.post("/deploy/agent")
    assert response.status_code == 200
    assert "no control plane node" in response.text
    assert manager.list_nodes() == []


def test_an_unknown_role_is_rejected_politely(client):
    response = client.post("/deploy/bogus")
    assert response.status_code == 200
    assert "unknown role" in response.text


def test_the_node_limit_is_reported_rather_than_raised(client, manager, cfg, observer):
    client.post("/deploy/server")
    observe(observer)
    for _ in range(cfg.vm.max_nodes - 1):
        client.post("/deploy/agent")

    assert len(manager.list_nodes()) == cfg.vm.max_nodes
    response = client.post("/deploy/agent")
    assert response.status_code == 200
    assert "node limit" in response.text
    assert len(manager.list_nodes()) == cfg.vm.max_nodes


def test_killing_a_node_removes_it_from_the_next_poll(client, manager):
    client.post("/deploy/server")
    node = manager.list_nodes()[0]

    response = client.post(f"/nodes/{node.name}/kill")
    assert response.status_code == 200
    assert manager.list_nodes() == []

    # The card is gone; only the transient "killed ..." flash still names it.
    later = client.get("/nodes").text
    assert node.name not in later
    assert "Kill node" not in later
    assert "No nodes yet" in later


def test_killing_an_unknown_node_is_a_404(client):
    assert client.post("/nodes/k3s-node-" + "0" * 32 + "/kill").status_code == 404
    assert client.post("/nodes/someone-elses-vm/kill").status_code == 404


def test_reset_clears_everything_and_says_how_much(client, manager, observer, conn):
    client.post("/deploy/server")
    observe(observer)
    client.post("/deploy/agent")
    assert len(manager.list_nodes()) == 2

    response = client.post("/reset")
    assert "removed 2 node(s)" in response.text
    assert manager.list_nodes() == []
    assert volumes(conn, "k3s-node-") == set()


@pytest.mark.parametrize("operation", ["kill", "reset"])
def test_cleanup_accepts_unrelated_source_only_qcow2(client, manager, conn, operation):
    node = manager.create(meta.ROLE_SERVER)
    root = ET.fromstring(
        unmanaged_domain_xml("opensusetumbleweed", disk="/foreign/root-owned.qcow2")
    )
    ET.SubElement(root.find("./devices/disk"), "driver", {"name": "qemu", "type": "qcow2"})
    foreign = conn.defineXML(ET.tostring(root, encoding="unicode"))
    before = foreign.XMLDesc(libvirt.VIR_DOMAIN_XML_INACTIVE)
    assert ET.fromstring(before).find("./devices/disk/backingStore") is None

    response = client.post(f"/nodes/{node.name}/kill" if operation == "kill" else "/reset")

    assert response.status_code == 200
    assert (f"killed {node.short_name}" if operation == "kill" else "removed 1 node(s)") in (
        response.text
    )
    assert manager.list_nodes() == []
    assert node.uuid not in {dom.UUIDString() for dom in conn.listAllDomains(0)}
    assert not {node.volume, node.seed_volume} & volumes(conn)
    assert foreign.isPersistent()
    assert not foreign.isActive()
    assert foreign.XMLDesc(libvirt.VIR_DOMAIN_XML_INACTIVE) == before


def test_the_quorum_banner_is_marked_as_advisory(client, manager, observer):
    client.post("/deploy/server")
    observe(observer)
    client.post("/deploy/server")  # two servers: an even number

    page = client.get("/nodes")
    assert "even number" in page.text
    assert "cannot see etcd members" in page.text, "the banner must not overclaim"


def test_a_lost_quorum_is_described(client, manager, conn_factory, observer):
    for _ in range(3):
        client.post("/deploy/server")
        observe(observer)
    conn = conn_factory()
    for node in manager.list_nodes()[:2]:
        conn.lookupByUUIDString(node.uuid).destroy()

    page = client.get("/nodes")
    assert "needs 2 for quorum" in page.text


def test_a_failed_node_shows_the_bounded_guest_error(client, manager, agent, observer):
    agent.responses = [
        (
            is_status,
            lambda: report(
                agent.uuid,
                prepared="0",
                started="0",
                prepare_state="failed",
                k3s_state="inactive",
                error_code="prepare-failed",
            ),
        )
    ]
    client.post("/deploy/server")
    observe(observer)

    page = client.get("/nodes")
    assert "failed" in page.text
    assert "prepare-failed" in page.text
    assert "firstboot output" not in page.text
    assert manager.list_nodes()[0].state == meta.FAILED


def test_a_configured_node_that_is_shut_off_reads_as_such(client, manager, conn_factory, observer):
    client.post("/deploy/server")
    observe(observer)
    node = manager.list_nodes()[0]
    conn_factory().lookupByUUIDString(node.uuid).destroy()

    page = client.get("/nodes")
    assert "configured" in page.text
    assert "shut off" in page.text
    assert node.short_name in page.text, "it is still listed, not deleted"


def test_a_node_waiting_for_guest_preparation_shows_progress(client, manager, observer, agent):
    agent.responses = [
        (
            is_status,
            lambda: report(
                agent.uuid,
                prepared="0",
                started="0",
                prepare_state="activating",
                k3s_state="inactive",
            ),
        )
    ]
    manager.create(meta.ROLE_SERVER)
    observe(observer)

    page = client.get("/nodes").text
    assert "waiting for guest preparation" in page
    assert "card-busy" in page, "an in-progress node is styled distinctly"
    assert "booting" in page


def test_prune_without_a_server_explains_itself(client):
    response = client.post("/prune-nodes")
    assert response.status_code == 200
    assert "no running, configured control plane node" in response.text


def test_observation_and_reset_never_reap_unclaimed_volumes(client, observer, manager, conn):
    add_volume(conn, ORPHAN)
    observe(observer)
    client.post("/reset")
    assert manager.unclaimed_volumes() == {ORPHAN}
    assert ORPHAN in volumes(conn)


def test_shutdown_with_work_in_flight_leaves_the_connection_open(app_state, manager, monkeypatch):
    """A blocked observer must retain its connection until the process exits."""
    monkeypatch.setattr(app_state.observer, "shutdown", lambda: False)
    closed: list[bool] = []
    monkeypatch.setattr(app_state.cm, "close", lambda: closed.append(True))

    app_state.shutdown()
    assert closed == []


def test_shutdown_closes_the_connection_when_drained(app_state, monkeypatch):
    monkeypatch.setattr(app_state.observer, "shutdown", lambda: True)
    closed: list[bool] = []
    monkeypatch.setattr(app_state.cm, "close", lambda: closed.append(True))

    app_state.shutdown()
    assert closed == [True]


def test_startup_starts_observer_without_synchronous_guest_calls(app_state, monkeypatch, agent):
    started = []
    monkeypatch.setattr(app_state.observer, "start", lambda: started.append(True))
    app_state.startup()
    assert started == [True]
    assert agent.calls == []


def test_joins_wait_for_an_observed_ready_server(client, manager, observer):
    client.post("/deploy/server")
    client.post("/deploy/agent")
    assert len(manager.list_nodes()) == 1
    observe(observer)
    client.post("/deploy/agent")
    observe(observer)
    nodes = manager.list_nodes()
    assert len(nodes) == 2
    assert all(node.state == meta.CONFIGURED for node in nodes)
    assert all(observer.join_ready(node) for node in nodes)


@pytest.mark.parametrize("state", [meta.CREATING, meta.DELETING])
@pytest.mark.parametrize("running", [True, False])
def test_interrupted_operations_remove_both_volumes(
    manager, observer, conn, deployed_node, state, running
):
    dom = conn.lookupByUUIDString(deployed_node.uuid)
    if not running:
        dom.destroy()
    meta.write(dom, dataclasses.replace(meta.read(dom), state=state))
    observer.reconcile()
    assert manager.list_nodes() == []
    assert volumes(conn, "k3s-node-") == set()


@pytest.mark.parametrize("state", [meta.BOOTING, meta.CONFIGURING, meta.CONFIGURED, meta.FAILED])
def test_stopped_nodes_survive_observer_restart(manager, cfg, conn, deployed_node, agent, state):
    dom = conn.lookupByUUIDString(deployed_node.uuid)
    dom.destroy()
    error = "prepare-failed" if state == meta.FAILED else None
    meta.write(dom, dataclasses.replace(meta.read(dom), state=state, error=error))
    agent.calls.clear()
    restarted = Observer(manager, cfg, agent_factory=agent.for_node)
    observe(restarted)
    survivor = manager.get(deployed_node.name)
    assert survivor.state == state
    assert survivor.error == error
    assert survivor.power == libvirtctl.POWER_SHUT_OFF
    assert {survivor.volume, survivor.seed_volume} <= volumes(conn)
    assert agent.calls == []
    assert restarted.shutdown()


def test_restart_observes_running_nodes_without_provisioning(manager, cfg, deployed_node, agent):
    agent.calls.clear()
    restarted = Observer(manager, cfg, agent_factory=agent.for_node)
    for _ in range(3):
        observe(restarted)
    assert manager.get(deployed_node.name).state == meta.CONFIGURED
    assert len(agent.calls) == 3
    assert all(is_status(call.path, call.args) for call in agent.calls)
    assert all(call.input_data is None for call in agent.calls)
    assert all(GUEST_PATH in call.args for call in agent.calls)
    assert restarted.shutdown()


def test_kill_during_observation_does_not_resurrect_a_node(client, manager, observer, conn):
    client.post("/deploy/server")
    node = manager.list_nodes()[0]
    observer.reconcile()
    assert client.post(f"/nodes/{node.name}/kill").status_code == 200
    observe(observer)
    assert manager.list_nodes() == []
    assert volumes(conn, "k3s-node-") == set()


def test_truncated_guest_output_is_marked_as_such():
    decoded = guestexec._decode(base64.b64encode(b"partial").decode(), True)
    assert decoded == "partial" + guestexec.TRUNCATION_NOTE
    assert guestexec._decode(None, True) == guestexec.TRUNCATION_NOTE
    assert guestexec._decode(None, False) == ""


def test_process_exits_with_a_blocked_observer():
    probe = textwrap.dedent("""
        import sys
        import threading
        from types import SimpleNamespace

        sys.path.insert(0, sys.argv[1])
        from k3s_kvm_demo.observer import Observer

        entered = threading.Event()
        blocked = threading.Event()

        def list_nodes():
            entered.set()
            blocked.wait(30)
            return []

        cfg = SimpleNamespace(
            observation=SimpleNamespace(interval_s=2),
            maintenance=SimpleNamespace(shutdown_grace_s=0.01),
        )
        observer = Observer(SimpleNamespace(list_nodes=list_nodes), cfg)
        observer.start()
        assert entered.wait(2)
        assert not observer.shutdown()
    """)
    completed = subprocess.run(
        [sys.executable, "-c", probe, str(REPO_ROOT / "src")],
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert completed.returncode == 0, completed.stderr
