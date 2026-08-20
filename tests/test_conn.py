"""Connection leases, reconnection and the singleton lock.

Two properties matter most.  A connection must never be closed while another
thread is inside a call on it, and holding a lease must not serialise other
callers — a ten second guest-agent call cannot be allowed to stall the poller.
"""

from __future__ import annotations

import threading
import time

import libvirt
import pytest

from k3s_kvm_demo.conn import AlreadyRunning, ConnectionManager, SingletonLock

from .conftest import fake_libvirt_error


class FakeConn:
    def __init__(self, index: int) -> None:
        self.index = index
        self.closed = False

    def close(self) -> None:
        self.closed = True


class Opener:
    def __init__(self) -> None:
        self.opened: list[FakeConn] = []

    def __call__(self, _uri: str) -> FakeConn:
        conn = FakeConn(len(self.opened))
        self.opened.append(conn)
        return conn


@pytest.fixture
def opener() -> Opener:
    return Opener()


@pytest.fixture
def manager(opener: Opener) -> ConnectionManager:
    return ConnectionManager("fake:///x", opener=opener, drain_timeout_s=0.2)


def test_lease_hands_out_the_current_connection(manager, opener):
    with manager.lease() as (conn, epoch):
        assert conn is opener.opened[0]
        assert epoch == 0
    assert len(opener.opened) == 1


def test_leases_do_not_serialise_each_other():
    """A long call under lease must not block anyone else acquiring one."""
    manager = ConnectionManager("fake:///x", opener=Opener())
    holding = threading.Event()
    release = threading.Event()

    def slow_caller():
        with manager.lease():
            holding.set()
            release.wait(5)

    thread = threading.Thread(target=slow_caller, daemon=True)
    thread.start()
    assert holding.wait(2)

    started = time.monotonic()
    with manager.lease() as (_conn, _epoch):
        pass
    assert time.monotonic() - started < 0.5

    release.set()
    thread.join(2)


def test_reconnect_closes_the_old_connection_when_it_is_idle(manager, opener):
    manager.open()
    manager.reconnect(stale_epoch=0)
    assert len(opener.opened) == 2
    assert opener.opened[0].closed is True
    assert manager.epoch == 1


def test_reconnect_abandons_rather_than_closes_a_connection_still_in_use(manager, opener):
    """Closing under an in-flight call is the one thing that must never
    happen; leaking the socket is the cheaper mistake."""
    manager.open()
    holding = threading.Event()
    release = threading.Event()

    def slow_caller():
        with manager.lease():
            holding.set()
            release.wait(5)

    thread = threading.Thread(target=slow_caller, daemon=True)
    thread.start()
    assert holding.wait(2)

    manager.reconnect(stale_epoch=0)  # drain_timeout_s expires with the lease held

    assert opener.opened[0].closed is False, "old connection must not be closed"
    assert len(opener.opened) == 2
    assert manager.epoch == 1

    release.set()
    thread.join(2)


def test_reconnect_is_idempotent_across_racing_callers(manager, opener):
    manager.open()
    manager.reconnect(stale_epoch=0)
    manager.reconnect(stale_epoch=0)  # a second caller with the same stale view
    assert len(opener.opened) == 2


def test_reconnect_from_inside_a_lease_is_a_programming_error(manager):
    with manager.lease() as (_conn, epoch), pytest.raises(RuntimeError, match="holds a lease"):
        manager.reconnect(epoch)


def test_reads_retry_once_on_the_new_connection(manager, opener):
    seen: list[int] = []

    def flaky(conn: FakeConn) -> str:
        seen.append(conn.index)
        if len(seen) == 1:
            raise fake_libvirt_error(libvirt.VIR_ERR_SYSTEM_ERROR)
        return "ok"

    assert manager.read(flaky) == "ok"
    assert seen == [0, 1], "the retry must run on the replacement connection"


def test_reads_do_not_retry_ordinary_errors(manager):
    calls: list[int] = []

    def broken(_conn: FakeConn) -> None:
        calls.append(1)
        raise fake_libvirt_error(libvirt.VIR_ERR_NO_DOMAIN)

    with pytest.raises(libvirt.libvirtError):
        manager.read(broken)
    assert len(calls) == 1


def test_mutations_are_never_retried(manager, opener):
    """Retrying an ambiguous mutation could duplicate it; reconciliation
    re-derives the truth instead."""
    calls: list[int] = []

    def mutate(conn: FakeConn) -> None:
        calls.append(conn.index)
        raise fake_libvirt_error(libvirt.VIR_ERR_SYSTEM_ERROR)

    with pytest.raises(libvirt.libvirtError):
        manager.call(mutate)
    assert calls == [0]
    assert len(opener.opened) == 1, "a failed mutation must not silently reconnect"


def test_close_refuses_while_a_call_is_in_flight(manager, opener):
    manager.open()
    holding = threading.Event()
    release = threading.Event()

    def slow_caller():
        with manager.lease():
            holding.set()
            release.wait(5)

    thread = threading.Thread(target=slow_caller, daemon=True)
    thread.start()
    assert holding.wait(2)

    assert manager.close() is False
    assert opener.opened[0].closed is False

    release.set()
    thread.join(2)


def test_close_closes_an_idle_connection(manager, opener):
    manager.open()
    assert manager.close() is True
    assert opener.opened[0].closed is True
    with pytest.raises(RuntimeError), manager.lease():
        pass


def test_state_survives_a_reconnect_on_the_real_test_driver(cfg):
    """Handles are never cached, so a reconnect re-looks-up by name and the
    domains are still there."""
    manager = ConnectionManager(cfg.libvirt.uri)
    manager.open()
    try:
        before = manager.read(lambda conn: sorted(d.name() for d in conn.listAllDomains(0)))
        manager.reconnect(manager.epoch)
        after = manager.read(lambda conn: sorted(d.name() for d in conn.listAllDomains(0)))
        assert before == after
        assert manager.epoch == 1
    finally:
        manager.close()


# -- singleton lock --------------------------------------------------------


def test_singleton_lock_excludes_a_second_holder(tmp_path):
    path = tmp_path / "demo.lock"
    first = SingletonLock(path)
    first.acquire()
    try:
        second = SingletonLock(path)
        with pytest.raises(AlreadyRunning, match="another k3s-kvm-demo instance"):
            second.acquire()
    finally:
        first.release()

    # Once released, the next process may take it.
    third = SingletonLock(path)
    third.acquire()
    third.release()


def test_singleton_lock_records_the_pid(tmp_path):
    import os

    path = tmp_path / "demo.lock"
    with SingletonLock(path):
        assert path.read_text().strip() == str(os.getpid())


def test_acquiring_twice_in_one_process_is_harmless(tmp_path):
    lock = SingletonLock(tmp_path / "demo.lock")
    lock.acquire()
    lock.acquire()
    lock.release()
    lock.release()
