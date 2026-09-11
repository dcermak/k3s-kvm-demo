"""Seed format, ISO construction, and the real stream-upload call sequence."""

from __future__ import annotations

import shutil
import subprocess
from unittest.mock import Mock

import pytest

from k3s_kvm_demo import seed

UUID = "12345678-1234-1234-1234-123456789abc"


def payload(**overrides):
    values = dict(node_uuid=UUID, hostname="node-a", role="server", config_bytes=b'token: "t"\n')
    values.update(overrides)
    return seed.build_seed_payload(**values)


def test_payload_contract_and_secret_bytes():
    config = b'token: "$(id) # \\""\n'
    assert payload(config_bytes=config) == {
        "schema": b"1\n",
        "node_uuid": (UUID + "\n").encode(),
        "hostname": b"node-a\n",
        "role": b"server\n",
        "config.yaml": config,
    }
    assert issubclass(seed.SeedError, ValueError)


@pytest.mark.parametrize(
    "values",
    [
        {"node_uuid": "not-a-uuid"},
        {"node_uuid": UUID.upper()},
        {"hostname": "-bad"},
        {"hostname": "a" * 64},
        {"hostname": "$(id)"},
        {"hostname": "node\nother"},
        {"role": "server;id"},
        {"config_bytes": "not bytes"},
        {"config_bytes": b""},
        {"config_bytes": b"x" * (seed.CONFIG_MAX_BYTES + 1)},
        {"config_bytes": b"a\0b"},
    ],
)
def test_invalid_payload(values):
    with pytest.raises(seed.SeedError):
        payload(**values)


def test_payload_at_size_limit():
    assert len(payload(config_bytes=b"x" * seed.CONFIG_MAX_BYTES)["config.yaml"]) == 131072


def test_missing_xorriso_is_seed_diagnostic(monkeypatch, tmp_path):
    monkeypatch.setattr(seed.shutil, "which", lambda _name: None)
    with pytest.raises(seed.SeedError, match="xorriso is required"):
        seed.build_seed_iso(payload(), tmp_path / "seed.iso")


@pytest.mark.parametrize(
    "mutation",
    [
        {"../escape": b"bad"},
        {"schema": b"2\n"},
        {"hostname": b"node-a"},
        {"node_uuid": b"\xff\n"},
        {"config.yaml": b"x" * (seed.CONFIG_MAX_BYTES + 1)},
    ],
)
def test_iso_revalidates_mapping_before_running_tools(monkeypatch, tmp_path, mutation):
    run = Mock()
    monkeypatch.setattr(seed.subprocess, "run", run)
    with pytest.raises(seed.SeedError):
        seed.build_seed_iso(payload() | mutation, tmp_path / "seed.iso")
    run.assert_not_called()


def test_real_iso_label_contents_and_permissions(tmp_path):
    if shutil.which("xorriso") is None:
        pytest.skip("xorriso is not installed")
    artifact = tmp_path / "seed.iso"
    contents = payload(config_bytes=b"x" * seed.CONFIG_MAX_BYTES)
    seed.build_seed_iso(contents, artifact)
    assert 0 < artifact.stat().st_size <= seed.ISO_MAX_BYTES
    assert artifact.stat().st_mode & 0o777 == 0o600
    image = artifact.read_bytes()
    assert image[16 * 2048 + 40 : 16 * 2048 + 72].rstrip(b" ") == b"K3SDEMO"
    extracted = tmp_path / "extracted"
    subprocess.run(
        ["xorriso", "-osirrox", "on", "-indev", str(artifact), "-extract", "/", str(extracted)],
        check=True,
        capture_output=True,
    )
    assert {path.name: path.read_bytes() for path in extracted.iterdir()} == contents
    assert not list(tmp_path.glob("k3s-demo-seed-*"))


@pytest.mark.parametrize("failure", ["tool", "oversize"])
def test_failed_build_keeps_destination_and_hides_tool_output(monkeypatch, tmp_path, failure):
    artifact = tmp_path / "seed.iso"
    artifact.write_bytes(b"previous")
    monkeypatch.setattr(seed.shutil, "which", lambda _name: "/usr/bin/xorriso")

    def run(argv, **kwargs):
        if failure == "tool":
            raise subprocess.CalledProcessError(1, argv, stderr=b"secret-token")
        from pathlib import Path

        Path(argv[argv.index("-o") + 1]).write_bytes(b"x" * (seed.ISO_MAX_BYTES + 1))

    monkeypatch.setattr(seed.subprocess, "run", run)
    with pytest.raises(seed.SeedError) as caught:
        seed.build_seed_iso(payload(), artifact)
    assert "secret-token" not in str(caught.value)
    assert artifact.read_bytes() == b"previous"
    assert not list(tmp_path.glob("k3s-demo-seed-*"))


def test_stream_upload_sends_artifact_and_finishes(tmp_path):
    artifact = tmp_path / "seed.iso"
    artifact.write_bytes(b"the image")
    connection, volume, stream = Mock(), Mock(), Mock()
    connection.newStream.return_value = stream
    received = bytearray()

    def send_all(handler, opaque):
        while chunk := handler(stream, 3, opaque):
            received.extend(chunk)

    stream.sendAll.side_effect = send_all
    seed.upload_seed(connection, volume, artifact)
    connection.newStream.assert_called_once_with(0)
    volume.upload.assert_called_once_with(stream, 0, 9, 0)
    assert received == b"the image"
    stream.finish.assert_called_once_with()
    stream.abort.assert_not_called()


@pytest.mark.parametrize("stage", ["newStream", "upload", "sendAll", "finish"])
def test_stream_errors_abort_and_do_not_expose_details(tmp_path, stage):
    artifact = tmp_path / "seed.iso"
    artifact.write_bytes(b"image")
    connection, volume, stream = Mock(), Mock(), Mock()
    connection.newStream.return_value = stream
    target = {"newStream": connection, "upload": volume}.get(stage, stream)
    getattr(target, stage).side_effect = RuntimeError("secret-token")
    stream.abort.side_effect = RuntimeError("abort also failed")
    with pytest.raises(seed.SeedError, match="could not upload") as caught:
        seed.upload_seed(connection, volume, artifact)
    assert "secret-token" not in str(caught.value)
    if stage == "newStream":
        stream.abort.assert_not_called()
    else:
        stream.abort.assert_called_once_with()
    if stage != "finish":
        stream.finish.assert_not_called()


@pytest.mark.parametrize("size", [0, seed.ISO_MAX_BYTES + 1])
def test_upload_rejects_unbounded_artifact(tmp_path, size):
    artifact = tmp_path / "seed.iso"
    artifact.write_bytes(b"x" * size)
    connection = Mock()
    with pytest.raises(seed.SeedError):
        seed.upload_seed(connection, Mock(), artifact)
    connection.newStream.assert_not_called()
