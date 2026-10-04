"""The wake greeting: the words, the gate in front of them, and what happens when the
gate never opens.

Three failure modes are worth more here than the happy path.

*The greeting is spoken while the figure is still half-drawn* -- which is the whole
reason the operator asked for the gate, and why the arrival must be reported by the page
rather than timed by the shell.

*The greeting silently never happens* -- a page that does not report (stale bundle, an
exception in the animation) must cost one delayed sentence plus a log line, not the
feature. That is the difference between a bug you can see and a bug you ship.

*She answers herself* -- the first version blocked the capture thread until the sentence
had played, which is exactly what it sounds like: the microphone never got drained, the
frames piled up in the source's own buffer, and the first thing it heard on resuming was
the tail of her own greeting. So the thread keeps reading and discards;
:func:`_greeting_finished` and the frame fed inside the fake ``_read_aloud`` are what pin
that, and neither is expressible in a design where the greeting is synchronous.

Also pinned: a wake word that came with a command already transcribed gets no greeting,
because "你好小夜 现在几点" asked a question and a sentence about being greeted is a
delay in front of the answer they came for.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any, cast

from jarvis.app.wake_greeting import DEFAULT_GREETING, WakeGreeter
from jarvis.orchestration.player import NullAudioPlayer
from jarvis.orchestration.types import PipelineEvent
from jarvis.orchestration.voice_pipeline import PipelineState, VoicePipeline
from jarvis.tts.types import AudioChunk
from jarvis.vad.segmenter import VoiceActivitySegmenter
from jarvis.wakeword.detector import WakeWordDetector
from jarvis.wakeword.types import WakeHit
from tests._fakes import (
    FakeAsr,
    FakeGraph,
    FakeTts,
    ScriptedAudioSource,
    ScriptedVadEngine,
    ScriptedWakeWordEngine,
)

_FRAME = b"\x00" * 1024


class _Clock:
    """A monotonic clock the test advances, so a bounded wait costs no real time."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class _FakeSleep:
    """Sleeping is the clock moving: the wait loop's exit conditions stay reachable."""

    def __init__(self, clock: _Clock, step: float = 0.05) -> None:
        self._clock = clock
        self._step = step

    def __call__(self, seconds: float) -> None:
        self._clock.advance(min(seconds, self._step))


def _greeter(text: str = DEFAULT_GREETING, **kwargs: Any) -> WakeGreeter:
    clock = _Clock()
    return WakeGreeter(lambda: text, clock=clock, sleep=_FakeSleep(clock), **kwargs)


class TestGateWithNothingPending:
    def test_no_arrival_in_flight_means_the_gate_is_already_open(self) -> None:
        """The normal case on a machine with the HUD on screen, or no pet at all: the
        shell never summons a figure, so nothing arms the wait and a greeting that
        blocked on a report that will never come would be a bug, not a design."""
        greeter = _greeter()
        assert greeter.wait_for_figure() is True

    def test_a_report_before_the_expectation_does_not_create_a_wait(self) -> None:
        greeter = _greeter()
        greeter.figure_arrived()
        greeter.expect_figure()
        # Still pending -- the report was about the *previous* arrival.
        assert greeter.wait_for_figure() is False
        assert greeter.skipped_waits == 1

    def test_an_empty_greeting_is_a_switch_not_a_missing_value(self) -> None:
        assert _greeter("").text() == ""

    def test_a_provider_that_throws_costs_only_the_greeting(self) -> None:
        def boom() -> str:
            raise RuntimeError("preferences unreadable")

        assert WakeGreeter(boom).text() == ""


class TestGateWaitsForTheFigure:
    def test_a_report_that_arrives_mid_wait_releases_it(self) -> None:
        """Real time and a real thread, because the thing under test is the handoff
        between the page's report and the voice thread that is blocked on it."""
        greeter = WakeGreeter(lambda: DEFAULT_GREETING, timeout_seconds=3.0)
        greeter.expect_figure()
        threading.Timer(0.05, greeter.figure_arrived).start()
        assert greeter.wait_for_figure() is True
        assert greeter.skipped_waits == 0

    def test_a_page_that_never_reports_costs_a_delay_and_a_counter(self) -> None:
        """The deadline is the whole point: without it the first stale UI bundle turns
        every future wake word into silence, and "the greeting is gone" would be
        reported as a voice bug rather than as the missing report it is."""
        greeter = _greeter(timeout_seconds=1.7)
        greeter.expect_figure()
        assert greeter.wait_for_figure() is False
        assert greeter.skipped_waits == 1
        # And it does not stay stuck: the next wake is not held hostage by it.
        assert greeter.wait_for_figure() is True

    def test_stopping_the_app_abandons_the_wait(self) -> None:
        greeter = _greeter(timeout_seconds=30.0)
        greeter.expect_figure()
        assert greeter.wait_for_figure(lambda: True) is False

    def test_an_abandoned_wait_costs_the_next_greeting_no_time(self) -> None:
        """A wait armed while the greeting was switched off is never awaited: the figure
        arrived, nobody was holding a sentence for it. The next wake must find it already
        expired rather than sit out a timeout that passed five seconds ago."""
        clock = _Clock()
        greeter = WakeGreeter(
            lambda: DEFAULT_GREETING,
            timeout_seconds=0.2,
            clock=clock,
            sleep=_FakeSleep(clock),
        )
        greeter.expect_figure()
        clock.advance(5.0)
        before = clock.now

        assert greeter.wait_for_figure() is False
        assert clock.now == before, "an already-expired wait was held for another timeout"
        assert greeter.skipped_waits == 1


def _pipeline(
    *,
    wake_hits: list[WakeHit] | None = None,
    greeting: str | None = DEFAULT_GREETING,
    provider: Any = None,
    gate: Any = None,
    events: list[PipelineEvent] | None = None,
    on_event: Any = None,
) -> tuple[VoicePipeline, FakeTts, FakeGraph]:
    detector = WakeWordDetector(
        ScriptedWakeWordEngine(wake_hits if wake_hits is not None else [WakeHit("你好小夜", 1.0)]),
        threshold=0.0,
        cooldown_seconds=0.0,
    )
    segmenter = VoiceActivitySegmenter(
        ScriptedVadEngine([0.0]),
        sample_rate=16_000,
        threshold=0.5,
        min_speech_ms=10,
        max_silence_ms=10,
        speech_pad_ms=0,
        max_speech_ms=0,
    )
    tts = FakeTts([AudioChunk(audio=b"x", sample_rate=16_000, is_final=True)])
    sink = on_event if on_event is not None else (events.append if events is not None else None)
    pipeline = VoicePipeline(
        detector=detector,
        segmenter=segmenter,
        asr=FakeAsr("几点了"),
        graph=FakeGraph("十点"),
        tts=tts,
        source_factory=lambda: ScriptedAudioSource([_FRAME, _FRAME, _FRAME, _FRAME]),
        player=NullAudioPlayer(),
        barge_in=True,
        on_event=sink,
        greeting_provider=(
            provider
            if provider is not None
            else ((lambda: greeting) if greeting is not None else None)
        ),
        greeting_gate=gate,
    )
    return pipeline, tts, FakeGraph("十点")


def _greeting_finished(pipeline: VoicePipeline, timeout: float = 2.0) -> bool:
    """Wait for the greeting thread to hand the microphone back.

    It runs off the capture thread on purpose -- that is what keeps the microphone
    draining while she speaks -- so a test that asserts on what she said has to wait for
    it rather than read a race. The flag is raised before the thread starts, so this can
    only miss a greeting that never began.
    """
    deadline = time.monotonic() + timeout
    while pipeline._greeting.is_set():
        if time.monotonic() > deadline:
            return False
        time.sleep(0.005)
    return True


class TestWakeSpeaksTheGreeting:
    def test_a_bare_wake_word_says_the_sentence(self) -> None:
        pipeline, tts, _ = _pipeline()
        pipeline._on_wake(_wake())
        assert _greeting_finished(pipeline)
        assert tts.calls == [DEFAULT_GREETING]

    def test_the_gate_is_asked_before_a_single_word_is_spoken(self) -> None:
        seen: list[str] = []
        pipeline, tts, _ = _pipeline(gate=_recording_gate(seen))
        pipeline._on_wake(_wake())
        assert _greeting_finished(pipeline)
        assert seen == ["gate"] and tts.calls == [DEFAULT_GREETING]

    def test_no_greeting_configured_means_the_gate_is_never_consulted(self) -> None:
        """The gate blocks up to its timeout; asking it when there is nothing to say
        would stall a wake word for no reason at all."""
        calls = 0

        def gate(_stop: Any) -> bool:
            nonlocal calls
            calls += 1
            return True

        pipeline, tts, _ = _pipeline(greeting="", gate=gate)
        assert pipeline._start_greeting() is False
        assert tts.calls == [] and calls == 0

    def test_a_greeting_of_none_is_the_same_as_off(self) -> None:
        pipeline, _, _ = _pipeline(greeting=None)
        assert pipeline._start_greeting() is False

    def test_a_wake_word_that_came_with_a_command_is_answered_not_greeted(self) -> None:
        events: list[PipelineEvent] = []
        pipeline, tts, _ = _pipeline(
            wake_hits=[WakeHit("你好小夜", 1.0, command="现在几点")],
            events=events,
        )
        pipeline._on_wake(_wake(command="现在几点"))
        # The answer runs on its own thread and may speak 「十点」; what must never appear
        # is the greeting, which would delay the reply the operator already asked for.
        # The flag is raised synchronously by a greeting that begins, so "not set" here is
        # the statement that no greeting thread was started at all.
        assert pipeline._greeting.is_set() is False
        assert DEFAULT_GREETING not in tts.calls
        assert [event.kind for event in events][:1] == ["wake"]

    def test_the_microphone_is_open_but_deaf_while_she_greets(self) -> None:
        """The guarantee is not "the words come first" -- it is that nothing she says
        comes back as the operator's command.

        A version that blocked the capture thread while greeting would pass every other
        test in this file and still answer herself: the frames it never read sit in the
        source's buffer and arrive the moment listening resumes. So the thread keeps
        reading, and this feeds it a frame mid-sentence.
        """
        pipeline, tts, _ = _pipeline()
        spoken: list[str] = []
        during: list[int] = []
        original = pipeline._read_aloud

        def speak(text: str) -> None:
            # A chunk arrives while she is still talking: read, drop, do not buffer.
            pipeline._dispatch(_FRAME)
            during.append(len(pipeline._buffer))
            original(text)
            spoken.append(text)

        pipeline._read_aloud = speak  # type: ignore[method-assign]
        pipeline._on_wake(_wake())
        # The state advances the moment the wake word returns: what is held is the
        # reading, not the listening.
        assert pipeline._get_state() is PipelineState.LISTENING
        assert _greeting_finished(pipeline)

        assert spoken == [DEFAULT_GREETING] and tts.calls == [DEFAULT_GREETING]
        assert during == [0], "her own greeting was fed to the voice activity detector"
        # And released afterwards: the next frame is a real one, so it is buffered.
        pipeline._dispatch(_FRAME)
        assert len(pipeline._buffer) == len(_FRAME)


def _wake(command: str = "") -> Any:
    from jarvis.wakeword.types import WakeEvent

    return WakeEvent(keyword="你好小夜", score=1.0, timestamp=0.0, command=command)


def _recording_gate(seen: list[str]) -> Any:
    """A gate that notes it was asked, then opens immediately."""

    def gate(_stop: Any) -> bool:
        seen.append("gate")
        return True

    return gate


class _Window:
    """The slice of a pywebview window the lifecycle touches on the way to hidden."""

    def __init__(self) -> None:
        self.hidden = 0

    def hide(self) -> None:
        self.hidden += 1

    def show(self) -> None:
        return None

    def restore(self) -> None:
        return None

    def destroy(self) -> None:
        return None

    def evaluate_js(self, script: str) -> Any:
        del script
        return None

    def close(self) -> None:
        return None


class _Pet:
    shown = True
    audio_window = None

    def __init__(self) -> None:
        self.summoned = 0

    def set_thinking(self, active: bool, *, source: str = "turn") -> None:
        del active, source  # 这条测试管的是问候语，不是她头上那张卡

    def toggle(self) -> bool:
        return True

    def summon(self) -> bool:
        self.summoned += 1
        return True

    def close(self) -> None:
        return None


class TestShellArmsTheGate:
    def test_summoning_the_figure_arms_the_wait(self) -> None:
        from jarvis.ui.lifecycle import ShellLifecycle

        greeter = _greeter()
        pet = _Pet()
        shell = ShellLifecycle(window=_Window(), pet=pet, hide_to_tray=True)
        shell.attach_greeter(greeter)
        assert shell.hide() is True

        assert shell.summon_pet() is True
        assert pet.summoned == 1
        assert greeter.wait_for_figure() is False, "an armed arrival must be awaited"

    def test_a_wake_word_while_the_hud_is_on_screen_arms_nothing(self) -> None:
        """The operator's own rule: no figure appears over the window they are reading.

        With nothing summoned there is nothing to report, so a greeting that waited for
        one here would be silent on every wake for anyone who never hides the window.
        """
        from jarvis.ui.lifecycle import ShellLifecycle

        greeter = _greeter()
        pet = _Pet()
        shell = ShellLifecycle(window=_Window(), pet=pet, hide_to_tray=True)
        shell.attach_greeter(greeter)
        assert shell.summon_pet() is False
        assert pet.summoned == 0
        assert greeter.wait_for_figure() is True

    def test_no_greeter_wired_still_summons_the_pet(self) -> None:
        """The gate is optional plumbing; forgetting it must not cost the figure."""
        from jarvis.ui.lifecycle import ShellLifecycle

        pet = _Pet()
        shell = ShellLifecycle(window=_Window(), pet=pet, hide_to_tray=True)
        assert shell.hide() is True
        assert shell.summon_pet() is True
        assert pet.summoned == 1


class _NoLlm:
    def set_section_override(self, value: Any) -> None:
        del value


class _NoSystem:
    def report(self) -> Any:
        from jarvis.app.system_service import SystemReport

        return SystemReport(metrics={}, error="smoke")


def _settings_service(tmp_path: Any) -> Any:
    from jarvis.app.preferences import Preferences
    from jarvis.app.settings_service import SettingsService
    from jarvis.config.schema import LlmSection

    service = SettingsService(
        Preferences(tmp_path / "p.json"),
        cast(Any, _NoLlm()),
        lambda: LlmSection(
            default_provider="a",
            timeout_seconds=30.0,
            max_retries=1,
            retry_backoff_seconds=0.5,
            providers={},
        ),
        environ={},
        persist_env=lambda _n, _v: True,
    )
    service.start()
    return service


class TestSettingsAndBridge:
    def test_the_sentence_round_trips_and_empty_means_quiet(self, tmp_path: Any) -> None:
        service = _settings_service(tmp_path)
        assert service.wake_greeting() == DEFAULT_GREETING
        assert service.apply({"wake_greeting": "  在呢  "})["problems"] == {}
        assert service.wake_greeting() == "在呢"
        service.apply({"wake_greeting": ""})
        assert service.wake_greeting() == ""

    def test_a_long_sentence_is_cut_at_the_stored_ceiling(self, tmp_path: Any) -> None:
        from jarvis.app.wake_greeting import MAX_GREETING_CHARS

        service = _settings_service(tmp_path)
        outcome = service.apply({"wake_greeting": "喂" * (MAX_GREETING_CHARS + 50)})
        assert outcome["problems"] == {}
        assert len(service.wake_greeting()) == MAX_GREETING_CHARS

    def test_the_ceiling_is_applied_where_the_value_is_written(self, tmp_path: Any) -> None:
        """Pinned on the file, not on the getter.

        :meth:`wake_greeting` clips as it reads, so every assertion written against it
        passes even if the write side stopped sanitising -- and then the unbounded string
        is what sits in ``preferences.json``, what the settings panel re-loads into its
        text box, and what a later reader that trusts the stored value would speak.
        """
        import json

        from jarvis.app.preferences import WAKE_GREETING
        from jarvis.app.wake_greeting import MAX_GREETING_CHARS

        service = _settings_service(tmp_path)
        service.apply({"wake_greeting": f"\n {'喂' * (MAX_GREETING_CHARS + 40)} \n"})
        stored = json.loads((tmp_path / "p.json").read_text(encoding="utf-8"))[WAKE_GREETING]
        assert stored == "喂" * MAX_GREETING_CHARS

    def test_the_page_report_releases_the_same_gate_the_voice_side_waits_on(self) -> None:
        from jarvis.ui.desktop import HudBridge

        greeter = _greeter()
        greeter.expect_figure()
        bridge = HudBridge(cast(Any, _NoSystem()), cast(Any, None))  # disk is unused here
        bridge.attach_greeter(greeter)
        assert bridge.pet_arrived() == {"ok": True, "error": ""}
        assert greeter.wait_for_figure() is True
        assert greeter.skipped_waits == 0

    def test_a_report_with_nothing_wired_answers_in_a_shape_the_page_can_read(
        self,
    ) -> None:
        from jarvis.ui.desktop import HudBridge

        bridge = HudBridge(cast(Any, _NoSystem()), cast(Any, None))
        assert bridge.pet_arrived()["ok"] is False


class _Worlds:
    """The three production objects, attached to each other the way ``__main__`` does.

    Everything above this class tests one side of a seam with a double in the middle.
    This one has no double in the middle: the words come from a real ``SettingsService``,
    the wait is armed by the real ``ShellLifecycle.on_event`` dispatching the real
    pipeline's own ``wake`` event, and it is released by the same ``HudBridge.pet_arrived``
    the page calls. A greeter that reaches the shell but not the voice stack -- or the
    other way round, which is exactly what a missing ``greeter=`` in the composition root
    would do -- is invisible to every test above and fails here.
    """

    def __init__(self, tmp_path: Any, *, timeout: float) -> None:
        from jarvis.ui.desktop import HudBridge
        from jarvis.ui.lifecycle import ShellLifecycle

        self.settings = _settings_service(tmp_path)
        self.greeter = WakeGreeter(self.settings.wake_greeting, timeout_seconds=timeout)
        self.pet = _Pet()
        shell = ShellLifecycle(window=_Window(), pet=self.pet, hide_to_tray=True)
        shell.attach_greeter(self.greeter)
        # Hidden is the only state in which a wake word summons a figure at all.
        assert shell.hide() is True
        self.bridge = HudBridge(cast(Any, _NoSystem()), cast(Any, None))
        self.bridge.attach_greeter(self.greeter)
        pipeline, tts, _graph = _pipeline(
            on_event=shell.on_event,
            provider=self.greeter.text,
            gate=self.greeter.wait_for_figure,
        )
        self.pipeline = pipeline
        self.tts = tts
        self.started_at = time.monotonic()

    def wake(self) -> None:
        """One wake word, exactly as the microphone thread would see it."""
        self.started_at = time.monotonic()
        self.pipeline._on_wake(_wake())

    def settle(self) -> float:
        """Wait for the greeting to finish, and return how long the whole thing took."""
        assert _greeting_finished(self.pipeline), "the greeting never handed the microphone back"
        return time.monotonic() - self.started_at


class TestTheRealJoin:
    def test_the_words_come_out_only_after_the_page_says_she_is_fully_drawn(
        self, tmp_path: Any
    ) -> None:
        """A real thread, a real sleep, and the arrival reported 0.2s in.

        The bound is 3s, so an elapsed time near 0.2s can only mean the gate opened on
        the report; near 3s it would mean the greeting arrived on a deadline instead,
        which is the failure the operator described as "she talks before she shows up".
        """
        worlds = _Worlds(tmp_path, timeout=3.0)
        threading.Timer(0.2, worlds.bridge.pet_arrived).start()
        worlds.wake()
        elapsed = worlds.settle()

        assert worlds.tts.calls == [DEFAULT_GREETING]
        assert worlds.pet.summoned == 1
        assert elapsed >= 0.15, "the sentence was spoken while the figure was still arriving"
        assert elapsed < 1.5, "the gate opened on its deadline, not on the page's report"
        assert worlds.greeter.skipped_waits == 0

    def test_editing_the_sentence_reaches_the_next_wake_without_a_restart(
        self, tmp_path: Any
    ) -> None:
        """The settings panel is the point of the feature, so it is tested through the
        same objects rather than against the getter alone."""
        worlds = _Worlds(tmp_path, timeout=0.0)
        worlds.settings.apply({"wake_greeting": "在呢，请讲"})
        worlds.wake()
        worlds.settle()
        assert worlds.tts.calls == ["在呢，请讲"]

    def test_a_page_that_never_reports_costs_a_delay_not_the_greeting(self, tmp_path: Any) -> None:
        """Through the real join, because this is the case a stale UI bundle produces:
        the page has no ``reportPetArrived`` at all. The words must still come, and the
        counter must say that the release was a timeout rather than a report."""
        worlds = _Worlds(tmp_path, timeout=0.2)
        worlds.wake()
        elapsed = worlds.settle()
        assert worlds.tts.calls == [DEFAULT_GREETING]
        assert elapsed >= 0.15
        assert worlds.greeter.skipped_waits == 1

    def test_turning_the_greeting_off_in_the_panel_silences_the_wake_word(
        self, tmp_path: Any
    ) -> None:
        """And the gate is not consulted either: an armed wait with nothing to say would
        hold the microphone shut for a sentence that does not exist."""
        worlds = _Worlds(tmp_path, timeout=3.0)
        worlds.settings.apply({"wake_greeting": ""})
        worlds.wake()
        # The flag is raised by a greeting that begins, before anything waits, so an
        # unset one here says the 3s gate was never asked about -- while the figure is
        # summoned exactly as it would be with a greeting.
        assert worlds.pipeline._greeting.is_set() is False
        assert worlds.tts.calls == []
        assert worlds.pet.summoned == 1
        assert worlds.greeter.skipped_waits == 0


FRONTEND = Path(__file__).resolve().parent.parent / "frontend" / "src"


def _source(*relative: str) -> str:
    return FRONTEND.joinpath(*relative).read_text(encoding="utf-8")


class TestTheCrossLanguageSeam:
    """The arrival report crosses JS↔Python, and its only failure mode is silence.

    Checked against the source rather than by running the page, because "she never greets
    after the animation" otherwise needs a desktop, a microphone and a hidden window to
    reproduce -- and a renamed method on either side leaves both halves looking correct
    while the greeting waits out its deadline every single time.
    """

    def test_the_page_reports_to_the_method_the_shell_answers(self) -> None:
        stage = _source("avatar", "PetStage.vue")
        api = _source("api", "bridge.ts")
        assert "void reportPetArrived()" in stage, "the page stopped reporting the arrival"
        assert "target.pet_arrived()" in api, "the report no longer crosses to the shell"
        from jarvis.ui.desktop import HudBridge

        assert callable(getattr(HudBridge, "pet_arrived", None))

    def test_the_panel_edits_the_key_the_shell_reads(self) -> None:
        """A row bound to a name ``SettingsService.apply`` does not know is a text box
        that looks saved and is discarded."""
        panel = _source("components", "SettingsPanel.vue")
        assert 'v-model="form.wake_greeting"' in panel
        assert "wake_greeting: form.value.wake_greeting" in panel
        assert "wake_greeting_max" in panel, "the box no longer advertises the ceiling"
