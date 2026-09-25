"""Access control and output escaping.

The API destroys VMs and runs root commands in guests with no authentication.
Loopback binding is enforced in config; it does not, on its own, stop a page on
another origin from POSTing here, which is what the middleware is for.
"""

from __future__ import annotations

import copy
import dataclasses

import pytest

from k3s_kvm_demo import config as configmod, meta

from .conftest import is_status, observe, result

UNSAFE = [
    ("POST", "/deploy/agent"),
    ("POST", "/deploy/server"),
    ("POST", "/reset"),
    ("POST", "/prune-nodes"),
    ("POST", "/kubeconfig/download"),
    ("POST", "/kubeconfig/copy"),
    ("POST", "/nodes/k3s-node-" + "0" * 32 + "/kill"),
]


@pytest.mark.parametrize(("method", "path"), UNSAFE)
def test_a_cross_origin_post_is_rejected(client, method, path):
    response = client.request(method, path, headers={"Origin": "https://evil.example"})
    assert response.status_code == 403
    assert "cross-origin" in response.text


@pytest.mark.parametrize(("method", "path"), UNSAFE)
def test_a_cross_site_fetch_is_rejected(client, method, path):
    response = client.request(method, path, headers={"Sec-Fetch-Site": "cross-site"})
    assert response.status_code == 403


@pytest.mark.parametrize(("method", "path"), UNSAFE)
def test_an_unexpected_host_header_is_rejected(client, method, path):
    response = client.request(method, path, headers={"Host": "kiosk.example.com"})
    assert response.status_code == 403


def test_a_same_origin_post_is_allowed(client):
    response = client.post(
        "/deploy/server",
        headers={"Origin": "http://testserver", "Sec-Fetch-Site": "same-origin"},
    )
    assert response.status_code == 200


def test_reads_are_not_gated(client):
    for path in ("/", "/nodes", "/static/app.css"):
        assert client.get(path, headers={"Origin": "https://evil.example"}).status_code == 200


def test_a_cross_origin_post_changes_nothing(client, manager):
    client.request("POST", "/deploy/server", headers={"Origin": "https://evil.example"})
    assert manager.list_nodes() == []


# -- output escaping -------------------------------------------------------


def test_arbitrary_guest_output_is_not_displayed(client, manager, agent, observer):
    payload = "<script>alert('xss')</script> & \"quotes\""
    agent.responses = [(is_status, result(1, stdout=payload, stderr=payload))]
    client.post("/deploy/server")
    observe(observer)

    page = client.get("/nodes").text
    assert "<script>alert" not in page
    assert "&lt;script&gt;" not in page
    assert "guest status unknown" in page
    assert manager.list_nodes()[0].state == meta.BOOTING


def test_a_persisted_failure_reason_is_escaped(client, manager):
    client.post("/deploy/server")
    node = manager.list_nodes()[0]
    manager.update_state(node.uuid, node.generation, meta.FAILED, error="<b>bold</b> & failure")

    page = client.get("/nodes").text
    assert "<b>bold</b>" not in page
    assert "&lt;b&gt;bold&lt;/b&gt;" in page
    assert "&amp; failure" in page


@pytest.mark.parametrize("exitcode", [0, 1])
def test_the_token_never_appears_in_any_response(client, cfg, agent, observer, exitcode):
    agent.responses = [
        (is_status, result(exitcode, stdout=cfg.cluster.token, stderr=cfg.cluster.token)),
    ]
    client.post("/deploy/server")
    observe(observer)

    for path in ("/", "/nodes"):
        assert cfg.cluster.token not in client.get(path).text, path


def test_guest_output_is_not_retained_in_metadata_or_observations(
    observer, manager, cfg, agent, conn
):
    agent.responses = [(is_status, result(1, stderr=f"token={cfg.cluster.token}"))]
    node = manager.create(meta.ROLE_SERVER)
    observe(observer)

    decorated = observer.decorate(manager.list_nodes())[0]
    assert not decorated.log
    assert decorated.error is None
    assert cfg.cluster.token not in repr(observer._observations)
    assert cfg.cluster.token not in conn.lookupByUUIDString(node.uuid).XMLDesc(0)


@pytest.mark.parametrize("exitcode", [0, 1])
def test_the_token_stays_out_of_log_records(observer, manager, cfg, agent, caplog, exitcode):
    agent.responses = [
        (
            is_status,
            result(
                exitcode, stdout=f"token={cfg.cluster.token}", stderr=f"token={cfg.cluster.token}"
            ),
        )
    ]
    manager.create(meta.ROLE_SERVER)
    with caplog.at_level("DEBUG"):
        observe(observer)
    assert cfg.cluster.token not in caplog.text


# -- the configuration guard ----------------------------------------------


def test_a_non_loopback_bind_refuses_to_start(config_values):
    for address in ("0.0.0.0", "192.168.1.10", "::"):
        values = copy.deepcopy(config_values)
        values["server"]["bind"] = address
        with pytest.raises(configmod.ConfigError) as excinfo:
            configmod.from_mapping(values)
        assert excinfo.value.key == "server.bind"
        assert "unauthenticated" in str(excinfo.value)


def test_the_allowlist_is_what_the_middleware_consults(cfg, app_state):
    from fastapi.testclient import TestClient

    from k3s_kvm_demo.app import create_app

    widened = dataclasses.replace(
        cfg, server=dataclasses.replace(cfg.server, allowed_hosts=("kiosk.local",))
    )
    with TestClient(create_app(widened, lambda: app_state)) as client:
        assert client.post("/deploy/server").status_code == 403
        assert client.post("/deploy/server", headers={"Host": "kiosk.local"}).status_code == 200
