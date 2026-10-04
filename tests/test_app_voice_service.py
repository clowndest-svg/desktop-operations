"""Tests for :class:`jarvis.app.voice_service.VoiceService`.

The real stack loads ~1 GB of models and needs a microphone, so every test here
injects a fake builder. What is under test is the *state machine* and the thread
contract: which phase is observable when, and whether anything can leave the HUD
stuck on "loading" or holding a microphone nobody asked for.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from pathlib import Path

import pytest

from jarvis.app.preferences import VOICE_AUTO_ARM, Preferences
from jarvis.app.voice_service import VoiceService
from jarvis.core.events import VOICE_STATUS_KIND, PipelineEvent, VoicePhase


class FakeLoop:
    """A voice stack that records what was done to it."""

    def __init__(self, *, alive: bool = True, accepts_talk: bool = True) -> None:
        self.starts = 0
        self.stops = 0
        self.pauses = 0
        self.talks = 0
        self.read_aloud: list[str] = []
        self.reading_stops = 0
        self._alive = alive
        self._accepts_talk = accepts_talk
        self._accepts_reading = True

    @property
    def listening(self) -> bool:
        return self._alive and self.starts > self.stops + self.pauses

    def start(self) -> None:
        self.starts += 1

    def stop(self) -> None:
        self.stops += 1

    def stop_listening(self) -> None:
        """Recorded apart from ``stop``: the two are different requests, and that is the bug."""
        self.pauses += 1

    def speak_now(self) -> bool:
        self.talks += 1
        return self._accepts_talk

    def speak_text(self, text: str) -> bool:
        self.read_aloud.append(text)
        return self._accepts_reading

    def stop_speaking(self) -> bool:
        self.reading_stops += 1
        return self._accepts_reading and bool(self.read_aloud)

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
    def test_mute_releases_the_microphone_and_nothing_else(
        self, events: list[PipelineEvent]
    ) -> None:
        """「聆听」 管的是耳朵。把整个栈拆了，等于顺手把她的嗓子也关了。"""
        loop = FakeLoop()
        service = _service(lambda _on_event: loop, events)
        service.enable()
        assert _wait_until(lambda: service.status.phase is VoicePhase.RUNNING)

        status = service.mute()

        assert status.phase is VoicePhase.MUTED
        assert loop.pauses == 1, "该只放掉麦克风"
        assert loop.stops == 0, "stop() 会连播放器一起关掉，朗读就没了"
        assert service.status.phase is VoicePhase.MUTED

    def test_she_can_still_read_aloud_with_the_microphone_shut(
        self, events: list[PipelineEvent]
    ) -> None:
        """用户报的那一条：关了聆听，打字的回答也该念出来。"""
        loop = FakeLoop()
        service = _service(lambda _on_event: loop, events)
        service.enable()
        assert _wait_until(lambda: service.status.phase is VoicePhase.RUNNING)
        service.mute()

        assert service.speak_text("今天多云") is True
        assert loop.read_aloud == ["今天多云"]

    def test_enabling_after_a_mute_reopens_without_a_second_model_load(
        self, events: list[PipelineEvent]
    ) -> None:
        """栈还在手上就别再装一遍：一次聆听开关不该值 30 秒。"""
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
        assert len(loops) == 1, "重新开聆听不该再装一套模型"
        assert loops[0].starts == 2 and loops[0].stops == 0
        assert loops[0].listening is True

    def test_muting_while_a_load_is_in_flight_retires_that_load(
        self, events: list[PipelineEvent]
    ) -> None:
        """刚按下"启用"就按"关掉"：那套装完的模型不许自己把麦克风打开。"""
        gate = threading.Event()
        built: list[SlowLoop] = []

        def builder(_on_event: object) -> SlowLoop:
            loop = SlowLoop(gate)
            built.append(loop)
            return loop

        service = _service(builder, events)
        assert service.enable().phase is VoicePhase.LOADING
        service.mute()
        gate.set()

        assert _wait_until(lambda: service.status.phase is VoicePhase.MUTED)
        time.sleep(0.2)
        assert service.status.phase is VoicePhase.MUTED, "一套没人要的加载接管了状态"
        assert built and built[0].stops == 1

    def test_barge_in_still_needs_the_microphone(self, events: list[PipelineEvent]) -> None:
        """「按一下说」是真的要听人说话，那一个开关关不掉它是错的。"""
        loop = FakeLoop()
        service = _service(lambda _on_event: loop, events)
        service.enable()
        assert _wait_until(lambda: service.status.phase is VoicePhase.RUNNING)
        service.mute()

        status = service.talk()

        assert status.phase is VoicePhase.MUTED
        assert loop.talks == 0

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


class TestTalk:
    """「按一下说」: open a turn without the wake word."""

    def test_refuses_before_the_stack_exists(self, events: list[PipelineEvent]) -> None:
        service = _service(lambda _on_event: FakeLoop(), events)

        status = service.talk()

        assert status.phase is VoicePhase.OFF
        assert "启用语音" in status.detail, "a refused press must say what to do about it"

    def test_refuses_while_models_load(self, events: list[PipelineEvent]) -> None:
        gate = threading.Event()
        service = _service(lambda _on_event: SlowLoop(gate), events)
        service.enable()

        status = service.talk()

        assert status.phase is VoicePhase.LOADING
        assert "加载" in status.detail
        gate.set()

    def test_running_stack_gets_one_turn_and_no_phase_change(
        self, events: list[PipelineEvent]
    ) -> None:
        loop = FakeLoop()
        service = _service(lambda _on_event: loop, events)
        service.enable()
        _wait_until(lambda: service.status.phase is VoicePhase.RUNNING)
        events.clear()

        status = service.talk()

        assert loop.talks == 1
        assert status.phase is VoicePhase.RUNNING
        assert status.detail, "the answer needs something to show next to the button"
        assert not [e for e in events if e.kind == VOICE_STATUS_KIND], (
            "opening a turn is not a change of availability; emitting one would let a "
            "press look like the microphone came back up"
        )

    def test_reports_when_the_pipeline_declines(self, events: list[PipelineEvent]) -> None:
        loop = FakeLoop(accepts_talk=False)
        service = _service(lambda _on_event: loop, events)
        service.enable()
        _wait_until(lambda: service.status.phase is VoicePhase.RUNNING)

        status = service.talk()

        assert loop.talks == 1
        assert status.phase is VoicePhase.RUNNING
        assert "正在" in status.detail, "busy is the one case with no other visible signal"


class TestRemembersConsent:
    """The press that opened the microphone is remembered; nothing else opens it.

    These six cases are the whole privacy argument for auto-arming, so each one
    names the failure it is holding back rather than just the value it returns.
    """

    def test_pressing_enable_records_the_choice(
        self, tmp_path: Path, events: list[PipelineEvent]
    ) -> None:
        store = Preferences(tmp_path / "preferences.json")
        service = _service(lambda _on_event: FakeLoop(), events, preferences=store)
        service.enable()
        assert Preferences(store.path).flag(VOICE_AUTO_ARM) is True

    def test_releasing_the_microphone_forgets_it(
        self, tmp_path: Path, events: list[PipelineEvent]
    ) -> None:
        store = Preferences(tmp_path / "preferences.json")
        service = _service(lambda _on_event: FakeLoop(), events, preferences=store)
        service.enable()
        _wait_until(lambda: service.status.phase is VoicePhase.RUNNING)
        service.mute()
        assert Preferences(store.path).flag(VOICE_AUTO_ARM) is False

    def test_a_refused_press_is_not_recorded_as_consent(
        self, tmp_path: Path, events: list[PipelineEvent]
    ) -> None:
        """A denied button must not quietly arm the next launch."""
        store = Preferences(tmp_path / "preferences.json")
        service = _service(
            lambda _on_event: FakeLoop(),
            events,
            preferences=store,
            permission=lambda: False,
        )
        service.enable()
        assert service.status.phase is VoicePhase.FAILED
        assert Preferences(store.path).flag(VOICE_AUTO_ARM) is False

    def test_closing_the_window_leaves_the_choice_alone(
        self, tmp_path: Path, events: list[PipelineEvent]
    ) -> None:
        """Quitting is not the same person saying they do not want voice."""
        store = Preferences(tmp_path / "preferences.json")
        service = _service(lambda _on_event: FakeLoop(), events, preferences=store)
        service.enable()
        _wait_until(lambda: service.status.phase is VoicePhase.RUNNING)
        service.stop()
        assert Preferences(store.path).flag(VOICE_AUTO_ARM) is True

    def test_remembered_choice_reopens_the_microphone_on_the_next_launch(
        self, tmp_path: Path, events: list[PipelineEvent]
    ) -> None:
        path = tmp_path / "preferences.json"
        first = Preferences(path)
        _service(lambda _on_event: FakeLoop(), events, preferences=first).enable()

        # A new process would build a new service; the file is the only thing shared.
        loops: list[FakeLoop] = []

        def build(_on_event: Callable[[PipelineEvent], None]) -> FakeLoop:
            loop = FakeLoop()
            loops.append(loop)
            return loop

        second = _service(build, events, preferences=Preferences(path))
        assert second.status.phase is VoicePhase.OFF, "start() must still open nothing"
        status = second.arm_if_remembered()
        assert status is not None and status.phase is VoicePhase.LOADING
        assert _wait_until(lambda: second.status.phase is VoicePhase.RUNNING)
        assert len(loops) == 1

    def test_no_choice_recorded_means_no_microphone(
        self, tmp_path: Path, events: list[PipelineEvent]
    ) -> None:
        built = 0

        def build(_on_event: Callable[[PipelineEvent], None]) -> FakeLoop:
            nonlocal built
            built += 1
            return FakeLoop()

        service = _service(
            build,
            events,
            preferences=Preferences(tmp_path / "preferences.json"),
        )
        assert service.arm_if_remembered() is None
        assert built == 0
        assert service.status.phase is VoicePhase.OFF

    def test_a_remembered_choice_stays_silent_when_the_gate_is_closed(
        self, tmp_path: Path, events: list[PipelineEvent], caplog: pytest.LogCaptureFixture
    ) -> None:
        """Refusing to arm is not a failure to report on the status bar."""
        store = Preferences(tmp_path / "preferences.json")
        store.set_flag(VOICE_AUTO_ARM, True)
        built = 0

        def build(_on_event: Callable[[PipelineEvent], None]) -> FakeLoop:
            nonlocal built
            built += 1
            return FakeLoop()

        service = _service(build, events, preferences=store, permission=lambda: False)
        with caplog.at_level(logging.WARNING):
            assert service.arm_if_remembered() is None
        assert built == 0
        assert (
            service.status.phase is VoicePhase.OFF
        ), "a launch nobody asked to be rejected must not light the panel red"
        assert caplog.text, "staying off by itself is too quiet to debug"

    def test_without_a_store_the_feature_is_simply_absent(
        self, events: list[PipelineEvent]
    ) -> None:
        service = _service(lambda _on_event: FakeLoop(), events)
        assert service.arm_if_remembered() is None


class TestSupersededBoot:
    """A load that finishes after the operator moved on must not speak for the panel.

    The bug these pin is the one that makes 「启用语音」 unpressable for the rest of a
    session: the status a boot thread reports used to be unconditional, so a press
    that arrived while an older load was still running could be answered by *that*
    thread -- installing a microphone the window had already muted, or leaving the
    light on 「加载中」 after the thread that would have changed it had nothing left
    to report.
    """

    def test_a_stack_built_after_a_mute_is_released_not_installed(
        self, events: list[PipelineEvent]
    ) -> None:
        gate = threading.Event()
        built: list[FakeLoop] = []

        def build(_on_event: Callable[[PipelineEvent], None]) -> FakeLoop:
            gate.wait(timeout=5.0)
            loop = FakeLoop()
            built.append(loop)
            return loop

        service = _service(build, events)
        service.enable()
        assert service.status.phase is VoicePhase.LOADING
        service.mute()
        gate.set()

        assert _wait_until(
            lambda: bool(built) and built[0].stops > 0
        ), "the late stack kept the microphone: nothing told it to let go"
        # Waited for rather than asserted outright: the teardown that releases the
        # late stack runs on the boot thread, so the phase lands in MUTED whenever
        # that thread gets to it.
        assert _wait_until(
            lambda: service.status.phase is VoicePhase.MUTED
        ), "a mute during loading must stay muted once the stack shows up"
        assert all(event.text != "running" for event in events if event.kind == "voice_status")

    def test_a_load_still_running_is_not_duplicated_by_a_second_press(
        self, events: list[PipelineEvent]
    ) -> None:
        """The other half: 「还在加载」 is a correct answer, and must stay one."""
        gate = threading.Event()
        built = 0

        def build(_on_event: Callable[[PipelineEvent], None]) -> FakeLoop:
            nonlocal built
            gate.wait(timeout=5.0)
            built += 1
            return FakeLoop()

        service = _service(build, events)
        service.enable()
        first = service.enable()
        assert first.phase is VoicePhase.LOADING
        gate.set()
        assert _wait_until(lambda: built == 1)
        assert service.status.phase is VoicePhase.RUNNING


class TestReadAloud:
    """「打字也朗读」 asks the same stack that answers spoken turns."""

    def test_a_sentence_is_handed_to_the_running_stack(self, events: list[PipelineEvent]) -> None:
        loop = FakeLoop()
        service = _service(lambda _on_event: loop, events)
        service.enable()
        assert _wait_until(lambda: service.status.phase is VoicePhase.RUNNING)

        assert service.speak_text("你好，我在。") is True
        assert loop.read_aloud == ["你好，我在。"]

    def test_nothing_is_spoken_before_the_stack_exists(self, events: list[PipelineEvent]) -> None:
        loop = FakeLoop()
        service = _service(lambda _on_event: loop, events)
        assert service.speak_text("你好") is False
        assert loop.read_aloud == []

    def test_blank_text_is_not_a_request(self, events: list[PipelineEvent]) -> None:
        loop = FakeLoop()
        service = _service(lambda _on_event: loop, events)
        service.enable()
        assert _wait_until(lambda: service.status.phase is VoicePhase.RUNNING)
        assert service.speak_text("   ") is False
        assert loop.read_aloud == []

    def test_a_failed_read_aloud_never_reaches_the_caller(
        self, events: list[PipelineEvent]
    ) -> None:
        """The answer is already on screen; a voice that throws must not take it away."""

        class Throwing(FakeLoop):
            def speak_text(self, text: str) -> bool:
                raise RuntimeError("合成断了")

        loop = Throwing()
        service = _service(lambda _on_event: loop, events)
        service.enable()
        assert _wait_until(lambda: service.status.phase is VoicePhase.RUNNING)
        assert service.speak_text("你好") is False

    def test_a_read_aloud_does_not_change_the_phase(self, events: list[PipelineEvent]) -> None:
        """Availability is one axis; "is it making sound" is another, and merging
        them is how the status light starts meaning two things at once."""
        loop = FakeLoop()
        service = _service(lambda _on_event: loop, events)
        service.enable()
        assert _wait_until(lambda: service.status.phase is VoicePhase.RUNNING)
        before = len(events)
        service.speak_text("你好")
        assert service.status.phase is VoicePhase.RUNNING
        assert len(events) == before

    def test_stopping_a_read_aloud_reaches_the_stack(self, events: list[PipelineEvent]) -> None:
        loop = FakeLoop()
        service = _service(lambda _on_event: loop, events)
        service.enable()
        assert _wait_until(lambda: service.status.phase is VoicePhase.RUNNING)
        service.speak_text("你好")
        assert service.stop_speaking() is True
        assert loop.reading_stops == 1

    def test_stopping_when_nothing_is_saying_anything_is_false(
        self, events: list[PipelineEvent]
    ) -> None:
        service = _service(lambda _on_event: FakeLoop(), events)
        assert service.stop_speaking() is False
