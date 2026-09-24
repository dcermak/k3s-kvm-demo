"""Runtime-directory readiness checks performed by the check command."""

from __future__ import annotations

import contextlib
from dataclasses import replace

import pytest

from k3s_kvm_demo import cli, config as configmod


def test_check_rejects_missing_lock_parent_without_creating_it(cfg, tmp_path, capsys):
    lock_path = tmp_path / "missing" / "dashboard.lock"
    cfg = replace(cfg, server=replace(cfg.server, lock_path=lock_path))

    assert cli.cmd_check(cfg) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "server.lock_path" in captured.err
    assert str(lock_path.parent) in captured.err
    assert not lock_path.parent.exists()


def test_check_accepts_usable_lock_parent(cfg, tmp_path, monkeypatch, capsys):
    cfg = replace(cfg, server=replace(cfg.server, lock_path=tmp_path / "dashboard.lock"))
    monkeypatch.setattr(cli, "_connect", lambda _cfg: contextlib.nullcontext(object()))
    monkeypatch.setattr(configmod, "validate_hypervisor", lambda _conn, _cfg: [])

    assert cli.cmd_check(cfg) == 0

    captured = capsys.readouterr()
    assert "configuration at None parses cleanly" in captured.out
    assert "hypervisor prerequisites are satisfied" in captured.out
    assert captured.err == ""


def test_serve_rejects_missing_lock_parent_before_startup(cfg, tmp_path):
    lock_path = tmp_path / "missing" / "dashboard.lock"
    cfg = replace(cfg, server=replace(cfg.server, lock_path=lock_path))

    with pytest.raises(configmod.ConfigError, match=r"server\.lock_path"):
        cli.cmd_serve(cfg)

    assert not lock_path.parent.exists()
