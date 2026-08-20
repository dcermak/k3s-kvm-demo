"""The worker pool.

The reason this is not ``ThreadPoolExecutor`` is measurable, and the last test
here measures it: the executor's threads are non-daemon and the interpreter
joins them at exit, so ``shutdown(wait=False, cancel_futures=True)`` does not
bound anything.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
import threading
import time

import pytest

from k3s_kvm_demo.workers import NotAccepting, QueueFull, WorkerPool


@pytest.fixture
def pool():
    created = WorkerPool(size=2, queue_size=2)
    created.start()
    yield created
    created.shutdown(2)


def test_tasks_run(pool):
    done = threading.Event()
    pool.submit(done.set)
    assert done.wait(3)


def test_a_failing_task_does_not_kill_its_worker(pool):
    def explode():
        raise RuntimeError("boom")

    pool.submit(explode)
    later = threading.Event()
    pool.submit(later.set)
    assert later.wait(3)


def test_the_queue_is_bounded():
    pool = WorkerPool(size=1, queue_size=2)
    pool.start()
    release = threading.Event()
    started = threading.Event()

    def block():
        started.set()
        release.wait(5)

    try:
        pool.submit(block)
        assert started.wait(2)
        pool.submit(lambda: None)
        pool.submit(lambda: None)
        with pytest.raises(QueueFull, match="too many nodes"):
            pool.submit(lambda: None)
    finally:
        release.set()
        pool.shutdown(2)


def test_submission_is_refused_once_shutting_down():
    pool = WorkerPool(size=1, queue_size=1)
    pool.start()
    pool.shutdown(2)
    with pytest.raises(NotAccepting):
        pool.submit(lambda: None)


def test_shutdown_drains_queued_work_that_never_started():
    pool = WorkerPool(size=1, queue_size=4)
    pool.start()
    release = threading.Event()
    ran = []

    pool.submit(lambda: release.wait(5))
    time.sleep(0.1)
    pool.submit(lambda: ran.append("queued"))

    release.set()
    assert pool.shutdown(2) is True
    assert ran == [], "queued work is dropped, not run, during shutdown"


def test_shutdown_reports_a_straggler_rather_than_waiting_for_it():
    pool = WorkerPool(size=1, queue_size=1)
    pool.start()
    release = threading.Event()
    started = threading.Event()

    def stubborn():
        started.set()
        release.wait(10)

    pool.submit(stubborn)
    assert started.wait(2)

    began = time.monotonic()
    drained = pool.shutdown(0.3)
    elapsed = time.monotonic() - began

    assert drained is False, "the caller must learn a worker is still running"
    assert elapsed < 2, "shutdown must honour its deadline"
    release.set()


PROCESS_EXIT_PROBE = """
import sys, time
sys.path.insert(0, {src!r})
from k3s_kvm_demo.workers import WorkerPool

pool = WorkerPool(size=1, queue_size=1)
pool.start()
pool.submit(lambda: time.sleep(5))
time.sleep(0.2)
pool.shutdown(0.2)
"""


def test_the_process_really_exits_with_work_still_running():
    """The property ThreadPoolExecutor cannot provide.

    Measured: an executor with a five second task still takes five seconds to
    exit despite shutdown(wait=False, cancel_futures=True), because its threads
    are non-daemon and the interpreter joins them.
    """
    from pathlib import Path

    src = str(Path(__file__).resolve().parents[1] / "src")
    began = time.monotonic()
    finished = subprocess.run(
        [sys.executable, "-c", textwrap.dedent(PROCESS_EXIT_PROBE).format(src=src)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    elapsed = time.monotonic() - began
    assert finished.returncode == 0, finished.stderr
    assert elapsed < 3, f"process took {elapsed:.1f}s to exit; daemon threads are not daemon"
