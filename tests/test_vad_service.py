"""Tests for VadService lifecycle (fake engine + fake source, no hardware)."""

from __future__ import annotations

import threading
import time
from collections.abc import Mapping
from typing import cast

import pytest

from jarvis.audio.format import DEFAULT_FORMAT, AudioFormat
from jarvis.config.loader import load_defaults
from jarvis.config.schema import AppConfig, VadSection
from jarvis.vad import VadEvent, VadEventType, VadService, VadSettings


def is_running(service: VadService) -> bool:
    """Read ``service.running`` through a call so mypy cannot narrow the
    property expression to ``Literal[True]`` across ``service.stop()``."""
    return service.running


def vad_section(**overrides: object) -> VadSection:
    raw = dict(cast(Mapping[str, object], load_defaults()["vad"]))
    raw.update(overrides)
    return VadSection.from_mapping(raw)


def settings(sample_rate: int = 1000, **overrides: object) -> VadSettings:
    return VadSettings(section=vad_section(**overrides), sample_rate=sample_rate)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class ConstantVadEngine:
    """Always returns the same probability for every frame."""

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


class ScriptedSource:
    """Endless zero PCM; blocks briefly per read to mimic a real device."""

    def __init__(self) -> None:
        self.opened = False
        self.closed = threading.Event()

    @property
    def format(self) -> AudioFormat:
        return DEFAULT_FORMAT

    def open(self) -> None:
        self.opened = True

    def read(self, samples: int) -> bytes:
        if self.closed.is_set():
            raise OSError("device closed")
        time.sleep(0.001)
        return b"\x00" * (samples * 2)

    def close(self) -> None:
        self.closed.set()


# ---------------------------------------------------------------------------
# Lifecycle tests
# ---------------------------------------------------------------------------


def test_disabled_service_does_nothing() -> None:
    factory_calls: list[object] = []
    service = VadService(
        lambda: settings(enabled=False),
        engine_factory=lambda s: factory_calls.append(s) or ConstantVadEngine(0.9),  # type: ignore[func-returns-value]
        source_factory=ScriptedSource,
    )
    service.start()
    assert not is_running(service)
    assert factory_calls == []  # engine never even constructed
    service.stop()  # must be a safe no-op


def test_enabled_service_emits_speech_segment_and_stops_cleanly() -> None:
    engine = ScriptedVadEngine([0.9, 0.9, 0.9, 0.9] + [0.1] * 12, frame_samples=1)
    source = ScriptedSource()
    got_end = threading.Event()
    events: list[VadEvent] = []

    def on_event(event: VadEvent) -> None:
        events.append(event)
        if event.type is VadEventType.SPEECH_END:
            got_end.set()

    service = VadService(
        lambda: settings(enabled=True, min_speech_ms=4, max_silence_ms=8, speech_pad_ms=2),
        on_event=on_event,
        engine_factory=lambda s: engine,
        source_factory=lambda: source,
    )
    service.start()
    assert is_running(service)
    assert source.opened
    assert got_end.wait(timeout=5.0), "speech end was not emitted"
    assert events[0].type is VadEventType.SPEECH_START
    service.stop()
    assert not is_running(service)
    assert engine.closed
    assert source.closed.is_set()


def test_flush_on_stop_emits_trailing_segment() -> None:
    engine = ConstantVadEngine(0.9, frame_samples=1)
    source = ScriptedSource()
    events: list[VadEvent] = []
    start_seen = threading.Event()

    def on_event(event: VadEvent) -> None:
        events.append(event)
        if event.type is VadEventType.SPEECH_START:
            start_seen.set()

    service = VadService(
        lambda: settings(enabled=True, min_speech_ms=4, max_silence_ms=8, speech_pad_ms=2),
        on_event=on_event,
        engine_factory=lambda s: engine,
        source_factory=lambda: source,
    )
    service.start()
    assert start_seen.wait(timeout=5.0), "speech start not emitted"
    service.stop()
    assert any(
        e.type is VadEventType.SPEECH_END for e in events
    ), "stop did not flush the final (still-open) segment"
    assert engine.closed


def test_stop_is_idempotent() -> None:
    service = VadService(
        lambda: settings(enabled=True, min_speech_ms=4, max_silence_ms=8, speech_pad_ms=2),
        engine_factory=lambda s: ConstantVadEngine(0.9, frame_samples=1),
        source_factory=ScriptedSource,
    )
    service.start()
    service.stop()
    service.stop()  # second stop must not raise
    assert not is_running(service)


def test_engine_closed_when_source_open_fails() -> None:
    engine = ConstantVadEngine(0.9, frame_samples=1)

    class BrokenSource(ScriptedSource):
        def open(self) -> None:
            raise OSError("no microphone")

    service = VadService(
        lambda: settings(enabled=True, min_speech_ms=4, max_silence_ms=8, speech_pad_ms=2),
        engine_factory=lambda s: engine,
        source_factory=BrokenSource,
    )
    with pytest.raises(OSError, match="no microphone"):
        service.start()
    assert engine.closed  # no leaked engine
    assert not is_running(service)


def test_callback_exception_does_not_kill_loop() -> None:
    # Two complete utterances in one read: START, END, START, END.
    engine = ScriptedVadEngine([0.9] * 4 + [0.1] * 12 + [0.9] * 4 + [0.1] * 12, frame_samples=1)
    source = ScriptedSource()
    second_start = threading.Event()
    call_count = 0

    def on_event(event: VadEvent) -> None:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise RuntimeError("boom")
        if event.type is VadEventType.SPEECH_START:
            second_start.set()

    service = VadService(
        lambda: settings(enabled=True, min_speech_ms=4, max_silence_ms=8, speech_pad_ms=2),
        on_event=on_event,
        engine_factory=lambda s: engine,
        source_factory=lambda: source,
    )
    service.start()
    assert second_start.wait(timeout=5.0), "loop died after callback exception"
    service.stop()


def test_default_config_vad_is_disabled() -> None:
    config = AppConfig.from_mapping(load_defaults())
    assert config.vad.enabled is False
    assert config.vad.engine == "silero"
    assert config.vad.threshold == pytest.approx(0.5)
    assert config.vad.min_speech_ms == 250
    assert config.vad.max_silence_ms == 500
    assert config.vad.speech_pad_ms == 100
    assert config.vad.max_speech_ms == 0
