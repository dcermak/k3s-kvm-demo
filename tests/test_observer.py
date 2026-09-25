"""Deterministic observer tests with no guest or libvirt RPCs."""

import base64
import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import UUID

import libvirt
import pytest

from k3s_kvm_demo import guestexec, libvirtctl, meta
from k3s_kvm_demo.observer import (
    ERROR_CODES,
    GUEST_PATH,
    MAX_CALLS_PER_PASS,
    MAX_STATUS_BYTES,
    Observer,
    parse_guest_status,
)

from .conftest import fake_libvirt_error
from .test_guest_image import UUID as GUEST_UUID, harness as harness


def status_text(uuid, **changes):
    fields = dict(
        protocol="1",
        node_uuid=uuid,
        prepared="1",
        started="1",
        prepare_state="active",
        k3s_state="active",
        error_code="none",
    )
    fields.update(changes)
    return "".join(f"{key}={value}\n" for key, value in fields.items())


@pytest.fixture
def clock():
    return SimpleNamespace(value=0.0)


@pytest.fixture
def cfg():
    return SimpleNamespace(
        observation=SimpleNamespace(interval_s=2, qga_timeout_s=2, stale_after_s=30),
        maintenance=SimpleNamespace(shutdown_grace_s=0.1),
    )


@pytest.fixture
def manager():
    node = libvirtctl.Node(
        name="k3s-node-" + "0" * 31 + "1",
        uuid=str(UUID(int=1)),
        role=meta.ROLE_SERVER,
        bootstrap=True,
        volume="node.qcow2",
        created="",
        generation=1,
        state=meta.BOOTING,
        error=None,
        power=libvirtctl.POWER_RUNNING,
        ip="192.0.2.1",
        vcpus=2,
        memory_mb=1024,
    )
    manager = Mock()
    manager.nodes = [node]
    manager.list_nodes.side_effect = lambda: list(manager.nodes)

    def update(uuid, generation, state, *, error=None, expected_states=None):
        for index, current in enumerate(manager.nodes):
            if current.uuid == uuid and current.generation == generation:
                if expected_states is not None and current.state not in expected_states:
                    return False
                manager.nodes[index] = replace(current, state=state, error=error)
                return True
        return False

    manager.update_state.side_effect = update
    return manager


@pytest.fixture
def agent(manager):
    agent = Mock()
    agent.start.return_value = 123
    agent.poll.side_effect = lambda _pid: guestexec.ExecResult(
        0, None, status_text(manager.nodes[0].uuid), ""
    )
    return agent


@pytest.fixture
def observer(manager, cfg, agent, clock):
    observer = Observer(
        manager, cfg, agent_factory=lambda uuid, timeout: agent, now=lambda: clock.value
    )
    yield observer
    observer.shutdown()


def observe(observer):
    observer.reconcile()  # launch
    observer.reconcile()  # reap


def test_status_parser():
    status = parse_guest_status(status_text(str(UUID(int=1))))
    assert status.protocol == 1
    assert status.prepared and status.started
    assert status.node_uuid == str(UUID(int=1))
    assert {"none", "prepare-failed", "k3s-failed"} == ERROR_CODES


@pytest.mark.parametrize(
    "changes",
    [
        {"protocol": "2"},
        {"protocol": "01"},
        {"prepared": "true"},
        {"started": "2"},
        {"node_uuid": "not-a-uuid"},
        {"node_uuid": ""},
        {"error_code": "arbitrary guest text"},
        {"prepare_state": "unexpected-state"},
        {"k3s_state": "active\r"},
        {"extra": "value"},
    ],
)
def test_status_rejects_invalid_fields(changes):
    with pytest.raises(ValueError):
        parse_guest_status(status_text(str(UUID(int=1)), **changes))


@pytest.mark.parametrize(
    "text",
    [
        "",
        "x" * (MAX_STATUS_BYTES + 1),
        "\N{SNOWMAN}",
        status_text(str(UUID(int=1))) + "protocol=1\n",
        status_text(str(UUID(int=1))).replace("protocol=1\n", ""),
        status_text(str(UUID(int=1))) + "\n",
    ],
)
def test_status_rejects_invalid_shape(text):
    with pytest.raises(ValueError):
        parse_guest_status(text)


def test_guest_owned_success(observer, manager, agent):
    observe(observer)
    node = manager.nodes[0]
    assert node.state == meta.CONFIGURED
    assert manager.join_ready(node)
    assert observer.decorate([node])[0].service == libvirtctl.SERVICE_ACTIVE
    agent.start.assert_called_once_with(
        "/usr/bin/timeout", ["--kill-after=1s", "3s", GUEST_PATH, "status"]
    )
    agent.poll.assert_called_once_with(123)
    agent.ping.assert_not_called()


@pytest.mark.parametrize(
    "prepared,started,prepare_state,code,state",
    [
        ("0", "0", "activating", "none", meta.BOOTING),
        ("1", "0", "active", "none", meta.CONFIGURING),
        ("0", "0", "failed", "prepare-failed", meta.FAILED),
        ("1", "1", "failed", "prepare-failed", meta.CONFIGURED),
        ("0", "1", "activating", "none", meta.CONFIGURED),
    ],
)
def test_durable_transitions(
    observer, manager, agent, prepared, started, prepare_state, code, state
):
    agent.poll.side_effect = lambda _pid: guestexec.ExecResult(
        0,
        None,
        status_text(
            manager.nodes[0].uuid,
            prepared=prepared,
            started=started,
            prepare_state=prepare_state,
            error_code=code,
        ),
        "",
    )
    observe(observer)
    assert manager.nodes[0].state == state
    assert not observer.join_ready(manager.nodes[0])


def test_failed_prepare_can_recover(observer, manager):
    manager.nodes[0] = replace(manager.nodes[0], state=meta.FAILED, error="prepare-failed")
    observe(observer)
    assert manager.nodes[0].state == meta.CONFIGURED
    assert manager.nodes[0].error is None


@pytest.mark.parametrize(
    "changes",
    [
        {"k3s_state": "inactive"},
        {"prepare_state": "failed", "error_code": "prepare-failed"},
        {"k3s_state": "failed", "error_code": "k3s-failed"},
        {"started": "0", "prepared": "0", "prepare_state": "activating"},
    ],
)
def test_configured_is_historical(observer, manager, agent, changes):
    observe(observer)
    agent.poll.side_effect = lambda _pid: guestexec.ExecResult(
        0, None, status_text(manager.nodes[0].uuid, **changes), ""
    )
    observe(observer)
    assert manager.nodes[0].state == meta.CONFIGURED
    assert not observer.join_ready(manager.nodes[0])
    if "error_code" in changes:
        assert observer.decorate(manager.nodes)[0].progress == changes["error_code"]
        assert manager.nodes[0].error == changes["error_code"]


@pytest.mark.parametrize("field", ["prepare_state", "k3s_state"])
def test_unknown_service_state_is_not_ready(observer, manager, agent, field):
    agent.poll.side_effect = lambda _pid: guestexec.ExecResult(
        0, None, status_text(manager.nodes[0].uuid, **{field: "unknown"}), ""
    )
    observe(observer)
    assert manager.nodes[0].state == meta.CONFIGURED
    assert not observer.join_ready(manager.nodes[0])
    if field == "k3s_state":
        assert observer.decorate(manager.nodes)[0].service == libvirtctl.SERVICE_UNKNOWN


def test_guest_script_current_service_failure_and_recovery(harness, observer, manager, agent):
    manager.nodes[0] = replace(manager.nodes[0], uuid=GUEST_UUID)
    assert harness.run("prepare").returncode == 0
    assert harness.run("mark-started").returncode == 0
    for state, service, error in (
        ("active", libvirtctl.SERVICE_ACTIVE, None),
        ("failed", libvirtctl.SERVICE_INACTIVE, "k3s-failed"),
        ("unexpected\nextra=value", libvirtctl.SERVICE_UNKNOWN, None),
        ("active", libvirtctl.SERVICE_ACTIVE, None),
    ):
        result = harness.run("status", PREPARE_STATE="active", K3S_STATE=state)
        assert result.returncode == 0, result.stderr
        agent.poll.side_effect = None
        agent.poll.return_value = guestexec.ExecResult(0, None, result.stdout, result.stderr)
        observe(observer)
        node = observer.decorate(manager.nodes)[0]
        assert node.state == meta.CONFIGURED
        assert node.service == service
        assert node.error == node.progress == error
        assert observer.join_ready(node) == (state == "active")


def test_guest_script_preidentity_failure_is_diagnostic(harness, observer, manager, agent):
    result = harness.run("status", PREPARE_STATE="failed", K3S_STATE="unexpected")
    assert result.returncode == 0, result.stderr
    status = parse_guest_status(result.stdout)
    assert status.node_uuid == ""
    assert status.k3s_state == "unknown"
    agent.poll.side_effect = None
    agent.poll.return_value = guestexec.ExecResult(0, None, result.stdout, result.stderr)
    observe(observer)
    manager.update_state.assert_not_called()
    node = observer.decorate(manager.nodes)[0]
    assert node.progress == "prepare-failed"
    assert node.service == libvirtctl.SERVICE_UNKNOWN
    assert not observer.join_ready(node)


@pytest.fixture
def qemu_agent(manager, monkeypatch):
    connection = Mock()
    manager.cm.call.side_effect = lambda fn: fn(connection)
    command = Mock()
    monkeypatch.setattr(guestexec.libvirt_qemu, "qemuAgentCommand", command)
    return guestexec.QemuAgent(manager.cm, manager.nodes[0].uuid, timeout_s=2), command


@pytest.mark.parametrize(
    "message",
    [
        "internal error: guest agent command failed: Failed to execute guest-exec-status: "
        "PID 123 does not exist",
        "internal error: guest agent command failed: PID not found",
    ],
)
def test_qemu_missing_pid_retires_job_and_allows_new_probe(manager, cfg, qemu_agent, message):
    agent, command = qemu_agent
    error = fake_libvirt_error(libvirt.VIR_ERR_INTERNAL_ERROR, message)
    command.side_effect = [
        '{"return":{"pid":123}}',
        error,
        '{"return":{"pid":124}}',
        json.dumps(
            {
                "return": {
                    "exited": True,
                    "exitcode": 0,
                    "out-data": base64.b64encode(
                        status_text(manager.nodes[0].uuid).encode()
                    ).decode(),
                }
            }
        ),
    ]
    observer = Observer(manager, cfg, agent_factory=lambda uuid, timeout: agent)
    observe(observer)
    assert not observer._pending
    assert not observer.join_ready(manager.nodes[0])
    observe(observer)
    assert observer.join_ready(manager.nodes[0])
    assert [json.loads(call.args[1])["execute"] for call in command.call_args_list] == [
        "guest-exec",
        "guest-exec-status",
        "guest-exec",
        "guest-exec-status",
    ]
    manager.cm.read.assert_not_called()


@pytest.mark.parametrize(
    "code,message",
    [
        (
            libvirt.VIR_ERR_INTERNAL_ERROR,
            "internal error: guest agent command failed: transport lost",
        ),
        (libvirt.VIR_ERR_INTERNAL_ERROR, "internal error: PID 123 does not exist"),
        (
            libvirt.VIR_ERR_INTERNAL_ERROR,
            "internal error: guest agent command failed: socket not found",
        ),
        (libvirt.VIR_ERR_OPERATION_TIMEOUT, "guest agent command failed: PID 123 does not exist"),
    ],
)
def test_qemu_ambiguous_failure_keeps_pending_pid(manager, cfg, qemu_agent, code, message):
    agent, command = qemu_agent
    error = fake_libvirt_error(code, message)
    command.side_effect = ['{"return":{"pid":123}}', error, error]
    observer = Observer(manager, cfg, agent_factory=lambda uuid, timeout: agent)
    observe(observer)
    observer.reconcile()
    assert observer._pending[manager.nodes[0].uuid].pid == 123
    assert not observer.join_ready(manager.nodes[0])
    assert [json.loads(call.args[1])["execute"] for call in command.call_args_list] == [
        "guest-exec",
        "guest-exec-status",
        "guest-exec-status",
    ]


def test_qemu_missing_pid_translation_is_scoped_to_status(qemu_agent):
    agent, command = qemu_agent
    error = fake_libvirt_error(
        libvirt.VIR_ERR_INTERNAL_ERROR, "guest agent command failed: PID 123 does not exist"
    )
    command.side_effect = error
    with pytest.raises(libvirt.libvirtError) as caught:
        agent.start("/bin/true", [])
    assert caught.value is error
    with pytest.raises(guestexec.AgentError) as caught:
        agent.poll(123)
    assert caught.value.__cause__ is error
    assert command.call_count == 2


def test_preidentity_failure_is_diagnostic_only(observer, manager, agent):
    agent.poll.side_effect = lambda _pid: guestexec.ExecResult(
        0,
        None,
        status_text(
            "",
            prepared="0",
            started="0",
            prepare_state="failed",
            error_code="prepare-failed",
        ),
        "",
    )
    observe(observer)
    manager.update_state.assert_not_called()
    decorated = observer.decorate(manager.nodes)[0]
    assert decorated.progress == "prepare-failed"
    assert decorated.service == libvirtctl.SERVICE_UNKNOWN
    assert not observer.join_ready(decorated)


def test_wrong_identity_is_unknown(observer, manager, agent):
    agent.poll.side_effect = lambda _pid: guestexec.ExecResult(
        0, None, status_text(str(UUID(int=2))), ""
    )
    observe(observer)
    manager.update_state.assert_not_called()
    assert observer.decorate(manager.nodes)[0].service == libvirtctl.SERVICE_UNKNOWN
    assert not observer.join_ready(manager.nodes[0])


@pytest.mark.parametrize("exitcode,signal", [(124, None), (0, 9)])
def test_unsuccessful_exec_cannot_report_success(observer, manager, agent, exitcode, signal):
    agent.poll.side_effect = lambda _pid: guestexec.ExecResult(
        exitcode, signal, status_text(manager.nodes[0].uuid), ""
    )
    observe(observer)
    manager.update_state.assert_not_called()
    assert not observer.join_ready(manager.nodes[0])


def test_state_update_is_conditional(observer, manager, agent):
    def poll(_pid):
        node = manager.nodes[0]
        manager.nodes[0] = replace(node, state=meta.DELETING)
        return guestexec.ExecResult(0, None, status_text(node.uuid), "")

    agent.poll.side_effect = poll
    observe(observer)
    assert manager.nodes[0].state == meta.DELETING
    assert manager.update_state.call_args.kwargs["expected_states"] == {meta.BOOTING}
    assert not observer.join_ready(manager.nodes[0])


def test_missing_agent_is_not_failed(observer, manager, agent):
    agent.start.side_effect = guestexec.AgentUnavailable("missing QGA")
    observe(observer)
    assert manager.nodes[0].state == meta.BOOTING
    assert observer.decorate(manager.nodes)[0].service == libvirtctl.SERVICE_UNKNOWN


def test_cache_requires_recent_same_generation_running_node(observer, manager, clock):
    observe(observer)
    node = manager.nodes[0]
    assert observer.join_ready(node)
    for changed in (replace(node, generation=2), replace(node, power=libvirtctl.POWER_SHUT_OFF)):
        assert not observer.join_ready(changed)
        assert observer.decorate([changed])[0].service == libvirtctl.SERVICE_UNKNOWN
    clock.value = 30
    assert not observer.join_ready(node)
    assert observer.decorate([node])[0].service == libvirtctl.SERVICE_UNKNOWN


def test_stopped_node_keeps_state_and_clears_cache(observer, manager, agent):
    observe(observer)
    node = manager.nodes[0]
    manager.nodes[0] = replace(node, power=libvirtctl.POWER_SHUT_OFF)
    observer.reconcile()
    assert manager.nodes[0].state == meta.CONFIGURED
    assert not observer.join_ready(node)
    assert agent.start.call_count == 1


def test_known_pid_never_relaunched_on_timeout(observer, manager, agent, clock):
    agent.poll.side_effect = None
    agent.poll.return_value = None
    observer.reconcile()
    for cycle in range(1, 20):
        clock.value = cycle * 10
        observer.reconcile()
    assert agent.start.call_count == 1
    assert agent.poll.call_count == 19
    assert observer.decorate(manager.nodes)[0].service == libvirtctl.SERVICE_UNKNOWN
    agent.poll.side_effect = guestexec.AgentError("pid no longer exists")
    observer.reconcile()
    observer.reconcile()
    assert agent.start.call_count == 2


def test_unavailable_poll_retains_pid(observer, agent, clock):
    observer.reconcile()
    agent.poll.side_effect = guestexec.AgentUnavailable("timeout")
    for _ in range(5):
        clock.value += 10
        observer.reconcile()
    assert agent.start.call_count == 1
    assert agent.poll.call_count == 5


def test_lost_launch_is_rate_limited(observer, agent, clock):
    agent.start.side_effect = guestexec.AgentUnavailable("lost reply")
    observe(observer)
    clock.value = 3
    observer.reconcile()
    assert agent.start.call_count == 1
    clock.value = 4
    observe(observer)
    assert agent.start.call_count == 2


def test_disappeared_domain_releases_pending(observer, manager, agent):
    observer.reconcile()
    manager.nodes = []
    observer.reconcile()
    assert not observer._pending
    assert not observer._observations
    agent.poll.assert_not_called()


def test_generation_change_discards_old_result(observer, manager):
    observer.reconcile()
    manager.nodes[0] = replace(manager.nodes[0], generation=2)
    observer.reconcile()
    manager.update_state.assert_not_called()
    assert not observer.join_ready(manager.nodes[0])


def test_late_success_is_unknown(observer, manager, clock):
    observer.reconcile()
    clock.value = 30
    observer.reconcile()
    manager.update_state.assert_not_called()
    assert not observer.join_ready(manager.nodes[0])
    assert observer.decorate(manager.nodes)[0].service == libvirtctl.SERVICE_UNKNOWN


def test_paused_domain_retains_pending_pid(observer, manager, agent):
    observer.reconcile()
    node = manager.nodes[0]
    manager.nodes[0] = replace(node, power="paused")
    observer.reconcile()
    agent.poll.assert_not_called()
    manager.nodes[0] = node
    observer.reconcile()
    assert agent.start.call_count == 1
    agent.poll.assert_called_once_with(123)


def test_decorate_and_join_do_not_wait_for_rpc(observer, manager, agent):
    import threading

    entered, release = threading.Event(), threading.Event()

    def blocked_start(*args):
        entered.set()
        release.wait(2)
        return 123

    agent.start.side_effect = blocked_start
    observer.start()
    assert entered.wait(1)
    try:
        assert not observer.join_ready(manager.nodes[0])
        assert observer.decorate(manager.nodes)[0].service == libvirtctl.SERVICE_UNKNOWN
    finally:
        release.set()


def test_one_bad_node_does_not_block_others(manager, cfg):
    first = manager.nodes[0]
    second = replace(first, uuid=str(UUID(int=2)))
    manager.nodes = [first, second]
    agent = Mock(start=Mock(return_value=123))

    def factory(uuid, timeout):
        if uuid == first.uuid:
            raise guestexec.AgentUnavailable("not ready")
        return agent

    observer = Observer(manager, cfg, agent_factory=factory)
    observer.reconcile()
    agent.start.assert_called_once()


def test_unknown_durable_state_is_untouched(observer, manager, agent):
    manager.nodes[0] = replace(manager.nodes[0], state="future-state")
    observer.reconcile()
    agent.start.assert_not_called()
    manager.update_state.assert_not_called()
    manager.delete_if_current.assert_not_called()


@pytest.mark.parametrize("state", list(meta.INCOMPLETE))
def test_cleanup_is_conditional(observer, manager, agent, state):
    node = replace(manager.nodes[0], state=state)
    manager.nodes[0] = node
    observer.reconcile()
    manager.delete_if_current.assert_called_once_with(node.uuid, node.generation, state)
    agent.start.assert_not_called()


def test_fair_bounded_scan(manager, cfg, clock):
    manager.nodes = [replace(manager.nodes[0], uuid=str(UUID(int=i + 1))) for i in range(20)]
    factory = Mock(side_effect=lambda uuid, timeout: Mock(start=Mock(return_value=123)))
    observer = Observer(manager, cfg, agent_factory=factory, now=lambda: clock.value)
    observer.reconcile()
    assert factory.call_count == MAX_CALLS_PER_PASS
    observer.reconcile()
    assert factory.call_count == 2 * MAX_CALLS_PER_PASS
    observer.reconcile()
    assert factory.call_count == 20
    assert {call.args[0] for call in factory.call_args_list} == {n.uuid for n in manager.nodes}
    assert all(call.args[1] == 2 for call in factory.call_args_list)


def test_rpc_timeout_is_capped(manager, cfg, agent):
    cfg.observation.qga_timeout_s = 20
    factory = Mock(return_value=agent)
    Observer(manager, cfg, agent_factory=factory).reconcile()
    factory.assert_called_once_with(manager.nodes[0].uuid, 2)


def test_shutdown_joins_within_grace(observer, manager):
    import threading

    entered, release = threading.Event(), threading.Event()

    def blocked_list():
        entered.set()
        release.wait(2)
        return manager.nodes

    manager.list_nodes.side_effect = blocked_list
    observer.start()
    observer.start()
    assert entered.wait(1)
    try:
        assert not observer.shutdown()
    finally:
        release.set()
        observer._thread.join(1)
    assert observer.shutdown()


def test_run_checks_cancellation_before_start(agent):
    with pytest.raises(guestexec.ExecTimeout, match="before starting"):
        guestexec.run(agent, "/bin/true", [], timeout_s=2, should_stop=lambda: True)
    agent.start.assert_not_called()


def test_run_does_not_retry_lost_launch(agent):
    agent.start.side_effect = guestexec.AgentUnavailable("lost reply")
    with pytest.raises(guestexec.AgentUnavailable):
        guestexec.run(agent, "/bin/true", [], timeout_s=2)
    agent.start.assert_called_once()
    agent.poll.assert_not_called()


@pytest.mark.parametrize("slow_call", ["start", "poll"])
def test_run_deadline_includes_all_rpcs(agent, clock, slow_call):
    def slow(*args, **kwargs):
        clock.value += 3
        return 123 if slow_call == "start" else guestexec.ExecResult(0, None, "", "")

    getattr(agent, slow_call).side_effect = slow
    with pytest.raises(guestexec.ExecTimeout):
        guestexec.run(agent, "/bin/true", [], timeout_s=2, now=lambda: clock.value)
    assert agent.start.call_count == 1
    assert agent.poll.call_count == (slow_call == "poll")


def test_run_sleep_does_not_exceed_remaining_budget(agent, clock):
    agent.poll.side_effect = None
    agent.poll.return_value = None
    sleeps = []

    def sleep(seconds):
        sleeps.append(seconds)
        clock.value += seconds

    with pytest.raises(guestexec.ExecTimeout):
        guestexec.run(
            agent,
            "/bin/true",
            [],
            timeout_s=1,
            interval_s=2,
            now=lambda: clock.value,
            sleep=sleep,
        )
    assert sleeps == [1]
    assert agent.start.call_count == agent.poll.call_count == 1
