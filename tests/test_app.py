"""The HTTP surface.

Every mutating route re-renders the same grid, so most of these assertions are
about what the operator sees: a refusal has to arrive as a readable message,
not a 500.
"""

from __future__ import annotations

import pytest

from k3s_kvm_demo import meta

from .conftest import is_firstboot, is_probe, result


@pytest.fixture(autouse=True)
def firstboot_succeeds(agent):
    agent.responses = [(is_firstboot, result(0)), (is_probe, result(0))]


def test_the_page_renders_with_an_empty_grid(client):
    page = client.get("/")
    assert page.status_code == 200
    assert 'id="grid"' in page.text
    assert 'hx-trigger="every 2s"' in page.text
    assert "Deploy Control Plane node" in page.text
    assert "No nodes yet" in page.text


def test_static_assets_are_served_locally(client):
    script = client.get("/static/htmx.min.js")
    assert script.status_code == 200
    assert b"htmx" in script.content
    assert client.get("/static/app.css").status_code == 200


def test_deploying_a_control_plane_node_shows_it_in_the_grid(client, manager):
    response = client.post("/deploy/server")
    assert response.status_code == 200

    node = manager.list_nodes()[0]
    assert node.role == meta.ROLE_SERVER
    assert node.bootstrap is True
    assert node.short_name in response.text
    assert node.name in response.text  # the full name is kept in a title


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


def test_the_node_limit_is_reported_rather_than_raised(client, manager, cfg):
    client.post("/deploy/server")
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


def test_reset_clears_everything_and_says_how_much(client, manager):
    client.post("/deploy/server")
    client.post("/deploy/agent")
    assert len(manager.list_nodes()) == 2

    response = client.post("/reset")
    assert "removed 2 node(s)" in response.text
    assert manager.list_nodes() == []


def test_the_quorum_banner_is_marked_as_advisory(client, manager):
    client.post("/deploy/server")
    client.post("/deploy/server")  # two servers: an even number

    page = client.get("/nodes")
    assert "even number" in page.text
    assert "cannot see etcd members" in page.text, "the banner must not overclaim"


def test_a_lost_quorum_is_described(client, manager, conn_factory):
    for _ in range(3):
        client.post("/deploy/server")
    conn = conn_factory()
    for node in manager.list_nodes()[:2]:
        conn.lookupByUUIDString(node.uuid).destroy()

    page = client.get("/nodes")
    assert "needs 2 for quorum" in page.text


def test_a_failed_node_shows_its_reason_and_log(client, manager, agent):
    agent.responses = [(is_firstboot, result(1, stderr="k3s is not on PATH"))]
    client.post("/deploy/server")

    page = client.get("/nodes")
    assert "failed" in page.text
    assert "k3s is not on PATH" in page.text
    assert "firstboot output" in page.text


def test_a_configured_node_that_is_shut_off_reads_as_such(client, manager, conn_factory):
    client.post("/deploy/server")
    node = manager.list_nodes()[0]
    conn_factory().lookupByUUIDString(node.uuid).destroy()

    page = client.get("/nodes")
    assert "configured" in page.text
    assert "shut off" in page.text
    assert node.short_name in page.text, "it is still listed, not deleted"


def test_a_node_being_worked_on_shows_its_progress(client, manager, provisioner, agent):
    agent.responses = [(is_firstboot, result(0))]
    node, _ = manager.create(meta.ROLE_SERVER)
    provisioner._set_progress(node.uuid, "running firstboot")

    page = client.get("/nodes").text
    assert "running firstboot" in page
    assert "card-busy" in page, "an in-progress node is styled distinctly"
    assert "booting" in page


def test_prune_without_a_server_explains_itself(client):
    response = client.post("/prune-nodes")
    assert response.status_code == 200
    assert "no running, configured control plane node" in response.text


def test_healthz_reports_what_an_operator_needs(client, manager):
    client.post("/deploy/server")
    payload = client.get("/healthz").json()

    assert payload["nodes"] == 1
    assert payload["states"] == [meta.CONFIGURED]
    assert payload["pool_marker"] is True
    assert payload["unclassified_volumes"] == []
    assert payload["orphan_candidates"] == []
    assert payload["in_flight"] == 0


def test_healthz_surfaces_orphan_candidates(client, manager, conn_factory):
    from .conftest import TEST_POOL

    storage = conn_factory().storagePoolLookupByName(TEST_POOL)
    orphan = "k3s-node-" + "7" * 32 + ".qcow2"
    storage.createXML(
        f"<volume type='file'><name>{orphan}</name>"
        "<capacity unit='bytes'>1048576</capacity>"
        "<target><format type='qcow2'/></target></volume>",
        0,
    )
    assert client.get("/healthz").json()["orphan_candidates"] == [orphan]


def test_orphans_are_only_reported_while_reaping_is_off(app_state, manager, conn_factory, caplog):
    from .conftest import TEST_POOL

    storage = conn_factory().storagePoolLookupByName(TEST_POOL)
    orphan = "k3s-node-" + "8" * 32 + ".qcow2"
    storage.createXML(
        f"<volume type='file'><name>{orphan}</name>"
        "<capacity unit='bytes'>1048576</capacity>"
        "<target><format type='qcow2'/></target></volume>",
        0,
    )

    app_state.reap_orphans()  # first sighting
    with caplog.at_level("WARNING"):
        assert app_state.reap_orphans() == []
    assert orphan in caplog.text
    assert "vol-delete" in caplog.text, "tell the operator how to do it by hand"
    assert manager.unclaimed_volumes() == {orphan}


def test_orphans_are_deleted_once_reaping_is_enabled(app_state, manager, conn_factory, cfg):
    import dataclasses

    from .conftest import TEST_POOL

    storage = conn_factory().storagePoolLookupByName(TEST_POOL)
    orphan = "k3s-node-" + "8" * 32 + ".qcow2"
    storage.createXML(
        f"<volume type='file'><name>{orphan}</name>"
        "<capacity unit='bytes'>1048576</capacity>"
        "<target><format type='qcow2'/></target></volume>",
        0,
    )
    app_state.cfg = dataclasses.replace(
        cfg, maintenance=dataclasses.replace(cfg.maintenance, reap_orphans=True)
    )

    assert app_state.reap_orphans() == [], "one sighting is never enough"
    assert app_state.reap_orphans() == [orphan]
    assert manager.unclaimed_volumes() == set()


def test_shutdown_with_work_in_flight_leaves_the_connection_open(app_state, manager, monkeypatch):
    """Closing the connection under a worker would be a use-after-free; the
    process exits anyway because the workers are daemon threads."""
    monkeypatch.setattr(app_state.provisioner, "shutdown", lambda: False)
    closed: list[bool] = []
    monkeypatch.setattr(app_state.cm, "close", lambda: closed.append(True))

    app_state.shutdown()
    assert closed == []


def test_shutdown_closes_the_connection_when_drained(app_state, monkeypatch):
    monkeypatch.setattr(app_state.provisioner, "shutdown", lambda: True)
    closed: list[bool] = []
    monkeypatch.setattr(app_state.cm, "close", lambda: closed.append(True))

    app_state.shutdown()
    assert closed == [True]
