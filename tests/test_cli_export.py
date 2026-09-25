"""Export through the CLI with real config loading, discovery and file IO."""

import stat

import libvirt
import pytest
import yaml

from k3s_kvm_demo import cli, meta, pool as poolmod
from k3s_kvm_demo.observer import Observer

from .conftest import unmanaged_domain_xml
from .export_support import (
    FAILURES,
    SECRET,
    assert_guest_read,
    candidate_cluster as candidate_cluster,
    config_file as config_file,
    config_values as config_values,
    expected_document,
    export_agent as export_agent,
    inventory,
    script_failure,
)


@pytest.mark.parametrize("candidate_cluster", ["age", "name"], indirect=True)
def test_stdout_export_coexists_with_dashboard_without_mutations(
    client, cfg, config_file, candidate_cluster, export_agent, conn, monkeypatch, capsys, caplog
):
    def forbidden_start(_self):
        pytest.fail("CLI export must not start an Observer")

    monkeypatch.setattr(Observer, "start", forbidden_start)
    before = inventory(conn)
    assert cfg.source is None
    with caplog.at_level("DEBUG"):
        assert cli.main(["-c", str(config_file), "export"]) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    assert yaml.safe_load(captured.out) == expected_document(candidate_cluster)
    assert client.get("/healthz").status_code == 200
    assert_guest_read(export_agent, candidate_cluster)
    assert inventory(conn) == before
    assert SECRET not in caplog.text


def test_file_export_is_private_and_refuses_to_overwrite(
    config_file, deployed_node, export_agent, tmp_path, capsys, caplog
):
    output = tmp_path / "admin kubeconfig.yaml"
    args = ["-c", str(config_file), "export", "-o", str(output)]
    with caplog.at_level("DEBUG"):
        assert cli.main(args) == 0
        captured = capsys.readouterr()
        assert captured.out == captured.err == ""
        assert yaml.safe_load(output.read_text()) == expected_document(deployed_node)
        assert stat.S_IMODE(output.stat().st_mode) == 0o600
        assert_guest_read(export_agent, deployed_node)

        original = b"existing credentials\x00must remain byte-for-byte\xff\n"
        output.write_bytes(original)
        assert cli.main(args) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "already exists" in captured.err
    assert output.read_bytes() == original
    assert SECRET not in captured.err + caplog.text


@pytest.mark.parametrize("case", FAILURES)
@pytest.mark.parametrize("to_file", [False, True], ids=["stdout", "file"])
def test_failed_export_is_sanitized_and_creates_no_output(
    config_file, deployed_node, export_agent, conn, tmp_path, capsys, caplog, case, to_file
):
    message = script_failure(case, export_agent, deployed_node, conn)
    before = inventory(conn)
    output = tmp_path / "failed.yaml"
    args = ["-c", str(config_file), "export"]
    if to_file:
        args += ["-o", str(output)]
    with caplog.at_level("DEBUG"):
        assert cli.main(args) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert message in captured.err
    assert SECRET not in captured.err + caplog.text
    assert not output.exists()
    assert inventory(conn) == before
    if case == "missing-server":
        assert not export_agent.calls
    else:
        assert_guest_read(export_agent, deployed_node)


@pytest.mark.parametrize("guard", ["unowned-pool", "incompatible-metadata"])
def test_export_checks_real_pool_ownership_and_metadata_before_guest_access(
    config_file, deployed_node, export_agent, conn, capsys, guard
):
    if guard == "unowned-pool":
        pool = conn.storagePoolLookupByName("default-pool")
        pool.storageVolLookupByName(poolmod.MARKER_VOLUME).delete(0)
        message = "marker"
    else:
        foreign = conn.defineXML(unmanaged_domain_xml("foreign-incompatible"))
        foreign.setMetadata(
            libvirt.VIR_DOMAIN_METADATA_ELEMENT,
            "<node/>",
            meta.KEY,
            meta.LEGACY_NS,
            libvirt.VIR_DOMAIN_AFFECT_CONFIG,
        )
        message = "incompatible"
    before = inventory(conn)
    assert cli.main(["-c", str(config_file), "export"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert message in captured.err
    assert not export_agent.calls
    assert inventory(conn) == before
