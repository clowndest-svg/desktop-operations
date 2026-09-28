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

from collections.abc import Callable
from dataclasses import dataclass
from threading import Event, Lock

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
    graph = FakeGraph(graph_reply)
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
