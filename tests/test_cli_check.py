"""Configuration checks performed by the check command."""

from __future__ import annotations

import contextlib

from k3s_kvm_demo import cli, config as configmod


def test_check_accepts_valid_configuration(cfg, monkeypatch, capsys):
    monkeypatch.setattr(cli, "_connect", lambda _cfg: contextlib.nullcontext(object()))
    monkeypatch.setattr(configmod, "validate_hypervisor", lambda _conn, _cfg: [])

    assert cli.cmd_check(cfg) == 0

    captured = capsys.readouterr()
    assert "configuration at None parses cleanly" in captured.out
    assert "hypervisor prerequisites are satisfied" in captured.out
    assert captured.err == ""
