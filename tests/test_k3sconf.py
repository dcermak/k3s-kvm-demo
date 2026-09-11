"""The guest-side files.

``config.yaml`` is emitted as JSON precisely so that hostile values cannot
break the document; these tests hold that property down by feeding it the
things that would break hand-written YAML.
"""

from __future__ import annotations

import pytest
import yaml

from k3s_kvm_demo import k3sconf, meta

HOSTILE = [
    'quote"inside',
    "single'inside",
    "line\nbreak",
    "EOF",
    "$(id)",
    "`id`",
    "#comment",
    "- dash",
    "{brace}",
    "\\backslash",
    "tab\there",
    "colon: value",
]


def parse(document: str) -> dict:
    return yaml.safe_load(document)


def test_bootstrap_server_initialises_the_cluster():
    document = parse(
        k3sconf.config_yaml(
            node_name="k3s-node-a",
            role=meta.ROLE_SERVER,
            token="t",
            is_bootstrap=True,
            server_url=None,
        )
    )
    assert document["cluster-init"] is True
    assert "server" not in document
    assert document["write-kubeconfig-mode"] == "0644"
    assert document["node-name"] == "k3s-node-a"


def test_joining_server_points_at_the_bootstrap():
    document = parse(
        k3sconf.config_yaml(
            node_name="k3s-node-b",
            role=meta.ROLE_SERVER,
            token="t",
            is_bootstrap=False,
            server_url="https://192.168.122.10:6443",
        )
    )
    assert document["server"] == "https://192.168.122.10:6443"
    assert "cluster-init" not in document


def test_agent_is_a_worker_with_no_kubeconfig():
    document = parse(
        k3sconf.config_yaml(
            node_name="k3s-node-c",
            role=meta.ROLE_AGENT,
            token="t",
            is_bootstrap=False,
            server_url="https://192.168.122.10:6443",
        )
    )
    assert document["server"] == "https://192.168.122.10:6443"
    assert "write-kubeconfig-mode" not in document
    assert "cluster-init" not in document


def test_tls_san_is_actually_written():
    """It was declared as an input and then silently dropped in an earlier
    draft of this design."""
    document = parse(
        k3sconf.config_yaml(
            node_name="k3s-node-d",
            role=meta.ROLE_SERVER,
            token="t",
            is_bootstrap=True,
            server_url=None,
            tls_san=["vip.example", "10.0.0.9"],
        )
    )
    assert document["tls-san"] == ["vip.example", "10.0.0.9"]


def test_tls_san_is_not_emitted_for_agents():
    document = parse(
        k3sconf.config_yaml(
            node_name="k3s-node-e",
            role=meta.ROLE_AGENT,
            token="t",
            is_bootstrap=False,
            server_url="https://s:6443",
            tls_san=["vip.example"],
        )
    )
    assert "tls-san" not in document


@pytest.mark.parametrize("value", HOSTILE)
def test_hostile_tokens_survive_intact(value):
    document = parse(
        k3sconf.config_yaml(
            node_name="k3s-node-f",
            role=meta.ROLE_AGENT,
            token=value,
            is_bootstrap=False,
            server_url="https://s:6443",
        )
    )
    assert document["token"] == value
    assert document["server"] == "https://s:6443"


@pytest.mark.parametrize("value", HOSTILE)
def test_hostile_sans_survive_intact(value):
    document = parse(
        k3sconf.config_yaml(
            node_name="k3s-node-g",
            role=meta.ROLE_SERVER,
            token="t",
            is_bootstrap=True,
            server_url=None,
            tls_san=[value],
        )
    )
    assert document["tls-san"] == [value]


def test_impossible_role_and_url_combinations_are_rejected():
    with pytest.raises(ValueError):
        k3sconf.config_yaml(
            node_name="n", role="bogus", token="t", is_bootstrap=False, server_url="u"
        )
    with pytest.raises(ValueError):
        # an agent cannot bootstrap
        k3sconf.config_yaml(
            node_name="n", role=meta.ROLE_AGENT, token="t", is_bootstrap=True, server_url=None
        )
    with pytest.raises(ValueError):
        # a joining server needs somewhere to join
        k3sconf.config_yaml(
            node_name="n", role=meta.ROLE_SERVER, token="t", is_bootstrap=False, server_url=None
        )
    with pytest.raises(ValueError):
        # the bootstrap has nowhere to join
        k3sconf.config_yaml(
            node_name="n", role=meta.ROLE_SERVER, token="t", is_bootstrap=True, server_url="u"
        )
    with pytest.raises(ValueError):
        k3sconf.config_yaml(
            node_name="n", role=meta.ROLE_AGENT, token="t", is_bootstrap=False, server_url=None
        )


def test_only_configuration_is_rendered_on_the_host():
    assert k3sconf.UNIT_NAME == "k3s-node.service"
    assert k3sconf.CONFIG_PATH == "/etc/rancher/k3s/config.yaml"
    assert not hasattr(k3sconf, "render_firstboot")
    assert not hasattr(k3sconf, "systemd_unit")
