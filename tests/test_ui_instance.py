"""The second launch must wake the first copy, not start a second assistant.

With a tray in play, "I closed the window" and "I cannot find the app" look the same
to an operator, and the natural response is to click the shortcut again. These tests
are the promise that clicking again is harmless: one process owns the lock, the
other one is an alarm clock.

Everything runs on loopback with ephemeral ports in a tmp directory; no patching.
"""

from __future__ import annotations

import json
import os
import socket
import threading
import time
from collections.abc import Callable
from pathlib import Path

import pytest

from jarvis.ui.instance import ACTIVATE, InstanceGate, read_lock, send_activate


def wait_for(predicate: Callable[[], bool], timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def activated_gate(lock: Path) -> tuple[InstanceGate, list[int]]:
    """A claimed, started gate plus the list that counts how often it was woken."""
    hits: list[int] = []
    gate = InstanceGate(lock, on_activate=lambda: hits.append(os.getpid()))
    assert gate.claim() is True
    gate.start()
    return gate, hits


class TestClaim:
    def test_first_launch_claims_and_writes_the_lock(self, tmp_path: Path) -> None:
        lock = tmp_path / "desktop.lock"
        gate = InstanceGate(lock)
        assert gate.claim() is True
        assert read_lock(lock) == (os.getpid(), gate.port)
        gate.release()

    def test_the_lock_file_is_machine_readable_json(self, tmp_path: Path) -> None:
        lock = tmp_path / "desktop.lock"
        gate = InstanceGate(lock)
        gate.claim()
        raw = json.loads(lock.read_text(encoding="utf-8"))
        assert set(raw) == {"pid", "port"}
        assert raw["port"] == gate.port
        gate.release()

    def test_second_launch_becomes_the_guest_and_wakes_the_owner(self, tmp_path: Path) -> None:
        lock = tmp_path / "desktop.lock"
        gate, hits = activated_gate(lock)
        guest = InstanceGate(lock)
        assert guest.claim() is False
        assert wait_for(lambda: len(hits) == 1)
        gate.release()

    def test_a_guest_that_never_started_a_window_leaves_the_lock_alone(
        self, tmp_path: Path
    ) -> None:
        lock = tmp_path / "desktop.lock"
        gate, _ = activated_gate(lock)
        InstanceGate(lock).claim()
        assert read_lock(lock) == (os.getpid(), gate.port)
        gate.release()

    @pytest.mark.parametrize("content", ["", "{not json", "[]", '{"pid": "x", "port": 1}'])
    def test_an_unreadable_lock_is_absent_not_fatal(self, tmp_path: Path, content: str) -> None:
        lock = tmp_path / "desktop.lock"
        lock.write_text(content, encoding="utf-8")
        gate = InstanceGate(lock)
        assert gate.claim() is True
        gate.release()

    def test_a_dead_owner_is_replaced_not_mourned(self, tmp_path: Path) -> None:
        """A crash or a reboot leaves a port nobody answers; that is not a reason to
        refuse to start forever."""
        lock = tmp_path / "desktop.lock"
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            dead_port = int(probe.getsockname()[1])
        lock.write_text(json.dumps({"pid": 999_999, "port": dead_port}), encoding="utf-8")
        gate = InstanceGate(lock)
        assert gate.claim() is True
        assert read_lock(lock) == (os.getpid(), gate.port)
        gate.release()

    def test_a_directory_that_cannot_be_written_still_lets_the_app_run(
        self, tmp_path: Path
    ) -> None:
        """The lock is a courtesy, not a licence: losing it must not cost the window."""
        lock = tmp_path / "desktop.lock"
        lock.mkdir()
        gate = InstanceGate(lock)
        assert gate.claim() is True
        gate.release()


class TestAnswering:
    def test_only_the_activation_verb_shows_a_window(self, tmp_path: Path) -> None:
        lock = tmp_path / "desktop.lock"
        gate, hits = activated_gate(lock)
        try:
            with socket.create_connection(("127.0.0.1", gate.port), timeout=2) as sock:
                sock.sendall(b"SNAPSHOT\n")
                assert sock.recv(16) == b""
        finally:
            gate.release()
        assert hits == []

    def test_an_empty_connection_is_ignored(self, tmp_path: Path) -> None:
        lock = tmp_path / "desktop.lock"
        gate, hits = activated_gate(lock)
        try:
            with socket.create_connection(("127.0.0.1", gate.port), timeout=2):
                pass
        finally:
            gate.release()
        assert hits == []

    def test_a_handler_that_raises_does_not_kill_the_listener(self, tmp_path: Path) -> None:
        lock = tmp_path / "desktop.lock"

        def explode() -> None:
            raise RuntimeError("窗口不肯出来")

        gate = InstanceGate(lock, on_activate=explode)
        gate.claim()
        gate.start()
        try:
            assert send_activate(gate.port) is True
            # Still alive afterwards: the next double-click must still get through.
            assert send_activate(gate.port) is True
        finally:
            gate.release()

    def test_activation_is_delivered_on_the_listener_thread(self, tmp_path: Path) -> None:
        lock = tmp_path / "desktop.lock"
        threads: list[str] = []
        gate = InstanceGate(
            lock, on_activate=lambda: threads.append(threading.current_thread().name)
        )
        gate.claim()
        gate.start()
        try:
            send_activate(gate.port)
            assert wait_for(lambda: bool(threads))
        finally:
            gate.release()
        assert threads == ["jarvis-instance"]

    def test_a_gate_that_did_not_claim_has_nothing_to_start(self, tmp_path: Path) -> None:
        gate = InstanceGate(tmp_path / "desktop.lock")
        gate.start()
        assert gate.owns_lock is False
        gate.release()


class TestRelease:
    def test_release_removes_our_own_lock(self, tmp_path: Path) -> None:
        lock = tmp_path / "desktop.lock"
        gate = InstanceGate(lock)
        gate.claim()
        gate.release()
        assert not lock.exists()

    def test_release_leaves_somebody_elses_lock_alone(self, tmp_path: Path) -> None:
        """Two instances converging on the same path must not delete the winner's file."""
        lock = tmp_path / "desktop.lock"
        gate = InstanceGate(lock)
        gate.claim()
        lock.write_text(json.dumps({"pid": 4242, "port": 4242}), encoding="utf-8")
        gate.release()
        assert read_lock(lock) == (4242, 4242)
        lock.unlink()

    def test_release_is_idempotent(self, tmp_path: Path) -> None:
        gate = InstanceGate(tmp_path / "desktop.lock")
        gate.claim()
        gate.release()
        gate.release()

    def test_send_activate_to_a_closed_port_is_false(self) -> None:
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = int(probe.getsockname()[1])
        assert send_activate(port) is False
        assert send_activate(0) is False


class TestProtocol:
    def test_the_verb_is_a_single_line(self) -> None:
        """The incumbent compares the whole line; a prefix would let anything through."""
        assert "\n" not in ACTIVATE and ACTIVATE.strip() == ACTIVATE

    def test_activating_an_owner_with_no_handler_still_answers(self, tmp_path: Path) -> None:
        lock = tmp_path / "desktop.lock"
        gate = InstanceGate(lock)
        gate.claim()
        gate.start()
        try:
            assert send_activate(gate.port) is True
        finally:
            gate.release()
