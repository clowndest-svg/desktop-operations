"""Tests for WakeWordService lifecycle (fake engine + fake source, no hardware)."""

from __future__ import annotations

import threading
import time
from collections.abc import Mapping
from typing import cast

import pytest

from jarvis.audio.format import DEFAULT_FORMAT, AudioFormat
from jarvis.config.loader import load_defaults
from jarvis.config.schema import AppConfig, WakeWordSection
from jarvis.wakeword.service import WakeWordService, WakeWordSettings
from jarvis.wakeword.types import WakeEvent, WakeHit


def is_running(service: WakeWordService) -> bool:
    """Read ``service.running`` through a call so mypy cannot narrow the
    property expression to ``Literal[True]`` across ``service.stop()``."""
    return service.running


def wakeword_section(**overrides: object) -> WakeWordSection:
    raw = dict(cast(Mapping[str, object], load_defaults()["wakeword"]))
    raw.update(overrides)
    return WakeWordSection.from_mapping(raw)


def settings(**overrides: object) -> WakeWordSettings:
    return WakeWordSettings(section=wakeword_section(**overrides), environ={})


class PulseEngine:
    """Fires one strong hit on the very first frame, silence afterwards."""

    def __init__(self) -> None:
        self.closed = False
        self._fired = False

    @property
    def name(self) -> str:
        return "pulse"

    @property
    def frame_samples(self) -> int:
        return 4

    def process(self, frame: bytes) -> tuple[WakeHit, ...]:
        if not self._fired:
            self._fired = True
            return (WakeHit("jarvis", 0.99),)
        return ()

    def close(self) -> None:
        self.closed = True


class SilenceSource:
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
        return b"\x00" * DEFAULT_FORMAT.bytes_for_samples(samples)

    def close(self) -> None:
        self.closed.set()


def test_disabled_service_does_nothing() -> None:
    factory_calls: list[object] = []
    service = WakeWordService(
        lambda: settings(enabled=False),
        engine_factory=lambda s: factory_calls.append(s) or PulseEngine(),  # type: ignore[func-returns-value]
        source_factory=SilenceSource,
    )
    service.start()
    assert not service.running
    assert factory_calls == []  # engine never even constructed
    service.stop()  # must be a safe no-op


def test_enabled_service_emits_wake_event_and_stops_cleanly() -> None:
    engine = PulseEngine()
    source = SilenceSource()
    got_event = threading.Event()
    events: list[WakeEvent] = []

    def on_wake(event: WakeEvent) -> None:
        events.append(event)
        got_event.set()

    service = WakeWordService(
        lambda: settings(enabled=True),
        on_wake=on_wake,
        engine_factory=lambda s: engine,
        source_factory=lambda: source,
    )
    service.start()
    assert is_running(service)
    assert source.opened
    assert got_event.wait(timeout=5.0), "wake event was not emitted"
    assert events[0].keyword == "jarvis"

    service.stop()
    assert not is_running(service)
    assert engine.closed
    assert source.closed.is_set()


def test_stop_is_idempotent() -> None:
    service = WakeWordService(
        lambda: settings(enabled=True),
        engine_factory=lambda s: PulseEngine(),
        source_factory=SilenceSource,
    )
    service.start()
    service.stop()
    service.stop()  # second stop must not raise
    assert not service.running


def test_engine_closed_when_source_open_fails() -> None:
    engine = PulseEngine()

    class BrokenSource(SilenceSource):
        def open(self) -> None:
            raise OSError("no microphone")

    service = WakeWordService(
        lambda: settings(enabled=True),
        engine_factory=lambda s: engine,
        source_factory=BrokenSource,
    )
    with pytest.raises(OSError, match="no microphone"):
        service.start()
    assert engine.closed  # no leaked engine
    assert not service.running


def test_callback_exception_does_not_kill_loop() -> None:
    class TwoPulseEngine(PulseEngine):
        def __init__(self) -> None:
            super().__init__()
            self.hits = 0

        def process(self, frame: bytes) -> tuple[WakeHit, ...]:
            # Fire on the first two frames.
            if self.hits < 2:
                self.hits += 1
                return (WakeHit("jarvis", 0.99),)
            return ()

    second_event = threading.Event()
    calls: list[int] = []

    def exploding_callback(event: WakeEvent) -> None:
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("boom")
        second_event.set()

    service = WakeWordService(
        lambda: settings(enabled=True, cooldown_seconds=0.0),
        on_wake=exploding_callback,
        engine_factory=lambda s: TwoPulseEngine(),
        source_factory=SilenceSource,
    )
    service.start()
    assert second_event.wait(timeout=5.0), "loop died after callback exception"
    service.stop()


def test_default_config_wakeword_is_disabled() -> None:
    config = AppConfig.from_mapping(load_defaults())
    assert config.wakeword.enabled is False
    assert config.wakeword.engine == "asr"
    assert config.wakeword.keywords == ("你好小夜", "你好小叶", "你好晓叶", "你好小业")
