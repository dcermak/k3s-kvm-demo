"""Run the installed shell program against a scratch guest filesystem."""

from __future__ import annotations

import fcntl
import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

from k3s_kvm_demo import seed
from k3s_kvm_demo.observer import parse_guest_status

FIRSTBOOT = Path(__file__).resolve().parents[1] / "firstboot"
SCRIPT = FIRSTBOOT / "k3s-demo-guest"
UUID = "12345678-1234-1234-1234-123456789abc"
CONFIG = b'{"node-name":"node-a","token":"$(touch /not-executed)"}\n'

SHIM = r"""#!/bin/sh
name=${0##*/}
printf '%s %s\n' "$name" "$*" >> "$K3S_DEMO_TEST_ROOT/calls"
case "$name" in
    systemctl)
        case "$1" in
            is-enabled|is-active) [ "${CLASH:-}" = "$3" ]; exit $? ;;
            show)
                case "$4" in
                    k3s-demo-prepare.service) printf '%s\n' "${PREPARE_STATE:-inactive}" ;;
                    k3s-node.service) printf '%s\n' "${K3S_STATE:-inactive}" ;;
                    *) exit 99 ;;
                esac ;;
            *) exit 99 ;;
        esac ;;
    hostnamectl) [ "${FAIL_HOSTNAME:-0}" = 0 ] ;;
    k3s)
        umask > "$K3S_DEMO_TEST_ROOT/k3s-umask"
        printf '%s\n' "$$" > "$K3S_DEMO_TEST_ROOT/k3s-pid" ;;
    mv)
        case "$*" in *"${FAIL_MV:-NO_MATCH}"*) exit 1 ;; esac
        exec /usr/bin/mv "$@" ;;
    sync)
        [ "${FAIL_SYNC:-0}" = 0 ] ;;
esac
"""


@dataclass
class Harness:
    root: Path
    bin: Path

    @property
    def state(self):
        return self.root / "var/lib/k3s-kvm-demo"

    @property
    def config(self):
        return self.root / "etc/rancher/k3s/config.yaml"

    @property
    def seed_dir(self):
        return self.root / "run/k3s-demo-seed"

    def environment(self, **env):
        return {
            **os.environ,
            "PATH": f"{self.bin}:{os.environ['PATH']}",
            "K3S_DEMO_TEST_ROOT": str(self.root),
            **env,
        }

    def run(self, command="prepare", **env):
        return subprocess.run(
            ["/bin/sh", str(SCRIPT), command],
            capture_output=True,
            text=True,
            timeout=10,
            env=self.environment(**env),
        )

    def status(self, **env):
        result = self.run("status", **env)
        assert result.returncode == 0, result.stderr
        assert not result.stderr
        lines = result.stdout.splitlines()
        assert [line.split("=", 1)[0] for line in lines] == [
            "protocol",
            "node_uuid",
            "prepared",
            "started",
            "prepare_state",
            "k3s_state",
            "error_code",
        ]
        return dict(line.split("=", 1) for line in lines)


@pytest.fixture
def harness(tmp_path):
    root = tmp_path / "guest"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in ("systemctl", "hostnamectl", "k3s", "mv", "sync"):
        shim = bin_dir / name
        shim.write_text(SHIM)
        shim.chmod(0o755)
    harness = Harness(root, bin_dir)
    harness.seed_dir.mkdir(parents=True)
    for name, value in seed.build_seed_payload(UUID, "node-a", "agent", CONFIG).items():
        (harness.seed_dir / name).write_bytes(value)
    return harness


def test_shell_syntax():
    subprocess.run(["/bin/sh", "-n", str(SCRIPT)], check=True)


def test_units_declare_prepare_before_k3s():
    prepare = (FIRSTBOOT / "k3s-demo-prepare.service").read_text()
    node = (FIRSTBOOT / "k3s-node.service").read_text()
    assert "Type=oneshot\nRemainAfterExit=yes" in prepare
    assert "ExecStart=/usr/local/libexec/k3s-demo-guest prepare" in prepare
    assert "Requires=k3s-demo-prepare.service" in node
    assert "After=network-online.target k3s-demo-prepare.service" in node
    assert "Type=notify" in node
    assert "ExecStart=/usr/local/libexec/k3s-demo-guest run" in node
    assert "ExecStartPost=/usr/local/libexec/k3s-demo-guest mark-started" in node
    assert "Restart=always\nRestartSec=5s" in node
    assert "StartLimitIntervalSec=0" in node
    assert "NotifyAccess=main" in node
    assert "TimeoutStartSec=300" in node
    assert "systemctl start" not in SCRIPT.read_text()
    assert "machine-id" not in SCRIPT.read_text()


def test_status_before_setup(harness):
    assert harness.status() == {
        "protocol": "1",
        "node_uuid": "",
        "prepared": "0",
        "started": "0",
        "prepare_state": "inactive",
        "k3s_state": "inactive",
        "error_code": "none",
    }
    assert not harness.state.exists()


def test_prepare_installs_files_but_does_not_start_k3s(harness):
    result = harness.run()
    assert result.returncode == 0, result.stderr
    assert result.stdout == result.stderr == ""
    assert harness.config.read_bytes() == CONFIG
    assert harness.config.stat().st_mode & 0o777 == 0o600
    assert harness.config.parent.stat().st_mode & 0o777 == 0o700
    assert harness.state.stat().st_mode & 0o777 == 0o700
    for name in ("prepare.lock", "prepared", "schema", "node_uuid", "hostname", "role"):
        assert (harness.state / name).stat().st_mode & 0o777 == 0o600
    assert (harness.root / "etc/hostname").read_text() == "node-a\n"
    assert (harness.state / "prepared").read_bytes() == b"1\n"
    assert not (harness.state / "started").exists()
    calls = (harness.root / "calls").read_text().splitlines()
    assert not any(line.startswith("k3s ") for line in calls)
    commit = next(i for i, line in enumerate(calls) if "prepared.tmp" in line)
    assert calls[commit - 1] == "sync "
    assert calls[commit + 1] == "sync "
    assert harness.status()["prepared"] == "1"
    assert not list(harness.root.rglob("*.tmp"))


def test_prepared_boot_needs_no_seed_and_accepts_existing_k3s_data(harness):
    assert harness.run().returncode == 0
    shutil.rmtree(harness.seed_dir)
    (harness.root / "var/lib/rancher/k3s").mkdir(parents=True)
    before = harness.config.stat().st_mtime_ns
    assert harness.run().returncode == 0
    assert harness.config.stat().st_mtime_ns == before
    assert harness.run("run").returncode == 0
    assert harness.status()["started"] == "0"


@pytest.mark.parametrize("damage", ["missing", "empty", "large", "permissions", "role", "hostname"])
def test_prepared_marker_never_bypasses_installed_validation(harness, damage):
    assert harness.run().returncode == 0
    if damage == "missing":
        harness.config.unlink()
    elif damage == "empty":
        harness.config.write_bytes(b"")
    elif damage == "large":
        harness.config.write_bytes(b"x" * (seed.CONFIG_MAX_BYTES + 1))
    elif damage == "permissions":
        harness.config.chmod(0o644)
    elif damage == "role":
        (harness.state / "role").write_text("server; id\n")
    else:
        (harness.root / "etc/hostname").write_text("other\n")
    assert harness.run().returncode != 0
    assert harness.run("run").returncode != 0
    assert harness.run("mark-started").returncode != 0
    assert harness.status()["error_code"] == "prepare-failed"


def test_started_is_history_not_current_health(harness):
    assert harness.run("mark-started").returncode != 0
    assert harness.run().returncode == 0
    assert harness.run("run").returncode == 0
    assert harness.run("mark-started").returncode == 0
    marker = harness.state / "started"
    assert marker.stat().st_mode & 0o777 == 0o600
    assert harness.config.stat().st_mode & 0o777 == 0o600
    before = marker.stat().st_mtime_ns
    assert harness.run("mark-started").returncode == 0
    assert marker.stat().st_mtime_ns == before
    status = harness.status(PREPARE_STATE="active", K3S_STATE="failed")
    assert status["prepared"] == status["started"] == "1"
    assert status["k3s_state"] == "failed"
    assert status["error_code"] == "k3s-failed"


def test_interrupted_started_write_keeps_prepared_and_can_retry_without_seed(harness):
    assert harness.run().returncode == 0
    shutil.rmtree(harness.seed_dir)
    assert harness.run("mark-started", FAIL_MV="started.tmp").returncode != 0
    assert harness.status()["prepared"] == "1"
    assert harness.status()["started"] == "0"
    assert harness.run("mark-started").returncode == 0
    assert harness.status()["started"] == "1"


def test_corrupt_prepared_marker_is_not_repaired_from_seed(harness):
    assert harness.run().returncode == 0
    (harness.state / "prepared").write_text("partial")
    assert harness.run().returncode != 0
    assert harness.run("run").returncode != 0
    assert harness.status()["error_code"] == "prepare-failed"


def test_server_role_is_read_from_installed_metadata(harness):
    (harness.seed_dir / "role").write_text("server\n")
    assert harness.run().returncode == 0
    shutil.rmtree(harness.seed_dir)
    assert harness.run("run").returncode == 0
    assert f"k3s server --config {harness.config}" in (harness.root / "calls").read_text()


def test_run_exec_preserves_pid_and_role(harness):
    assert harness.run("run").returncode != 0
    assert harness.run().returncode == 0
    with subprocess.Popen(
        ["/bin/sh", str(SCRIPT), "run"],
        env=harness.environment(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    ) as process:
        _, stderr = process.communicate(timeout=10)
        assert process.returncode == 0, stderr
        assert int((harness.root / "k3s-pid").read_text()) == process.pid
    assert int((harness.root / "k3s-umask").read_text(), 8) == 0o022
    assert f"k3s agent --config {harness.config}" in (harness.root / "calls").read_text()


@pytest.mark.parametrize("started", [False, True])
def test_data_before_prepared_is_always_rejected(harness, started):
    (harness.root / "var/lib/rancher/k3s").mkdir(parents=True)
    if started:
        harness.state.mkdir(parents=True)
        (harness.state / "started").write_text("1\n")
    assert harness.run().returncode != 0
    assert not harness.config.exists()


@pytest.mark.parametrize("unit", ["k3s.service", "k3s-server.service", "k3s-agent.service"])
def test_packaged_unit_must_not_race(harness, unit):
    assert harness.run(CLASH=unit).returncode != 0
    assert not (harness.state / "prepared").exists()


@pytest.mark.parametrize(
    "field,value",
    [
        ("schema", b"2\n"),
        ("node_uuid", b"not-a-uuid\n"),
        ("hostname", b"$(id)\n"),
        ("role", b"agent\nserver\n"),
        ("config.yaml", b""),
        ("config.yaml", b"x" * (seed.CONFIG_MAX_BYTES + 1)),
    ],
    ids=["schema", "uuid", "hostname", "role", "empty-config", "oversize-config"],
)
def test_invalid_seed_is_rejected_without_logging_contents(harness, field, value):
    (harness.seed_dir / field).write_bytes(value)
    result = harness.run()
    assert result.returncode != 0
    assert result.stdout == ""
    assert result.stderr == "prepare-failed\n"
    assert not (harness.state / "prepared").exists()


@pytest.mark.parametrize(
    "failure",
    [
        {"FAIL_MV": "config.yaml"},
        {"FAIL_MV": "/role"},
        {"FAIL_HOSTNAME": "1"},
        {"FAIL_SYNC": "1"},
        {"FAIL_MV": "prepared.tmp"},
    ],
)
def test_interrupted_prerequisites_do_not_commit_and_retry_recovers(harness, failure):
    result = harness.run(**failure)
    assert result.returncode != 0
    assert not (harness.state / "prepared").exists()
    assert not (harness.state / "started").exists()
    assert harness.run("run").returncode != 0
    result = harness.run()
    assert result.returncode == 0, result.stderr
    assert harness.config.read_bytes() == CONFIG
    assert harness.status()["prepared"] == "1"
    assert not list(harness.root.rglob("*.tmp"))


def test_uncommitted_marker_and_files_are_not_success(harness):
    harness.state.mkdir(parents=True)
    (harness.state / "prepared.tmp").write_text("1\n")
    (harness.state / "started.tmp").write_text("1\n")
    assert harness.status()["prepared"] == harness.status()["started"] == "0"
    assert harness.run().returncode == 0
    assert harness.status()["started"] == "0"


def test_prepare_serializes_with_flock(harness):
    harness.state.mkdir(parents=True)
    with (harness.state / "prepare.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert harness.run().returncode == 75
    assert harness.run().returncode == 0


def test_status_does_not_wait_for_prepare_lock(harness):
    harness.state.mkdir(parents=True)
    with (harness.state / "prepare.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        status = harness.status(PREPARE_STATE="activating")
        assert status["prepared"] == status["started"] == "0"
        assert status["prepare_state"] == "activating"
        assert status["error_code"] == "none"


@pytest.mark.parametrize(
    "phase,prepare_state,k3s_state,error_code",
    [
        ("fresh", "inactive", "inactive", "none"),
        ("partial", "activating", "inactive", "none"),
        ("prepared", "active", "activating", "none"),
        ("started", "active", "active", "none"),
        ("started", "active", "failed", "k3s-failed"),
        ("broken", "failed", "inactive", "prepare-failed"),
    ],
)
def test_observer_parses_actual_guest_output(harness, phase, prepare_state, k3s_state, error_code):
    if phase == "partial":
        assert harness.run(FAIL_MV="/role").returncode != 0
    elif phase != "fresh":
        assert harness.run().returncode == 0
        if phase == "started":
            assert harness.run("mark-started").returncode == 0
        elif phase == "broken":
            harness.config.unlink()
    result = harness.run("status", PREPARE_STATE=prepare_state, K3S_STATE=k3s_state)
    assert result.returncode == 0, result.stderr
    status = parse_guest_status(result.stdout)
    assert status.protocol == 1
    assert status.node_uuid == ("" if phase == "fresh" else UUID)
    assert status.prepared == (phase in {"prepared", "started"})
    assert status.started == (phase == "started")
    assert status.prepare_state == prepare_state
    assert status.k3s_state == k3s_state
    assert status.error_code == error_code


def test_status_reports_prepare_failure_and_bounds_systemctl_output(harness):
    status = harness.status(PREPARE_STATE="failed", K3S_STATE="injected\nextra=value")
    assert status["prepare_state"] == "failed"
    assert status["k3s_state"] == "unknown"
    assert status["error_code"] == "prepare-failed"


BUILD_SHIM = """import json
import os
import sys
from pathlib import Path

name = Path(sys.argv[0]).name
args = sys.argv[1:]
with open(os.environ['BUILD_CALLS'], 'a') as log:
    log.write(json.dumps([name, *args]) + '\\n')
if name == 'curl':
    Path(args[args.index('-o') + 1]).write_bytes(b'new image')
elif name == 'virt-customize':
    race = os.environ.get('BUILD_RACE')
    output = Path(os.environ['OUTPUT'])
    if race == 'file':
        output.write_bytes(b'other builder image')
    elif race == 'symlink':
        output.symlink_to(os.environ['BUILD_OLD_IMAGE'])
    elif race == 'directory-symlink':
        output.symlink_to(os.environ['BUILD_OLD_DIRECTORY'], target_is_directory=True)
elif name == 'install':
    if os.environ.get('BUILD_FAIL_INSTALL'):
        Path(args[-1]).write_bytes(b'partial copy')
        sys.exit(1)
    os.execv('/usr/bin/install', ['install', *args])
"""


@pytest.fixture
def builder(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    output_dir = tmp_path / "output directory"
    output_dir.mkdir()
    output = output_dir / "base image.qcow2"
    calls = tmp_path / "build-calls"
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    for command in ("curl", "qemu-img", "virt-customize", "install"):
        shim = bin_dir / command
        shim.write_text(f"#!{sys.executable}\n" + BUILD_SHIM)
        shim.chmod(0o755)

    def run(**env):
        return subprocess.run(
            ["bash", str(FIRSTBOOT.parent / "scripts/build-image.sh")],
            capture_output=True,
            text=True,
            timeout=10,
            env={
                **os.environ,
                "PATH": f"{bin_dir}:{os.environ['PATH']}",
                "OUTPUT": str(output),
                "BUILD_CALLS": str(calls),
                "TMPDIR": str(scratch),
                "ROOT_PASSWORD": "",
                **env,
            },
        )

    return run, output, calls, scratch


@pytest.mark.parametrize("kind", ["file", "directory", "symlink", "dangling-symlink"])
def test_builder_rejects_existing_output_before_download(builder, tmp_path, kind):
    run, output, calls, scratch = builder
    old_image = tmp_path / "old-image"
    old_image.write_bytes(b"old image")
    if kind == "file":
        output.write_bytes(b"old image")
    elif kind == "directory":
        output.mkdir()
    else:
        output.symlink_to(old_image if kind == "symlink" else tmp_path / "missing")
    result = run()
    assert result.returncode != 0
    assert "Refusing to overwrite" in result.stderr
    assert not calls.exists()
    assert old_image.read_bytes() == b"old image"
    if kind == "file":
        assert output.read_bytes() == b"old image"
    elif kind.endswith("symlink"):
        assert output.is_symlink()
    assert not list(scratch.iterdir())


def test_builder_rejects_missing_output_directory_before_download(builder):
    run, output, calls, _scratch = builder
    result = run(OUTPUT=str(output.parent / "missing" / "image.qcow2"))
    assert result.returncode != 0
    assert "Output directory must exist and be writable" in result.stderr
    assert not calls.exists()


@pytest.mark.parametrize("race", ["file", "symlink", "directory-symlink"])
def test_builder_publication_never_clobbers_racing_output(builder, tmp_path, race):
    run, output, _calls, scratch = builder
    old_image = tmp_path / "old-image"
    old_image.write_bytes(b"old image")
    old_directory = tmp_path / "old-directory"
    old_directory.mkdir()
    result = run(
        BUILD_RACE=race,
        BUILD_OLD_IMAGE=str(old_image),
        BUILD_OLD_DIRECTORY=str(old_directory),
    )
    assert result.returncode != 0
    assert old_image.read_bytes() == b"old image"
    assert not list(old_directory.iterdir())
    if race == "file":
        assert output.read_bytes() == b"other builder image"
    else:
        assert output.is_symlink()
    assert list(output.parent.iterdir()) == [output]
    assert not list(scratch.iterdir())


def test_builder_cleans_partial_staging_file(builder):
    run, output, _calls, scratch = builder
    result = run(BUILD_FAIL_INSTALL="1")
    assert result.returncode != 0
    assert not output.exists()
    assert not list(output.parent.iterdir())
    assert not list(scratch.iterdir())


@pytest.mark.parametrize("dbus_alias", [False, True])
def test_builder_publishes_image_and_leaves_empty_machine_id(builder, tmp_path, dbus_alias):
    run, output, calls, scratch = builder
    result = run()
    assert result.returncode == 0, result.stderr
    assert output.read_bytes() == b"new image"
    assert output.stat().st_mode & 0o777 == 0o644
    assert list(output.parent.iterdir()) == [output]
    assert not list(scratch.iterdir())
    invocations = [json.loads(line) for line in calls.read_text().splitlines()]
    customize = next(args for command, *args in invocations if command == "virt-customize")
    commands = [customize[i + 1] for i, arg in enumerate(customize) if arg == "--run-command"]
    reset = next(command for command in commands if "/var/lib/dbus/machine-id" in command)
    assert reset == (
        "rm -f /var/lib/dbus/machine-id /etc/machine-id /var/lib/systemd/random-seed "
        "&& install -m 0644 /dev/null /etc/machine-id"
    )
    assert "systemctl enable k3s-node.service" in commands
    assert "systemctl mask k3s.service k3s-server.service k3s-agent.service" in commands
    assert "mkdir -p /etc/cloud; touch /etc/cloud/cloud-init.disabled" in commands
    assert not any("systemctl preset" in command for command in commands)
    # Execute only the identity-reset command, with every guest path redirected.
    root = tmp_path / "guest"
    for path in ("etc", "var/lib/dbus", "var/lib/systemd"):
        (root / path).mkdir(parents=True)
    protected = root / "protected"
    protected.write_bytes(b"do not truncate")
    (root / "etc/machine-id").symlink_to(protected)
    dbus_id = root / "var/lib/dbus/machine-id"
    if dbus_alias:
        dbus_id.symlink_to(root / "etc/machine-id")
    else:
        dbus_id.write_text("stale dbus ID\n")
    random_seed = root / "var/lib/systemd/random-seed"
    random_seed.write_bytes(b"stale random seed")
    for path in ("/var/lib/dbus/machine-id", "/etc/machine-id", "/var/lib/systemd/random-seed"):
        reset = reset.replace(path, f"'{root}{path}'")
    subprocess.run(["/bin/sh", "-c", reset], check=True)
    assert (root / "etc/machine-id").read_bytes() == b""
    assert not (root / "etc/machine-id").is_symlink()
    assert (root / "etc/machine-id").stat().st_mode & 0o777 == 0o644
    assert not dbus_id.exists() and not dbus_id.is_symlink()
    assert not random_seed.exists()
    assert protected.read_bytes() == b"do not truncate"
