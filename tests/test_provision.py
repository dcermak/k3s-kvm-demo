"""The provisioning state machine.

Driven against a fake guest agent, so no VM and no real waiting is involved;
libvirt underneath is still the real test driver, so the durable state
transitions are genuine.
"""

from __future__ import annotations

import base64


from k3s_kvm_demo import guestexec, k3sconf, meta, provision

from .conftest import is_firstboot, is_probe, result


def deploy(manager, provisioner, role=meta.ROLE_SERVER):
    node, server_url = manager.create(role)
    provisioner.submit(node, server_url)
    return manager.get(node.name)


def test_a_successful_firstboot_reaches_configured(manager, provisioner, agent):
    agent.responses = [(is_firstboot, result(0, stdout="firstboot complete"))]
    node = deploy(manager, provisioner)

    assert node.state == meta.CONFIGURED
    assert node.error is None
    assert provisioner.in_flight == 0


def test_the_script_is_delivered_on_stdin_under_a_marker(manager, provisioner, agent):
    agent.responses = [(is_firstboot, result(0))]
    node = deploy(manager, provisioner)

    call = agent.calls[0]
    assert call.path == "/bin/sh"
    assert call.args[0] == "-s"
    # The marker is what pkill -f can find if the run has to be killed.
    assert call.args[1] == f"{k3sconf.PROCESS_MARKER}-{node.uuid.replace('-', '')}"
    assert call.input_data is not None
    assert "firstboot complete" in call.input_data


def test_the_delivered_script_carries_this_node_s_config(manager, provisioner, agent):
    import re

    import yaml

    agent.responses = [(is_firstboot, result(0))]
    node = deploy(manager, provisioner)

    blob = re.findall(r"printf %s '([A-Za-z0-9+/=]+)'", agent.calls[0].input_data)[0]
    document = yaml.safe_load(base64.b64decode(blob).decode())
    assert document["node-name"] == node.name
    assert document["cluster-init"] is True


def test_an_agent_that_never_answers_fails_the_node(manager, provisioner, agent, cfg):
    agent.pings_until_up = 10_000
    node = deploy(manager, provisioner)

    assert node.state == meta.FAILED
    assert "guest agent did not respond" in node.error
    assert "qemu-guest-agent" in node.error, "the error should say what to check"
    assert agent.calls == [], "firstboot must not run without a reachable agent"


def test_a_slow_agent_is_waited_for(manager, provisioner, agent):
    agent.pings_until_up = 3
    agent.responses = [(is_firstboot, result(0))]
    node = deploy(manager, provisioner)
    assert node.state == meta.CONFIGURED


def test_a_failing_script_records_its_last_line(manager, provisioner, agent):
    agent.responses = [
        (is_firstboot, result(1, stdout="starting\n", stderr="k3s is not on PATH\n"))
    ]
    node = deploy(manager, provisioner)

    assert node.state == meta.FAILED
    assert "exit 1" in node.error
    assert "k3s is not on PATH" in node.error


def test_the_captured_log_is_kept_for_display(manager, provisioner, agent):
    agent.responses = [(is_firstboot, result(1, stdout="out line", stderr="err line"))]
    deploy(manager, provisioner)

    node = provisioner.decorate(manager.list_nodes())[0]
    assert "out line" in node.log
    assert "err line" in node.log


def test_the_token_never_reaches_the_stored_log(manager, provisioner, agent, cfg):
    leak = f"joining with token {cfg.cluster.token} now"
    agent.responses = [(is_firstboot, result(1, stderr=leak))]
    deploy(manager, provisioner)

    node = provisioner.decorate(manager.list_nodes())[0]
    assert cfg.cluster.token not in node.log
    assert "***" in node.log
    assert cfg.cluster.token not in (node.error or "")


def test_the_log_is_capped(manager, provisioner, agent, cfg):
    agent.responses = [(is_firstboot, result(1, stdout="x" * 5000))]
    deploy(manager, provisioner)

    node = provisioner.decorate(manager.list_nodes())[0]
    assert len(node.log) <= cfg.firstboot.log_tail_bytes + 1
    assert node.log.startswith("…")


def test_truncated_guest_output_is_marked_as_such():
    """A clipped log must never read as a complete one."""
    decoded = guestexec._decode(base64.b64encode(b"partial").decode(), True)
    assert decoded == "partial" + guestexec.TRUNCATION_NOTE
    assert guestexec._decode(None, True) == guestexec.TRUNCATION_NOTE
    assert guestexec._decode(None, False) == ""


# -- exit 75: another run holds the guest-side lock ------------------------


def test_exit_75_is_retried_rather_than_treated_as_failure(manager, provisioner, agent):
    attempts = {"n": 0}

    def firstboot():
        attempts["n"] += 1
        return result(k3sconf.EX_TEMPFAIL) if attempts["n"] == 1 else result(0)

    agent.responses = [(is_firstboot, firstboot), (is_probe, result(1))]
    node = deploy(manager, provisioner)

    assert attempts["n"] == 2, "the script is re-executed, not polled"
    assert node.state == meta.CONFIGURED


def test_exit_75_stops_early_when_the_other_run_already_finished(manager, provisioner, agent):
    """75 means a *completed* process of ours; the run holding the lock may
    well have done the job, so check before re-executing."""
    agent.responses = [
        (is_firstboot, result(k3sconf.EX_TEMPFAIL)),
        (is_probe, result(0)),
    ]
    node = deploy(manager, provisioner)

    firstboot_calls = [c for c in agent.calls if c.args[:1] == ["-s"]]
    assert len(firstboot_calls) == 1
    assert node.state == meta.CONFIGURED


def test_a_lock_that_is_never_released_eventually_fails(manager, provisioner, agent):
    agent.responses = [
        (is_firstboot, result(k3sconf.EX_TEMPFAIL)),
        (is_probe, result(1)),
    ]
    node = deploy(manager, provisioner)

    assert node.state == meta.FAILED
    assert "never completed" in node.error


# -- timeouts and cancellation --------------------------------------------


def test_a_run_that_overruns_is_failed_and_signalled(manager, provisioner, agent):
    class NeverFinishes:
        def __init__(self) -> None:
            self.killed = []
            self.pings = 0

        def ping(self):
            self.pings += 1

        def start(self, path, args, *, input_data=None):
            if path == "/usr/bin/pkill":
                self.killed.append(args)
            return 1

        def poll(self, pid):
            return None  # never exits

    stubborn = NeverFinishes()
    provisioner._agent_factory = lambda uuid, timeout: stubborn
    node = deploy(manager, provisioner)

    assert node.state == meta.FAILED
    assert "did not finish" in node.error
    assert "kill the node" in node.error, "the operator needs to know what to do"
    assert stubborn.killed, "a best-effort pkill should have been attempted"
    assert stubborn.killed[0][0] == "-f"


def test_cancelling_a_job_stops_it_writing_state(manager, provisioner, agent):
    agent.responses = [(is_firstboot, result(0))]
    node, server_url = manager.create(meta.ROLE_SERVER)

    # Kill the node before the work runs, exactly as the kill route does.
    provisioner.cancel(node.uuid)
    manager.delete(node.uuid)

    provisioner.submit(node, server_url)
    assert manager.list_nodes() == []


def test_a_stale_generation_cannot_overwrite_a_replacement(manager, provisioner, agent):
    """A worker that wakes up after its node was killed must write nothing."""
    node, _ = manager.create(meta.ROLE_SERVER)
    manager.delete(node.uuid)

    job = provision.Job(uuid=node.uuid, generation=node.generation)
    provisioner._fail(job, "stale worker reporting in")

    assert manager.list_nodes() == []


# -- verification ----------------------------------------------------------


def test_verify_records_the_unit_state_without_touching_durable_state(manager, provisioner, agent):
    agent.responses = [(is_firstboot, result(0)), (is_probe, result(0))]
    node = deploy(manager, provisioner)
    assert node.state == meta.CONFIGURED

    agent.responses = [(is_probe, result(3))]
    provisioner.reconcile()

    decorated = provisioner.decorate(manager.list_nodes())[0]
    assert decorated.state == meta.CONFIGURED, "a probe result is not durable state"
    assert decorated.service == "inactive"


def test_probe_failures_are_counted_and_then_given_up_on(manager, provisioner, agent, cfg):
    agent.responses = [(is_firstboot, result(0))]
    deploy(manager, provisioner)

    class Unreachable:
        def ping(self):
            raise guestexec.AgentUnavailable("gone")

        def start(self, path, args, *, input_data=None):
            raise guestexec.AgentUnavailable("gone")

        def poll(self, pid):
            return None

    provisioner._agent_factory = lambda uuid, timeout: Unreachable()

    for _ in range(cfg.maintenance.service_probe_attempts):
        provisioner.reconcile()

    decorated = provisioner.decorate(manager.list_nodes())[0]
    assert decorated.service == "unknown"
    assert "gave up" in decorated.progress
    assert decorated.state == meta.CONFIGURED, "an unreachable agent is not a failure"


def test_redaction_and_tail_helpers():
    assert provision.redact("a secret b", "secret") == "a *** b"
    assert provision.redact("unchanged", "") == "unchanged"
    assert provision.tail("abcdef", 10) == "abcdef"
    assert provision.tail("abcdef", 3) == "…def"
