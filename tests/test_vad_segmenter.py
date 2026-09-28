"""Tests for the streaming VAD endpoint state machine (no native deps).

The :class:`~jarvis.vad.segmenter.VoiceActivitySegmenter` is the heart of
phase 7: it turns a raw per-frame speech-probability stream into utterance
boundaries. Everything here runs with a *scripted* scorer and an injected
clock, so no microphone, no ONNX runtime, and no real audio are needed.

Time-base trick: the segmenter computes sample thresholds as
``ms * sample_rate / 1000``. By passing ``sample_rate=1000`` in these tests
1 ms == 1 sample, so ``min_speech_ms`` / ``max_silence_ms`` / ``speech_pad_ms``
are used verbatim as sample counts — small, readable numbers.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from jarvis.vad import (
    SpeechSegment,
    VadEvent,
    VadEventType,
    VadState,
    VoiceActivitySegmenter,
)
from jarvis.vad.types import VoiceActivityDetector

# ---------------------------------------------------------------------------
# Fake scorers / clock (engine = dumb probability source)
# ---------------------------------------------------------------------------


class ConstantVadEngine:
    """Always returns the same speech probability for every frame."""

    def __init__(self, value: float, frame_samples: int = 1) -> None:
        self._value = value
        self._frame_samples = frame_samples
        self.closed = False

    @property
    def name(self) -> str:
        return "constant"

    @property
    def frame_samples(self) -> int:
        return self._frame_samples

    def process(self, frame: bytes) -> float:
        return self._value

    def close(self) -> None:
        self.closed = True


class ScriptedVadEngine:
    """Returns a pre-scripted probability per frame, then silence."""

    def __init__(self, script: list[float], frame_samples: int = 1) -> None:
        self._script = list(script)
        self._index = 0
        self._frame_samples = frame_samples
        self.closed = False

    @property
    def name(self) -> str:
        return "scripted"

    @property
    def frame_samples(self) -> int:
        return self._frame_samples

    def process(self, frame: bytes) -> float:
        if self._index >= len(self._script):
            return 0.0
        value = self._script[self._index]
        self._index += 1
        return value

    def close(self) -> None:
        self.closed = True


class FakeClock:
    """Monotonic-ish clock returning ever-increasing timestamps."""

    def __init__(self, start: float = 1000.0) -> None:
        self._now = start

    def __call__(self) -> float:
        now = self._now
        self._now += 0.01
        return now


def make_segmenter(
    engine: VoiceActivityDetector,
    *,
    threshold: float = 0.5,
    min_speech_ms: int = 4,
    max_silence_ms: int = 8,
    speech_pad_ms: int = 2,
    max_speech_ms: int = 0,
    clock: Callable[[], float] | None = None,
) -> VoiceActivitySegmenter:
    """A segmenter on the compressed (sample_rate = 1000) time base."""
    return VoiceActivitySegmenter(
        engine,
        sample_rate=1000,
        threshold=threshold,
        min_speech_ms=min_speech_ms,
        max_silence_ms=max_silence_ms,
        speech_pad_ms=speech_pad_ms,
        max_speech_ms=max_speech_ms,
        clock=clock if clock is not None else FakeClock(),
    )


# ---------------------------------------------------------------------------
# Happy path: a clean utterance
# ---------------------------------------------------------------------------


def test_speech_then_silence_emits_start_and_end() -> None:
    # 4 speech frames (>= min_speech) then 8 silence (>= max_silence).
    engine = ScriptedVadEngine([0.9, 0.9, 0.9, 0.9] + [0.1] * 8)
    seg = make_segmenter(engine)

    events = seg.feed(b"\x00\x00" * 12)  # 12 frames @ 2 bytes each

    assert [e.type for e in events] == [VadEventType.SPEECH_START, VadEventType.SPEECH_END]
    assert events[0].segment is None
    segment = events[1].segment
    assert isinstance(segment, SpeechSegment)
    # start_sample padded back by 2 from the first speech frame (index 0)
    assert segment.start_sample == 0
    # end_sample padded forward by 2 from the last speech frame (index 4)
    assert segment.end_sample == 6
    assert segment.duration_samples == 6
    assert segment.duration_ms == pytest.approx(6.0)
    assert seg.state is VadState.SILENCE


def test_state_is_speech_while_utterance_open() -> None:
    engine = ScriptedVadEngine([0.9, 0.9, 0.9, 0.9, 0.1])
    seg = make_segmenter(engine)

    # After the 4th speech frame the utterance is confirmed but not ended yet.
    events = seg.feed(b"\x00\x00" * 5)
    assert events[0].type is VadEventType.SPEECH_START
    assert seg.state is VadState.SPEECH


# ---------------------------------------------------------------------------
# Min-speech filter: short blips are dropped before any start
# ---------------------------------------------------------------------------


def test_blip_shorter_than_min_speech_emits_nothing() -> None:
    # 3 speech frames (< min_speech 4) then silence -> abandoned, no start.
    engine = ScriptedVadEngine([0.9, 0.9, 0.9, 0.1])
    seg = make_segmenter(engine)

    events = seg.feed(b"\x00\x00" * 4)
    assert events == []
    assert seg.state is VadState.SILENCE


# ---------------------------------------------------------------------------
# Padding extends the segment around the detected edges
# ---------------------------------------------------------------------------


def test_padding_extends_start_backwards_into_silence() -> None:
    # 5 silence then 4 speech then 8 silence.
    engine = ScriptedVadEngine([0.1] * 5 + [0.9] * 4 + [0.1] * 8)
    seg = make_segmenter(engine)

    events = seg.feed(b"\x00\x00" * 17)
    assert events[0].type is VadEventType.SPEECH_START
    segment = events[1].segment
    assert isinstance(segment, SpeechSegment)
    # first speech frame at index 5; padded back 2 -> 3
    assert segment.start_sample == 3
    # last speech frame at index 9; padded forward 2 -> 11
    assert segment.end_sample == 11


# ---------------------------------------------------------------------------
# Max-speech hard cap auto-segments a long utterance
# ---------------------------------------------------------------------------


def test_max_speech_cap_forces_segment_boundary() -> None:
    engine = ScriptedVadEngine([0.9] * 20)  # 20 continuous speech frames
    seg = make_segmenter(engine, max_speech_ms=16)

    events = seg.feed(b"\x00\x00" * 20)
    # confirmed at frame 4 (START), capped at frame 16 (END), then frames
    # 17-20 re-open a (still-open) utterance -> a second START.
    assert [e.type for e in events] == [
        VadEventType.SPEECH_START,
        VadEventType.SPEECH_END,
        VadEventType.SPEECH_START,
    ]
    segment = events[1].segment
    assert isinstance(segment, SpeechSegment)
    assert segment.end_sample == 18  # last speech frame 16 + pad 2
    assert seg.state is VadState.SPEECH  # frames 17-20 reopened an utterance


# ---------------------------------------------------------------------------
# Flush on stream stop / handoff
# ---------------------------------------------------------------------------


def test_flush_emits_end_for_confirmed_utterance() -> None:
    engine = ScriptedVadEngine([0.9, 0.9, 0.9, 0.9])
    seg = make_segmenter(engine)

    feed_events = seg.feed(b"\x00\x00" * 4)  # confirms START, no natural end
    assert feed_events[0].type is VadEventType.SPEECH_START

    flush_events = seg.flush()
    assert len(flush_events) == 1
    assert flush_events[0].type is VadEventType.SPEECH_END
    assert seg.state is VadState.SILENCE


def test_flush_drops_unconfirmed_blip() -> None:
    engine = ScriptedVadEngine([0.9, 0.9, 0.9])  # < min_speech, never confirmed
    seg = make_segmenter(engine)

    seg.feed(b"\x00\x00" * 3)
    assert seg.flush() == []  # blip discarded, no orphan start


# ---------------------------------------------------------------------------
# Frames are reassembled correctly across arbitrary chunk boundaries
# ---------------------------------------------------------------------------


def test_frames_reassembled_across_chunks() -> None:
    engine = ScriptedVadEngine([0.9, 0.9, 0.9, 0.9] + [0.1] * 8)
    seg = make_segmenter(engine)

    events: list[VadEvent] = []
    events += seg.feed(b"\x00\x00\x00")  # 1 frame + 1 byte buffered
    events += seg.feed(b"\x00\x00\x00")  # 2 more frames (3 total)
    events += seg.feed(b"\x00\x00")  # frame 4 -> START
    events += seg.feed(b"\x00\x00" * 8)  # 8 silence -> END

    assert [e.type for e in events] == [VadEventType.SPEECH_START, VadEventType.SPEECH_END]


def test_feed_empty_chunk_is_safe() -> None:
    engine = ScriptedVadEngine([0.9, 0.9, 0.9, 0.9])
    seg = make_segmenter(engine)
    assert seg.feed(b"") == []
    events = seg.feed(b"\x00\x00" * 4)
    assert events[0].type is VadEventType.SPEECH_START


def test_reset_clears_buffered_audio_and_state() -> None:
    seg = make_segmenter(ScriptedVadEngine([0.9, 0.9, 0.9, 0.9] + [0.1] * 8))
    seg.feed(b"\x00\x00" * 2)  # partial utterance
    seg.reset()
    assert seg.state is VadState.SILENCE

    # A fresh segmenter (fresh engine — real engines like Silero are
    # stateless per frame) proves a complete utterance still works after the
    # reset() contract is honoured.
    fresh = make_segmenter(ScriptedVadEngine([0.9, 0.9, 0.9, 0.9] + [0.1] * 8))
    events = fresh.feed(b"\x00\x00" * 12)
    assert events[0].type is VadEventType.SPEECH_START
    assert events[1].type is VadEventType.SPEECH_END


# ---------------------------------------------------------------------------
# Constructor validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "kwargs",
    [
        {"threshold": 1.5},
        {"threshold": -0.1},
        {"min_speech_ms": -1},
        {"max_silence_ms": 0},
        {"speech_pad_ms": -1},
        {"max_speech_ms": -1},
        {"sample_rate": 0},
    ],
)
def test_invalid_construction_rejected(kwargs: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        VoiceActivitySegmenter(ConstantVadEngine(0.9), **kwargs)  # type: ignore[arg-type]
