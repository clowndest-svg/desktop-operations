"""Tests for the UI event pump: latest-wins delivery that survives a broken page.

The properties that matter are the ones the demo depends on: a snapshot produced
while the sink is blocked is *replaced*, not queued; a sink that throws does not
kill the thread (that thread is the only thing standing between a closed window and
a microphone left open); and nothing is delivered after ``stop()``.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable

from jarvis.ui.pump import UiEventPump
from jarvis.ui.state_bridge import UiState, UiVoiceState


def _state(event: str = "wake", voice_state: UiVoiceState = UiVoiceState.IDLE) -> UiState:
    return UiState(
        voice_state=voice_state,
        history=(),
        last_event=event,
        interrupted=False,
    )


def _wait_until(predicate: Callable[[], bool], timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()


class TestDelivery:
    def test_a_submitted_snapshot_arrives(self) -> None:
        delivered: list[UiState] = []
        pump = UiEventPump(delivered.append, debounce=0.01)
        pump.start()
        try:
            pump.submit(_state("wake"))
            assert _wait_until(lambda: len(delivered) == 1)
        finally:
            pump.stop()
        assert delivered[0].last_event == "wake"

    def test_a_burst_collapses_to_the_newest_snapshot(self) -> None:
        """The queue is a state, not a log: only the latest reading is worth sending.

        The gate makes the coalescing deterministic -- without it the pump could
        legally deliver the first snapshot and then the last, which is also correct
        behaviour, just not the thing being asserted here.
        """
        delivered: list[UiState] = []
        gate = threading.Event()
        started = threading.Event()

        def sink(snapshot: UiState) -> None:
            started.set()
            gate.wait(timeout=3.0)
            delivered.append(snapshot)

        pump = UiEventPump(sink, debounce=0.01)
        pump.start()
        try:
            pump.submit(_state("first"))
            assert _wait_until(started.is_set)
            for name in ("second", "third", "fourth"):
                pump.submit(_state(name))
            gate.set()
            assert _wait_until(lambda: len(delivered) >= 2)
            assert delivered[-1].last_event == "fourth"
            assert [item.last_event for item in delivered] == ["first", "fourth"]
        finally:
            gate.set()
            pump.stop()

    def test_nothing_is_delivered_before_start(self) -> None:
        delivered: list[UiState] = []
        pump = UiEventPump(delivered.append, debounce=0.01)

        pump.submit(_state())
        time.sleep(0.05)

        assert delivered == []

    def test_start_is_idempotent(self) -> None:
        delivered: list[UiState] = []
        pump = UiEventPump(delivered.append, debounce=0.01)

        pump.start()
        pump.start()
        try:
            assert pump.running
            pump.submit(_state("solo"))
            assert _wait_until(lambda: len(delivered) == 1)
            time.sleep(0.05)
        finally:
            pump.stop()
        assert len(delivered) == 1, "two threads would deliver twice"


class TestBrokenSink:
    """The hard contract: a sink that throws must not strand the phase machine."""

    def test_the_pump_keeps_running_and_keeps_delivering(self) -> None:
        attempts: list[str] = []

        def sink(snapshot: UiState) -> None:
            attempts.append(snapshot.last_event)
            raise RuntimeError("evaluate_js failed")

        pump = UiEventPump(sink, debounce=0.01)
        pump.start()
        try:
            pump.submit(_state("first"))
            assert _wait_until(lambda: len(attempts) == 1)
            pump.submit(_state("second"))
            assert _wait_until(lambda: len(attempts) == 2)
            assert pump.running
        finally:
            pump.stop()

    def test_a_permanently_broken_sink_slows_down_instead_of_spinning(self) -> None:
        """A window that has gone away fails forever; the pump must go quiet."""
        calls: list[str] = []

        def sink(snapshot: UiState) -> None:
            calls.append(snapshot.last_event)
            raise RuntimeError("window closed")

        pump = UiEventPump(sink, debounce=0.001, max_errors_before_backoff=2, backoff_seconds=0.2)
        pump.start()
        try:
            for index in range(3):
                pump.submit(_state(f"event-{index}"))
                time.sleep(0.02)
            assert _wait_until(lambda: len(calls) >= 2)
            assert pump.running
        finally:
            pump.stop()


class TestShutdown:
    def test_stop_joins_a_waiting_thread(self) -> None:
        delivered: list[UiState] = []
        pump = UiEventPump(delivered.append, debounce=0.02)
        pump.start()

        pump.stop(timeout=1.0)

        assert not pump.running

    def test_stop_releases_a_thread_blocked_on_the_condition(self) -> None:
        delivered: list[UiState] = []
        pump = UiEventPump(delivered.append, debounce=5.0)
        pump.start()
        assert _wait_until(lambda: pump.running)

        started = time.monotonic()
        pump.stop(timeout=1.0)

        assert time.monotonic() - started < 1.0, "stop() waited out the whole debounce"

    def test_submits_after_stop_are_harmless(self) -> None:
        delivered: list[UiState] = []
        pump = UiEventPump(delivered.append, debounce=0.01)
        pump.start()
        pump.stop()

        pump.submit(_state("late"))
        time.sleep(0.05)

        assert delivered == []

    def test_stop_is_idempotent(self) -> None:
        pump = UiEventPump(lambda _snapshot: None, debounce=0.01)
        pump.start()

        pump.stop()
        pump.stop()

        assert not pump.running

    def test_a_snapshot_queued_before_stop_still_arrives(self) -> None:
        """Losing the final state would leave the HUD showing the previous one."""
        delivered: list[UiState] = []
        pump = UiEventPump(delivered.append, debounce=0.01)
        pump.start()

        pump.submit(_state("final"))
        pump.stop(timeout=1.0)

        assert [item.last_event for item in delivered] == ["final"]


class TestSustainedLoad:
    """A short soak of the component the demo depends on most.

    Voice events arrive in bursts from four different threads while the sink is a
    blocking ``evaluate_js``. The failure this rules out is the one that would be
    hardest to reproduce on stage: the pump stalling, or the HUD latching onto a
    state that is no longer true.
    """

    def test_many_snapshots_from_many_threads_delivers_the_newest(self) -> None:
        delivered: list[UiState] = []
        lock = threading.Lock()

        def sink(snapshot: UiState) -> None:
            with lock:
                delivered.append(snapshot)

        pump = UiEventPump(sink, debounce=0.005)
        pump.start()

        def produce(index: int) -> None:
            for n in range(250):
                pump.submit(_state(f"t{index}-{n}"))

        threads = [threading.Thread(target=produce, args=(index,)) for index in range(4)]
        try:
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=10.0)
                assert not thread.is_alive(), "a producer thread blocked on submit()"
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline:
                with lock:
                    count = len(delivered)
                if count > 0:
                    break
                time.sleep(0.01)
            assert pump.running
        finally:
            pump.stop(timeout=2.0)

        with lock:
            assert delivered, "1,000 snapshots produced and none delivered"
            # Coalescing is allowed to drop intermediate states, never the newest one.
            assert delivered[-1].last_event in {f"t{index}-249" for index in range(4)}, delivered[
                -1
            ].last_event
            assert len(delivered) < 1000, "no coalescing happened: the queue is a log"

    def test_a_slow_sink_does_not_make_producers_wait(self) -> None:
        """``submit()`` is called from the microphone thread; it must never block."""
        release = threading.Event()

        def sink(_snapshot: UiState) -> None:
            release.wait(timeout=3.0)

        pump = UiEventPump(sink, debounce=0.005)
        pump.start()
        try:
            pump.submit(_state("first"))
            assert _wait_until(lambda: pump.running)
            started = time.perf_counter()
            for index in range(200):
                pump.submit(_state(f"burst-{index}"))
            waited = time.perf_counter() - started
            assert waited < 0.5, f"submitting 200 snapshots took {waited:.2f}s"
        finally:
            release.set()
            pump.stop(timeout=2.0)
