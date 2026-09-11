"""Create the guest's bounded, read-only seed and upload it through libvirt."""

from __future__ import annotations

import contextlib
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import UUID

if TYPE_CHECKING:
    import libvirt

CONFIG_MAX_BYTES = 128 * 1024
ISO_MAX_BYTES = 1024 * 1024
SEED_LABEL = "K3SDEMO"
_FILES = {"schema", "node_uuid", "hostname", "role", "config.yaml"}


class SeedError(ValueError):
    """The seed could not be validated, built, or uploaded."""


def build_seed_payload(
    node_uuid: str, hostname: str, role: str, config_bytes: bytes
) -> dict[str, bytes]:
    """Return the five files understood by guest protocol version 1."""
    try:
        valid_uuid = str(UUID(node_uuid)) == node_uuid
    except (ValueError, AttributeError, TypeError):
        valid_uuid = False
    if not valid_uuid:
        raise SeedError("node_uuid must be a canonical UUID")
    if not isinstance(hostname, str) or not re.fullmatch(
        r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", hostname
    ):
        raise SeedError("hostname must be a DNS label of at most 63 characters")
    if role not in ("server", "agent"):
        raise SeedError("role must be server or agent")
    if not isinstance(config_bytes, bytes) or not 0 < len(config_bytes) <= CONFIG_MAX_BYTES:
        raise SeedError("config.yaml must contain 1 to 131072 bytes")
    if b"\0" in config_bytes:
        raise SeedError("config.yaml must not contain NUL bytes")
    return {
        "schema": b"1\n",
        "node_uuid": (node_uuid + "\n").encode("ascii"),
        "hostname": (hostname + "\n").encode("ascii"),
        "role": (role + "\n").encode("ascii"),
        "config.yaml": config_bytes,
    }


def build_seed_iso(payload: dict[str, bytes], destination: Path) -> None:
    """Build an ISO with fixed filenames and label; never pass payload text as argv."""
    if set(payload) != _FILES or any(not isinstance(v, bytes) for v in payload.values()):
        raise SeedError("seed must contain exactly the five protocol files as bytes")
    try:
        expected = build_seed_payload(
            payload["node_uuid"].decode("ascii").removesuffix("\n"),
            payload["hostname"].decode("ascii").removesuffix("\n"),
            payload["role"].decode("ascii").removesuffix("\n"),
            payload["config.yaml"],
        )
    except UnicodeError:
        raise SeedError("seed metadata must be ASCII") from None
    if payload != expected:
        raise SeedError("seed metadata must use schema 1 and newline-terminated fields")
    xorriso = shutil.which("xorriso")
    if xorriso is None:
        raise SeedError("xorriso is required to build the guest seed ISO")
    try:
        destination = destination.absolute()
        with tempfile.TemporaryDirectory(prefix="k3s-demo-seed-", dir=destination.parent) as tmp:
            directory = Path(tmp)
            source = directory / "payload"
            source.mkdir(mode=0o700)
            for name, contents in payload.items():
                path = source / name
                with path.open("xb") as handle:
                    os.fchmod(handle.fileno(), 0o600)
                    handle.write(contents)
            artifact = directory / "seed.iso"
            subprocess.run(
                [
                    xorriso,
                    "-as",
                    "mkisofs",
                    "-quiet",
                    "-R",
                    "-V",
                    SEED_LABEL,
                    "-o",
                    str(artifact),
                    str(source),
                ],
                check=True,
                capture_output=True,
                timeout=60,
            )
            if not 0 < artifact.stat().st_size <= ISO_MAX_BYTES:
                raise SeedError("seed ISO must contain 1 to 1048576 bytes")
            artifact.chmod(0o600)
            artifact.replace(destination)
    except (OSError, subprocess.SubprocessError):
        # Tool output and exception details are not safe diagnostic channels for the token.
        raise SeedError("could not build guest seed ISO") from None


def upload_seed(conn: libvirt.virConnect, volume: libvirt.virStorageVol, artifact: Path) -> None:
    """Upload the artifact to an already-created raw volume using a libvirt stream."""
    stream = None
    try:
        with artifact.open("rb") as handle:
            size = os.fstat(handle.fileno()).st_size
            if not 0 < size <= ISO_MAX_BYTES:
                raise SeedError("seed ISO must contain 1 to 1048576 bytes")
            stream = conn.newStream(0)
            volume.upload(stream, 0, size, 0)
            stream.sendAll(lambda _stream, length, source: source.read(length), handle)
            stream.finish()
    except Exception as exc:
        if stream is not None:
            with contextlib.suppress(Exception):
                stream.abort()
        if isinstance(exc, SeedError):
            raise
        raise SeedError("could not upload guest seed ISO") from None
