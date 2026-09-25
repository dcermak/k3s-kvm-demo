"""Automatic exports use the real formatter and filesystem with a scripted guest."""

import os
import threading
from dataclasses import replace

import pytest
import yaml

from k3s_kvm_demo import config as configmod
from k3s_kvm_demo.kubeconfig_export import KubeconfigExporter

from .conftest import result
from .export_support import (
    SECRET,
    YAML,
    expected_document,
    export_agent as export_agent,
    script_failure,
)


@pytest.fixture
def exporter(manager, cfg):
    worker = KubeconfigExporter(manager, cfg)
    yield worker
    assert worker.shutdown()


def test_refresh_writes_private_config_and_replaces_only_changes(
    exporter, deployed_node, export_agent, monkeypatch,
):
    exporter.refresh()
    path = exporter.path
    assert yaml.safe_load(path.read_text()) == expected_document(deployed_node)
    assert path.stat().st_mode & 0o777 == 0o600
    original = path.stat()

    exporter.refresh()
    assert path.stat().st_ino == original.st_ino
    assert path.stat().st_mtime_ns == original.st_mtime_ns

    old_content = path.read_bytes()
    real_replace = os.replace

    def checked_replace(source, destination):
        assert path.read_bytes() == old_content, "old file stays complete until replacement"
        real_replace(source, destination)

    monkeypatch.setattr(os, "replace", checked_replace)
    export_agent.default = result(stdout=YAML.replace(SECRET, "new-key"))
    exporter.refresh()
    assert "new-key" in path.read_text()
    assert SECRET not in path.read_text()
    assert path.stat().st_mode & 0o777 == 0o600
    assert list(path.parent.glob(f".{path.name}-*")) == []

    monkeypatch.setattr(os, "replace", real_replace)
    path.unlink()
    exporter.refresh()
    assert "new-key" in path.read_text()


@pytest.mark.parametrize(
    "failure", ["failed-read", "empty", "malformed", "truncated", "agent-error"],
)
def test_failure_removes_file_and_next_success_recovers(
    exporter, deployed_node, export_agent, conn, failure, caplog,
):
    exporter.refresh()
    assert exporter.path.exists()
    script_failure(failure, export_agent, deployed_node, conn)
    exporter.refresh()
    assert not exporter.path.exists()
    assert SECRET not in caplog.text

    # Restore the scripted guest without replacing the worker.
    export_agent.responses.clear()
    export_agent.default = result(stdout=YAML)
    exporter.refresh()
    assert yaml.safe_load(exporter.path.read_text()) == expected_document(deployed_node)


def test_reset_removes_export_on_next_refresh(exporter, deployed_node, export_agent, manager):
    exporter.refresh()
    assert exporter.path.exists()
    manager.reset()
    exporter.refresh()
    assert not exporter.path.exists()


def test_failed_replace_removes_old_export_and_temporary_file(
    exporter, deployed_node, export_agent, monkeypatch, caplog,
):
    exporter.refresh()
    export_agent.default = result(stdout=YAML.replace(SECRET, "new-key"))

    def failed_replace(*args):
        raise OSError("read-only filesystem")

    monkeypatch.setattr(os, "replace", failed_replace)
    exporter.refresh()
    assert not exporter.path.exists()
    assert list(exporter.path.parent.glob(f".{exporter.path.name}-*")) == []
    assert "read-only filesystem" in caplog.text


def test_app_refreshes_without_http_requests_and_removes_on_shutdown(
    app_state, deployed_node, export_agent, monkeypatch,
):
    exporter = KubeconfigExporter(app_state.manager, app_state.cfg)
    app_state.exporter = exporter
    exporter.path.write_text("stale credentials")
    first, second = threading.Event(), threading.Event()
    real_write = exporter._write

    def published(text):
        real_write(text)
        (second if first.is_set() else first).set()

    monkeypatch.setattr(exporter, "_write", published)
    try:
        app_state.startup()
        assert first.wait(3)
        assert yaml.safe_load(exporter.path.read_text()) == expected_document(deployed_node)
        export_agent.default = result(stdout=YAML.replace(SECRET, "renewed-key"))
        assert second.wait(3), "worker must keep refreshing without browser activity"
        assert "renewed-key" in exporter.path.read_text()
    finally:
        app_state.shutdown()
    assert not exporter.path.exists()
    assert not exporter._thread.is_alive()


def test_shutdown_during_guest_read_prevents_late_publication(
    app_state, deployed_node, export_agent, monkeypatch,
):
    cfg = replace(app_state.cfg, maintenance=replace(app_state.cfg.maintenance, shutdown_grace_s=0))
    exporter = KubeconfigExporter(app_state.manager, cfg)
    app_state.exporter = exporter
    entered, release = threading.Event(), threading.Event()
    real_poll = export_agent.poll

    def blocked_poll(pid):
        entered.set()
        assert release.wait(5)
        return real_poll(pid)

    monkeypatch.setattr(export_agent, "poll", blocked_poll)
    closed = []
    real_close = app_state.cm.close
    monkeypatch.setattr(app_state.cm, "close", lambda: closed.append(True))
    try:
        app_state.startup()
        assert entered.wait(3)
        app_state.shutdown()
        assert closed == [], "libvirt must remain open while the exporter is using it"
        assert not exporter.path.exists()
    finally:
        release.set()
        if exporter._thread is not None:
            exporter._thread.join(3)
        monkeypatch.setattr(app_state.cm, "close", real_close)
    assert exporter.shutdown()
    assert not exporter.path.exists()


@pytest.mark.parametrize("value", [0, -1, True, 1.5, "1"])
def test_interval_must_be_positive_integer(config_values, value):
    config_values["kubeconfig_export"]["interval_s"] = value
    with pytest.raises(configmod.ConfigError, match=r"kubeconfig_export\.interval_s"):
        configmod.from_mapping(config_values)


@pytest.mark.parametrize("value", ["", "relative.yaml", True])
def test_export_path_must_be_absolute(config_values, value):
    config_values["kubeconfig_export"]["path"] = value
    with pytest.raises(configmod.ConfigError, match=r"kubeconfig_export\.path"):
        configmod.from_mapping(config_values)


def test_defaults(config_values):
    del config_values["kubeconfig_export"]
    cfg = configmod.from_mapping(config_values)
    assert str(cfg.kubeconfig_export.path) == "/run/k3s-kvm-demo/kubeconfig.yaml"
    assert cfg.kubeconfig_export.interval_s == 1


def test_missing_parent_is_rejected_without_creation(cfg, tmp_path):
    path = tmp_path / "missing" / "kubeconfig.yaml"
    cfg = replace(cfg, kubeconfig_export=replace(cfg.kubeconfig_export, path=path))
    with pytest.raises(configmod.ConfigError, match="parent directory"):
        configmod.validate_export_path(cfg)
    assert not path.parent.exists()
