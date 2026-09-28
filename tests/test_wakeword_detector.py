"""Tests for WakeWordDetector: threshold, cooldown, frame plumbing."""

from __future__ import annotations

import pytest

from jarvis.wakeword.detector import WakeWordDetector
from jarvis.wakeword.types import WakeHit


class ScriptedEngine:
    """Engine returning a pre-scripted hit tuple per processed frame."""

    def __init__(self, frame_samples: int, script: list[tuple[WakeHit, ...]]) -> None:
        self._frame_samples = frame_samples
        self._script = script
        self.frames_seen: list[bytes] = []
        self.closed = False

    @property
    def name(self) -> str:
        return "scripted"

    @property
    def frame_samples(self) -> int:
        return self._frame_samples

    def process(self, frame: bytes) -> tuple[WakeHit, ...]:
        self.frames_seen.append(frame)
        if self._script:
            return self._script.pop(0)
        return ()

    def close(self) -> None:
        self.closed = True


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def make_detector(
    script: list[tuple[WakeHit, ...]],
    *,
    threshold: float = 0.5,
    cooldown: float = 2.0,
    frame_samples: int = 4,
) -> tuple[WakeWordDetector, ScriptedEngine, FakeClock]:
    engine = ScriptedEngine(frame_samples, script)
    clock = FakeClock()
    detector = WakeWordDetector(engine, threshold=threshold, cooldown_seconds=cooldown, clock=clock)
    return detector, engine, clock


FRAME = b"\x00" * 8  # 4 samples * 2 bytes


def test_hit_above_threshold_fires_event() -> None:
    detector, _, _ = make_detector([(WakeHit("jarvis", 0.9),)])
    events = detector.feed(FRAME)
    assert len(events) == 1
    assert events[0].keyword == "jarvis"
    assert events[0].score == pytest.approx(0.9)


def test_hit_below_threshold_ignored() -> None:
    detector, _, _ = make_detector([(WakeHit("jarvis", 0.49),)])
    assert detector.feed(FRAME) == []


def test_cooldown_suppresses_burst() -> None:
    script: list[tuple[WakeHit, ...]] = [(WakeHit("jarvis", 0.9),), (WakeHit("jarvis", 0.95),)]
    detector, _, clock = make_detector(script, cooldown=2.0)
    assert len(detector.feed(FRAME)) == 1
    clock.advance(0.5)  # still inside cooldown
    assert detector.feed(FRAME) == []


def test_event_allowed_after_cooldown_expires() -> None:
    script: list[tuple[WakeHit, ...]] = [(WakeHit("jarvis", 0.9),), (WakeHit("jarvis", 0.95),)]
    detector, _, clock = make_detector(script, cooldown=2.0)
    assert len(detector.feed(FRAME)) == 1
    clock.advance(2.5)
    assert len(detector.feed(FRAME)) == 1


def test_partial_chunks_are_reassembled_into_engine_frames() -> None:
    detector, engine, _ = make_detector([], frame_samples=4)
    detector.feed(b"\x00" * 3)
    assert engine.frames_seen == []  # not enough for a frame yet
    detector.feed(b"\x00" * 5)
    assert len(engine.frames_seen) == 1
    assert len(engine.frames_seen[0]) == 8


def test_reset_clears_cooldown_and_buffer() -> None:
    script: list[tuple[WakeHit, ...]] = [(WakeHit("jarvis", 0.9),), (WakeHit("jarvis", 0.9),)]
    detector, _engine, _ = make_detector(script, cooldown=100.0)
    assert len(detector.feed(FRAME)) == 1
    detector.reset()
    assert len(detector.feed(FRAME)) == 1  # cooldown gone after reset


def test_invalid_threshold_rejected() -> None:
    engine = ScriptedEngine(4, [])
    with pytest.raises(ValueError, match="threshold"):
        WakeWordDetector(engine, threshold=1.5, cooldown_seconds=1.0)


def test_negative_cooldown_rejected() -> None:
    engine = ScriptedEngine(4, [])
    with pytest.raises(ValueError, match="cooldown"):
        WakeWordDetector(engine, threshold=0.5, cooldown_seconds=-1.0)
