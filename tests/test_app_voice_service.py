"""Tests for :class:`jarvis.app.voice_service.VoiceService`.

The real stack loads ~1 GB of models and needs a microphone, so every test here
injects a fake builder. What is under test is the *state machine* and the thread
contract: which phase is observable when, and whether anything can leave the HUD
stuck on "loading" or holding a microphone nobody asked for.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable

import pytest

from jarvis.app.voice_service import VoiceService
from jarvis.core.events import VOICE_STATUS_KIND, PipelineEvent, VoicePhase


class FakeLoop:
    """A voice stack that records what was done to it."""

    def __init__(self, *, alive: bool = True) -> None:
        self.starts = 0
        self.stops = 0
        self._alive = alive

    @property
    def listening(self) -> bool:
        return self._alive and self.starts > self.stops

    def start(self) -> None:
        self.starts += 1

    def stop(self) -> None:
        self.stops += 1

    def set_alive(self, alive: bool) -> None:
        """Simulate the capture thread going away, or coming back."""
        self._alive = alive


class SlowLoop(FakeLoop):
    """A stack whose start blocks until somebody releases it."""

    def __init__(self, gate: threading.Event) -> None:
        super().__init__()
        self._gate = gate

    def start(self) -> None:
        self._gate.wait(timeout=5.0)
        super().start()


def _wait_until(predicate: Callable[[], bool], timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()


@pytest.fixture()
def events() -> list[PipelineEvent]:
    return []


def _service(
    builder: Callable[[Callable[[PipelineEvent], None]], FakeLoop],
    events: list[PipelineEvent],
    **kwargs: object,
) -> VoiceService:
    kwargs.setdefault("keywords", lambda: ("你好小夜",))
    service = VoiceService(builder, **kwargs)  # type: ignore[arg-type]
    service.subscribe(events.append)
    service.start()
    return service


class TestStartIsCheap:
    def test_start_does_not_touch_the_builder(self, events: list[PipelineEvent]) -> None:
        calls = 0

        def builder(_on_event: object) -> FakeLoop:
            nonlocal calls
            calls += 1
            return FakeLoop()

        service = _service(builder, events)

        assert calls == 0
        assert service.status.phase is VoicePhase.OFF

    def test_start_reports_off_rather_than_an_empty_state(
        self, events: list[PipelineEvent]
    ) -> None:
        service = _service(lambda _on_event: FakeLoop(), events)

        assert service.status.keyword == "你好小夜"


class TestEnable:
    def test_refuses_when_configuration_disables_voice(self, events: list[PipelineEvent]) -> None:
        built = 0

        def builder(_on_event: object) -> FakeLoop:
            nonlocal built
            built += 1
            return FakeLoop()

        service = _service(builder, events, permission=lambda: False)

        status = service.enable()

        assert status.phase is VoicePhase.FAILED
        assert "orchestration.enabled" in status.detail
        assert built == 0, "a refused enable must not load a single model"

    def test_loads_then_reports_running(self, events: list[PipelineEvent]) -> None:
        loop = FakeLoop()
        service = _service(lambda _on_event: loop, events)

        loading = service.enable()

        assert loading.phase is VoicePhase.LOADING
        assert _wait_until(lambda: service.status.phase is VoicePhase.RUNNING)
        assert loop.starts == 1
        assert service.status.keyword == "你好小夜"

    def test_the_phases_arrive_as_voice_status_events(self, events: list[PipelineEvent]) -> None:
        service = _service(lambda _on_event: FakeLoop(), events)

        service.enable()
        assert _wait_until(lambda: service.status.phase is VoicePhase.RUNNING)

        phases = [event.text for event in events if event.kind == VOICE_STATUS_KIND]
        assert phases == ["loading", "running"]

    def test_a_failed_build_is_reported_not_swallowed(
        self, events: list[PipelineEvent], caplog: pytest.LogCaptureFixture
    ) -> None:
        def builder(_on_event: object) -> FakeLoop:
            raise RuntimeError("没有可用的麦克风")

        service = _service(builder, events)

        service.enable()

        assert _wait_until(lambda: service.status.phase is VoicePhase.FAILED)
        assert "RuntimeError" in service.status.detail
        assert "没有可用的麦克风" in service.status.detail
        assert "voice stack failed to load" in caplog.text

    def test_a_failed_start_releases_the_stack(self, events: list[PipelineEvent]) -> None:
        class Exploding(FakeLoop):
            def start(self) -> None:
                raise OSError("设备忙")

        loop = Exploding()
        service = _service(lambda _on_event: loop, events)

        service.enable()

        assert _wait_until(lambda: service.status.phase is VoicePhase.FAILED)
        assert loop.stops == 1, "a stack that failed to start must still be closed"

    def test_enable_is_idempotent_while_loading(self, events: list[PipelineEvent]) -> None:
        gate = threading.Event()
        builds = 0

        def builder(_on_event: object) -> FakeLoop:
            nonlocal builds
            builds += 1
            return SlowLoop(gate)

        service = _service(builder, events)
        first = service.enable()
        second = service.enable()

        assert first.phase is VoicePhase.LOADING
        assert second.phase is VoicePhase.LOADING
        assert builds == 1, "a second click must not queue a second model load"
        gate.set()


class TestMute:
    def test_mute_releases_the_microphone(self, events: list[PipelineEvent]) -> None:
        loop = FakeLoop()
        service = _service(lambda _on_event: loop, events)
        service.enable()
        assert _wait_until(lambda: service.status.phase is VoicePhase.RUNNING)

        status = service.mute()

        assert status.phase is VoicePhase.MUTED
        assert loop.stops == 1
        assert service.status.phase is VoicePhase.MUTED

    def test_enabling_after_a_mute_builds_again(self, events: list[PipelineEvent]) -> None:
        loops: list[FakeLoop] = []

        def builder(_on_event: object) -> FakeLoop:
            loop = FakeLoop()
            loops.append(loop)
            return loop

        service = _service(builder, events)
        service.enable()
        assert _wait_until(lambda: service.status.phase is VoicePhase.RUNNING)
        service.mute()

        service.enable()

        assert _wait_until(lambda: service.status.phase is VoicePhase.RUNNING)
        assert len(loops) == 2
        assert loops[0].stops == 1

    def test_muting_without_a_stack_is_harmless(self, events: list[PipelineEvent]) -> None:
        service = _service(lambda _on_event: FakeLoop(), events)

        assert service.mute().phase is VoicePhase.MUTED


class TestStatusReconciliation:
    def test_a_dead_capture_thread_is_reported_as_failure(
        self, events: list[PipelineEvent]
    ) -> None:
        """The indicator must not keep claiming to listen after the loop exits."""
        loop = FakeLoop()
        service = _service(lambda _on_event: loop, events)
        service.enable()
        assert _wait_until(lambda: service.status.phase is VoicePhase.RUNNING)

        loop.set_alive(False)

        status = service.status
        assert status.phase is VoicePhase.FAILED
        assert "拾音线程已退出" in status.detail

    def test_the_observed_failure_does_not_rewrite_internal_state(
        self, events: list[PipelineEvent]
    ) -> None:
        loop = FakeLoop()
        service = _service(lambda _on_event: loop, events)
        service.enable()
        assert _wait_until(lambda: service.status.phase is VoicePhase.RUNNING)
        loop.set_alive(False)
        degraded = service.status
        assert degraded.phase is VoicePhase.FAILED

        loop.set_alive(True)
        recovered = service.status
        assert recovered.phase is VoicePhase.RUNNING


class TestStop:
    def test_stop_releases_the_microphone(self, events: list[PipelineEvent]) -> None:
        loop = FakeLoop()
        service = _service(lambda _on_event: loop, events)
        service.enable()
        assert _wait_until(lambda: service.status.phase is VoicePhase.RUNNING)

        service.stop()

        assert loop.stops == 1
        assert service.status.phase is VoicePhase.OFF

    def test_stop_is_idempotent(self, events: list[PipelineEvent]) -> None:
        loop = FakeLoop()
        service = _service(lambda _on_event: loop, events)
        service.enable()
        assert _wait_until(lambda: service.status.phase is VoicePhase.RUNNING)

        service.stop()
        service.stop()

        assert loop.stops == 1

    def test_a_stop_during_a_slow_build_does_not_hand_back_the_microphone(
        self, events: list[PipelineEvent]
    ) -> None:
        """The race the window close button actually hits.

        ``stop()`` cannot interrupt a model load. If the boot thread then started
        the loop anyway, a window that has already closed would leave a live
        microphone behind -- so the stack is torn down as soon as it appears.
        """
        gate = threading.Event()
        loop_box: list[SlowLoop] = []

        def builder(_on_event: object) -> SlowLoop:
            loop = SlowLoop(gate)
            loop_box.append(loop)
            return loop

        service = _service(builder, events, join_timeout=0.05)
        service.enable()
        assert _wait_until(lambda: bool(loop_box))

        service.stop()  # boot thread is still inside loop.start()
        gate.set()

        assert _wait_until(lambda: loop_box[0].stops >= 1)
        assert service.status.phase is VoicePhase.OFF


class TestListenerFailures:
    """A sink that throws must never strand the phase machine or the microphone."""

    def test_a_throw_on_the_way_to_running_still_reaches_running(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:

        def sink(_event: PipelineEvent) -> None:
            raise RuntimeError("HUD 断了")

        service = VoiceService(lambda _on_event: FakeLoop(), keywords=lambda: ("你好小夜",))
        service.subscribe(sink)
        service.start()

        service.enable()

        assert _wait_until(lambda: service.status.phase is VoicePhase.RUNNING)
        assert "voice event listener raised" in caplog.text

    def test_a_pipeline_event_that_throws_does_not_escape_the_capture_loop(
        self, events: list[PipelineEvent]
    ) -> None:
        service = _service(lambda _on_event: FakeLoop(), events)
        service.enable()
        assert _wait_until(lambda: service.status.phase is VoicePhase.RUNNING)

        service.subscribe(lambda _event: 1 / 0)  # type: ignore[arg-type]

        service.forward_event(PipelineEvent(kind="wake", text="你好小夜"))  # must not raise


class TestKeywordDisplay:
    def test_only_the_first_three_keywords_are_shown(self, events: list[PipelineEvent]) -> None:
        service = VoiceService(
            lambda _on_event: FakeLoop(),
            keywords=lambda: ("你好小夜", "你好小叶", "你好晓叶", "你好小业"),
        )
        service.start()

        assert service.status.keyword == "你好小夜 / 你好小叶 / 你好晓叶"

    def test_no_keywords_configured_shows_an_empty_string(
        self, events: list[PipelineEvent]
    ) -> None:
        service: VoiceService = VoiceService(lambda _on_event: FakeLoop())
        service.start()

        assert service.status.keyword == ""


def test_the_service_is_a_registration_ready_component() -> None:
    """``Application.register`` needs name/start/stop; a rename would break boot."""
    from jarvis.app.application import Application

    service = VoiceService(lambda _on_event: FakeLoop())
    app = Application()
    app.register(service)
    app.start()
    app.stop()

    assert service.status.phase is VoicePhase.OFF


class TestEventForwarding:
    def test_pipeline_events_reach_the_subscriber_verbatim(self) -> None:
        events: list[PipelineEvent] = []
        service = VoiceService(lambda _on_event: FakeLoop())
        service.subscribe(events.append)
        service.start()
        sink: Callable[[PipelineEvent], None] = service.forward_event

        sink(PipelineEvent(kind="wake", text="你好小夜", detail=0.9))

        assert events == [PipelineEvent(kind="wake", text="你好小夜", detail=0.9)]


def test_type_checking_helper_keeps_self_importable() -> None:
    """Guard against a stale ``Self`` import left over from refactoring."""
    import ast
    import pathlib

    source = pathlib.Path("jarvis/app/voice_service.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
    }
    assert "Self" not in imported
