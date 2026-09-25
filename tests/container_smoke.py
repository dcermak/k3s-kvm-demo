"""Run via container stdin: python - < tests/container_smoke.py (no host mounts)."""

import json
import subprocess
import tempfile
from pathlib import Path

import libvirt
import libvirt_qemu

from k3s_kvm_demo import app, seed


def main():
    assert libvirt.getVersion() > 0
    assert callable(libvirt_qemu.qemuAgentCommand)
    subprocess.run(["k3s-demo", "--help"], check=True, capture_output=True)
    for name in ("index.html", "_card.html", "_grid.html", "_macros.html"):
        app.TEMPLATES.get_template(name)
    for name in ("app.css", "htmx.min.js", "notifications.js"):
        assert (app.HERE / "static" / name).stat().st_size > 0

    info = json.loads(subprocess.check_output([
        "qemu-img", "info", "--output=json",
        "/usr/share/k3s-kvm-demo/k3s-image.qcow2",
    ]))
    assert info["format"] == "qcow2", info
    assert not info.get("backing-filename"), info
    assert 0 < info["virtual-size"] <= 24 * 1024**3, info

    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        payload = seed.build_seed_payload(
            "12345678-1234-1234-1234-123456789abc", "smoke-node", "agent",
            b'token: container-smoke\nserver: https://192.0.2.1:6443\n',
        )
        iso = root / "seed.iso"
        seed.build_seed_iso(payload, iso)
        # Read the ISO back with the installed tool, rather than checking size alone.
        extracted = root / "extracted"
        subprocess.run([
            "xorriso", "-osirrox", "on", "-indev", str(iso),
            "-extract", "/", str(extracted),
        ], check=True, capture_output=True)
        for name, expected in payload.items():
            assert (extracted / name).read_bytes() == expected, name
    print("Container package, assets, qcow2, and seed ISO round-trip passed.")


if __name__ == "__main__":
    main()
