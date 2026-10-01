"""Tests for the end-to-end voice pipeline state machine.

The pipeline owns ONE microphone loop and drives wake-word -> VAD -> ASR ->
agent-graph -> TTS. Everything below is deterministic: scripted engines, a
replaying audio source, and in-memory fakes for ASR / graph / TTS. No real
models, microphones, or network are touched.

State machine under test:

    IDLE        wait for wake word -> LISTENING
    LISTENING   feed VAD; on SPEECH_END -> slice + ASR -> PROCESSING
    PROCESSING  run ASR text -> AgentGraph -> TTS (Barge-In watches the mic)
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from threading import Event, Lock
from typing import Any

from jarvis.orchestration.player import NullAudioPlayer
from jarvis.orchestration.types import PipelineEvent
from jarvis.orchestration.voice_pipeline import PipelineState, VoicePipeline, speech_level
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

# One capture frame at 16 kHz / s16le == 512 samples == 1024 bytes.
_FRAME = b"\x00" * 1024


def _wait_for(cond: Callable[[], bool], timeout: float = 2.0) -> bool:
    """Poll ``cond`` until true or ``timeout`` elapses (test helper)."""
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        time.sleep(0.01)
    return False


@dataclass
class Harness:
    """Bundles a configured pipeline with its fakes and event trace."""

    pipeline: VoicePipeline
    asr: FakeAsr
    graph: FakeGraph
    tts: FakeTts
    player: NullAudioPlayer
    events: list[PipelineEvent]
    finished: Event


def build(
    *,
    barge_in: bool = True,
    wake_hits: list[WakeHit] | None = None,
    vad_scores: list[float] | None = None,
    asr_text: str = "你好",
    graph_reply: str = "好的",
    graph_raises: Exception | None = None,
    tts_chunks: list[AudioChunk] | None = None,
    source_chunks: list[bytes] | None = None,
) -> Harness:
    """Construct a pipeline wired entirely to deterministic fakes."""
    events: list[PipelineEvent] = []
    lock = Lock()
    finished = Event()

    def on_event(event: PipelineEvent) -> None:
        with lock:
            events.append(event)
        if event.kind == "reply" and event.text == graph_reply:
            finished.set()

    detector = WakeWordDetector(
        ScriptedWakeWordEngine(wake_hits if wake_hits is not None else [WakeHit("jarvis", 1.0)]),
        threshold=0.0,
        cooldown_seconds=0.0,
    )
    segmenter = VoiceActivitySegmenter(
        # NOTE: the segmenter emits SPEECH_START only on the *second* consecutive
        # speech frame (its min-speech check runs in the else branch), so we
        # need >= 2 speech scores followed by >= 1 silence to get a full
        # SPEECH_END.
        ScriptedVadEngine(vad_scores if vad_scores is not None else [1.0, 1.0, 0.0]),
        sample_rate=16_000,
        threshold=0.5,
        min_speech_ms=10,
        max_silence_ms=10,
        speech_pad_ms=0,
        max_speech_ms=0,
    )
    asr = FakeAsr(asr_text)
    graph = FakeGraph(graph_reply, raises=graph_raises)
    tts = FakeTts(tts_chunks)
    player = NullAudioPlayer()
    source = ScriptedAudioSource(
        source_chunks if source_chunks is not None else [_FRAME, _FRAME, _FRAME, _FRAME]
    )
    pipeline = VoicePipeline(
        detector=detector,
        segmenter=segmenter,
        asr=asr,
        graph=graph,
        tts=tts,
        source_factory=lambda: source,
        player=player,
        barge_in=barge_in,
        on_event=on_event,
    )
    return Harness(pipeline, asr, graph, tts, player, events, finished)


def test_happy_path_runs_full_chain() -> None:
    h = build()
    h.pipeline.start()
    assert h.finished.wait(timeout=5.0), "agent reply was never emitted"
    # The processing turn must reset back to LISTENING afterwards.
    assert _wait_for(lambda: h.pipeline._get_state() == PipelineState.LISTENING, timeout=2.0)
    h.pipeline.stop()

    kinds = [e.kind for e in h.events]
    assert "wake" in kinds
    assert "speech_start" in kinds
    assert "speech_end" in kinds
    # First reply is the transcript, second is the agent's answer.
    replies = [e.text for e in h.events if e.kind == "reply"]
    assert replies == ["你好", "好的"]

    assert len(h.asr.calls) == 1
    assert len(h.graph.calls) == 1
    assert h.graph.calls[0][0] == "你好"
    assert h.tts.calls == ["好的"]
    assert h.player.total_bytes >= 1


def test_wake_command_is_answered_in_one_breath() -> None:
    """A wake phrase carrying its command must not force a second utterance.

    The transcript-matching wake engine transcribes the whole utterance anyway,
    so it hands the tail over on the wake event. Re-running ASR here would mean
    the assistant ignored what the user already said.
    """
    h = build(
        wake_hits=[WakeHit("jarvis", 1.0, command="帮我看下订单")],
        vad_scores=[0.0],  # no further speech: nothing left to endpoint
        graph_reply="今天有三单",
    )
    h.pipeline.start()
    assert h.finished.wait(timeout=5.0), "agent reply was never emitted"
    assert _wait_for(lambda: h.pipeline._get_state() == PipelineState.LISTENING, timeout=2.0)
    h.pipeline.stop()

    assert len(h.asr.calls) == 0
    assert h.graph.calls[0][0] == "帮我看下订单"
    kinds = [e.kind for e in h.events]
    assert "wake" in kinds
    assert "speech_end" not in kinds
    assert [e.text for e in h.events if e.kind == "reply"] == ["帮我看下订单", "今天有三单"]


def test_bare_wake_word_still_waits_for_a_command() -> None:
    """Without a command on the event, the classic two-utterance flow holds."""
    h = build(wake_hits=[WakeHit("jarvis", 1.0)])
    h.pipeline.start()
    assert h.finished.wait(timeout=5.0)
    h.pipeline.stop()

    assert len(h.asr.calls) == 1
    assert h.graph.calls[0][0] == "你好"


def test_empty_transcript_is_a_noop() -> None:
    h = build(asr_text="")  # ASR heard nothing
    h.pipeline.start()
    assert _wait_for(lambda: any(e.kind == "speech_end" for e in h.events), timeout=5.0)
    # No graph / TTS, and the state machine returns to LISTENING.
    assert _wait_for(lambda: h.pipeline._get_state() == PipelineState.LISTENING, timeout=2.0)
    h.pipeline.stop()

    assert len(h.graph.calls) == 0
    assert len(h.tts.calls) == 0
    assert not any(e.kind == "reply" for e in h.events)


def test_barge_in_interrupts() -> None:
    h = build(barge_in=True)
    h.pipeline._set_state(PipelineState.PROCESSING)
    # Speech needs two consecutive frames before SPEECH_START fires.
    h.pipeline._dispatch(_FRAME)
    h.pipeline._dispatch(_FRAME)
    assert h.pipeline._interrupt.is_set()
    assert any(e.kind == "barge_in" for e in h.events)
    h.pipeline.stop()


def test_barge_in_disabled_ignores_speech() -> None:
    h = build(barge_in=False)
    h.pipeline._set_state(PipelineState.PROCESSING)
    h.pipeline._dispatch(_FRAME)
    h.pipeline._dispatch(_FRAME)
    assert not h.pipeline._interrupt.is_set()
    assert not any(e.kind == "barge_in" for e in h.events)
    h.pipeline.stop()


def test_idempotent_start_stop() -> None:
    h = build()
    h.pipeline.start()
    assert h.pipeline.running
    h.pipeline.start()  # second start is a no-op
    h.pipeline.stop()
    h.pipeline.stop()  # second stop is a no-op
    assert not h.pipeline.running


def test_wake_word_required_to_listen() -> None:
    # No wake hits -> the pipeline stays in IDLE forever (no false triggers).
    h = build(wake_hits=[])
    h.pipeline.start()
    assert _wait_for(lambda: h.pipeline.running, timeout=1.0)
    # After a couple of silent frames, still idle and no speech events.
    assert _wait_for(lambda: h.pipeline._get_state() == PipelineState.IDLE, timeout=1.0)
    h.pipeline.stop()
    assert not any(e.kind in ("speech_start", "speech_end") for e in h.events)


def test_speak_now_runs_a_turn_without_the_wake_word() -> None:
    """The HUD's 「按一下说」: same chain, no keyword spoken."""
    h = build(wake_hits=[], source_chunks=[_FRAME] * 8)
    h.pipeline.start()
    assert _wait_for(lambda: h.pipeline.state == PipelineState.IDLE, timeout=1.0)

    assert h.pipeline.speak_now() is True
    assert h.finished.wait(timeout=5.0), "the manual turn never reached the agent"
    h.pipeline.stop()

    assert len(h.asr.calls) == 1
    assert [e.text for e in h.events if e.kind == "reply"] == ["你好", "好的"]
    assert not [e for e in h.events if e.kind == "wake"], (
        "a manual turn must not fabricate a wake: the wake event is what the "
        "cooldown and the keyword label are built on"
    )
    states = [e.text for e in h.events if e.kind == "state"]
    assert states[:2] == ["idle", "listening"], "the press must open the turn, not log only"


def test_speak_now_is_refused_while_a_turn_is_already_open() -> None:
    h = build(wake_hits=[], source_chunks=[_FRAME] * 4)
    h.pipeline.start()

    assert h.pipeline.speak_now() is True
    assert h.pipeline.speak_now() is False, (
        "a second press must not reset the buffer mid-utterance -- that would "
        "drop what the user already said"
    )
    h.pipeline.stop()


def test_speak_now_is_refused_before_the_loop_runs() -> None:
    h = build()

    assert h.pipeline.speak_now() is False, "no capture thread means nothing to listen"
    assert h.pipeline.state == PipelineState.IDLE


class TestTurnFailureIsExplained:
    """A turn that cannot answer has to say why, in the window, not only in a log.

    The first shipped build woke on a real voice, answered nothing, and showed
    ``processing failed`` -- the actual cause was one unset environment variable,
    and nothing on screen pointed at it.
    """

    def test_a_missing_api_key_names_the_variable_on_screen(self) -> None:
        from jarvis.llm.errors import LlmAuthError

        h = build(graph_raises=LlmAuthError("API key 'QWENAI_API_KEY' is not set"))
        h.pipeline.start()
        assert _wait_for(lambda: any(e.kind == "error" for e in h.events), timeout=5.0)
        h.pipeline.stop()

        text = " ".join(e.text for e in h.events if e.kind == "error")
        assert "QWENAI_API_KEY" in text
        assert text != "processing failed"

    def test_an_unexpected_error_shows_its_kind_and_not_its_content(self) -> None:
        h = build(graph_raises=ValueError("Authorization: Bearer sk-secret-token"))
        h.pipeline.start()
        assert _wait_for(lambda: any(e.kind == "error" for e in h.events), timeout=5.0)
        h.pipeline.stop()

        text = " ".join(e.text for e in h.events if e.kind == "error")
        assert "ValueError" in text, "the operator has to be able to name it in a bug report"
        assert "sk-secret-token" not in text, (
            "an unexpected exception can carry whatever the transport echoed back; "
            "the log is the place for that, not a screen that gets photographed"
        )

    def test_a_failed_turn_still_gives_the_microphone_back(self) -> None:
        """Failing must not strand the pipeline in PROCESSING."""
        from jarvis.llm.errors import LlmConnectionError

        h = build(graph_raises=LlmConnectionError("connection reset"))
        h.pipeline.start()
        assert _wait_for(lambda: any(e.kind == "error" for e in h.events), timeout=5.0)
        assert _wait_for(lambda: h.pipeline.state is PipelineState.LISTENING, timeout=2.0)
        h.pipeline.stop()


class GatedTts:
    """A synthesizer that produces its second chunk only when told to.

    Cancellation is a race, and a race needs something slow on the other side of it
    to be a test rather than a coin flip.
    """

    name = "gated"
    sample_rate = 16_000
    running = True

    def __init__(self, gate: Event, chunks: list[AudioChunk]) -> None:
        self._gate = gate
        self._chunks = chunks
        self.calls: list[str] = []
        self.voices: list[str | None] = []

    def synthesize(
        self, text: str, *, voice: str | None = None, should_stop: Any = None
    ) -> Iterator[AudioChunk]:
        self.calls.append(text)
        self.voices.append(voice)
        yield self._chunks[0]
        self._gate.wait(timeout=2.0)
        for chunk in self._chunks[1:]:
            if should_stop is not None and should_stop():
                return
            yield chunk

    def close(self) -> None:
        return None


class TestReadAloud:
    """``utter`` gives a typed answer a voice without borrowing the microphone."""

    def test_a_sentence_is_synthesized_and_played(self) -> None:
        tts = FakeTts()
        player = NullAudioPlayer()
        pipeline = _bare_pipeline(tts=tts, player=player)
        pipeline.start()
        try:
            assert pipeline.utter("今天是星期六") is True
            assert _wait_for(lambda: player.total_bytes > 0)
            assert tts.calls == ["今天是星期六"]
        finally:
            pipeline.stop()

    def test_the_chosen_voice_is_asked_per_utterance_not_baked_at_boot(self) -> None:
        """A voice picked in the HUD must change the *next* sentence.

        The engine takes the id per call, so the pipeline has to ask every time --
        a value captured at boot would make the picker a restart-only setting.
        """
        gate = Event()
        tts = GatedTts(gate, [AudioChunk(audio=b"a" * 4, sample_rate=16_000, is_final=True)])
        chosen = ["zh-CN-XiaoxiaoNeural"]
        pipeline = _bare_pipeline(tts=tts, voice_provider=lambda: chosen[0])
        pipeline.start()
        try:
            assert pipeline.utter("第一段") is True
            assert _wait_for(lambda: tts.calls == ["第一段"])
            gate.set()
            assert _wait_for(lambda: not pipeline.reading)
            assert tts.voices == ["zh-CN-XiaoxiaoNeural"]
            chosen[0] = "zh-CN-YunxiNeural"
            gate.clear()
            assert pipeline.utter("第二段") is True
            assert _wait_for(lambda: tts.voices[-1] == "zh-CN-YunxiNeural")
        finally:
            pipeline.stop()

    def test_a_voice_provider_that_raises_falls_back_to_the_configured_voice(
        self,
    ) -> None:
        def broken() -> str | None:
            raise RuntimeError("prefs unreadable")

        gate = Event()
        tts = GatedTts(gate, [AudioChunk(audio=b"a" * 4, sample_rate=16_000, is_final=True)])
        pipeline = _bare_pipeline(tts=tts, voice_provider=broken)
        pipeline.start()
        try:
            assert pipeline.utter("第一段") is True
            gate.set()
            assert _wait_for(lambda: tts.voices == [None])
        finally:
            pipeline.stop()

    def test_reading_does_not_move_the_state_machine(self) -> None:
        """The wake word has to stay audible while it talks. Flipping to PROCESSING
        would close the microphone on the person who wants to interrupt."""
        player = NullAudioPlayer()
        pipeline = _bare_pipeline(tts=FakeTts(), player=player)
        pipeline.start()
        try:
            assert pipeline.utter("你好") is True
            assert _wait_for(lambda: player.total_bytes > 0)
            assert pipeline.state is PipelineState.IDLE
        finally:
            pipeline.stop()

    def test_a_press_before_the_loop_exists_is_refused(self) -> None:
        h = build()

        assert h.pipeline.utter("你好") is False
        assert h.tts.calls == []

    def test_it_will_not_talk_over_a_turn_in_flight(self) -> None:
        """Two voices answering the same moment is how an operator ends up unable to
        say which question got answered."""
        h = build(
            wake_hits=[],
            vad_scores=[1.0, 1.0, 0.0],
            source_chunks=[_FRAME] * 4,
        )
        h.pipeline.start()
        try:
            assert h.pipeline.speak_now() is True
            assert h.pipeline.state is PipelineState.LISTENING
            assert h.pipeline.utter("你好") is False
        finally:
            h.pipeline.stop()

    def test_only_one_read_aloud_at_a_time(self) -> None:
        gate = Event()
        chunks = [
            AudioChunk(audio=b"a" * 4, sample_rate=16_000),
            AudioChunk(audio=b"b" * 4, sample_rate=16_000, is_final=True),
        ]
        tts = GatedTts(gate, chunks)
        pipeline = _bare_pipeline(tts=tts)
        pipeline.start()
        try:
            assert pipeline.utter("第一段") is True
            assert _wait_for(lambda: tts.calls == ["第一段"])
            assert pipeline.utter("第二段") is False
            gate.set()
        finally:
            pipeline.stop()

    def test_cancelling_stops_the_samples_that_have_not_been_played_yet(self) -> None:
        gate = Event()
        chunks = [
            AudioChunk(audio=b"a" * 4, sample_rate=16_000),
            AudioChunk(audio=b"b" * 4, sample_rate=16_000, is_final=True),
        ]
        tts = GatedTts(gate, chunks)
        player = NullAudioPlayer()
        pipeline = _bare_pipeline(tts=tts, player=player)
        pipeline.start()
        try:
            assert pipeline.utter("一段很长的话") is True
            assert _wait_for(lambda: player.total_bytes == 4)
            assert pipeline.cancel_utterance() is True
            gate.set()
            assert _wait_for(lambda: not pipeline.reading)
            assert player.total_bytes == 4, "the cancelled half kept playing"
        finally:
            pipeline.stop()

    def test_cancelling_with_nothing_reading_says_so(self) -> None:
        h = build()
        h.pipeline.start()
        try:
            assert h.pipeline.cancel_utterance() is False
        finally:
            h.pipeline.stop()


def _bare_pipeline(
    *, tts: Any, player: Any | None = None, voice_provider: Any | None = None
) -> VoicePipeline:
    """A pipeline with no frames queued: idle, running, and nothing else to do."""
    detector = WakeWordDetector(ScriptedWakeWordEngine([]), threshold=0.0, cooldown_seconds=0.0)
    segmenter = VoiceActivitySegmenter(
        ScriptedVadEngine([0.0]),
        sample_rate=16_000,
        threshold=0.5,
        min_speech_ms=10,
        max_silence_ms=10,
        speech_pad_ms=0,
        max_speech_ms=0,
    )
    return VoicePipeline(
        detector=detector,
        segmenter=segmenter,
        asr=FakeAsr("你好"),
        graph=FakeGraph("好的"),
        tts=tts,
        source_factory=lambda: ScriptedAudioSource([_FRAME]),
        player=player if player is not None else NullAudioPlayer(),
        barge_in=True,
        voice_provider=voice_provider,
    )


class TestSpeechLevel:
    """The one thing that makes "the mic heard nothing" visible in a log."""

    def test_silence_reads_as_zero(self) -> None:
        assert speech_level(b"") == (0, 0.0)
        assert speech_level(b"\x00\x00" * 100)[0] == 0

    def test_a_full_scale_tone_peaks_at_the_ceiling(self) -> None:
        import math
        from array import array as int16

        tone = int16("h")
        for index in range(1600):
            tone.append(int(30000 * math.sin(index * 0.2)))
        peak, rms = speech_level(tone.tobytes())

        assert peak > 29_000
        # A sine sits at peak/sqrt(2) on average; anything near the peak would mean
        # the RMS is reading the envelope, not the level.
        assert 18_000 < rms < 24_000

    def test_a_quiet_capture_stays_quiet(self) -> None:
        from array import array as int16

        quiet = int16("h", [40] * 800)
        peak, _rms = speech_level(quiet.tobytes())

        assert peak < 400, "the log's 'almost no audio' threshold is calibrated on this"

    def test_an_odd_byte_count_does_not_crash(self) -> None:
        assert speech_level(b"\x01\x02\x03")[0] >= 0
