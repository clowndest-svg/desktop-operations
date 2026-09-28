"""Tests for AsrService lifecycle (fake engine, no hardware / native deps)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import cast

import pytest

from jarvis.asr.service import AsrService, AsrSettings, default_engine_factory
from jarvis.asr.types import AsrResultType, AsrStream, RecognitionResult
from jarvis.config.loader import load_defaults
from jarvis.config.schema import AsrSection
from jarvis.core.exceptions import AsrError
from jarvis.vad.types import SpeechSegment


def is_running(service: AsrService) -> bool:
    """Read ``service.running`` through a call so mypy cannot narrow the
    property expression to ``Literal[True]`` across ``service.stop()``."""
    return service.running


def asr_section(**overrides: object) -> AsrSection:
    raw = dict(cast(Mapping[str, object], load_defaults()["asr"]))
    raw.update(overrides)
    return AsrSection.from_mapping(raw)


def settings(**overrides: object) -> AsrSettings:
    return AsrSettings(section=asr_section(**overrides))


class FakeAsrEngine:
    """Deterministic engine that records the PCM it decoded."""

    def __init__(self, section: AsrSection | None = None) -> None:
        self.section = section
        self.closed = False
        self.last_audio: bytes | None = None
        self.last_segment: SpeechSegment | None = None

    @property
    def name(self) -> str:
        return "fake"

    @property
    def sample_rate(self) -> int:
        return 16_000

    def recognize(
        self,
        audio: bytes,
        *,
        segment: SpeechSegment | None = None,
        language: str | None = None,
    ) -> RecognitionResult:
        self.last_audio = audio
        self.last_segment = segment
        return RecognitionResult(
            text=f"decoded:{len(audio)}",
            type=AsrResultType.FINAL,
            language=language,
            segment=segment,
        )

    def stream(self) -> AsrStream:
        from jarvis.asr.engines import BufferedAsrStream

        return BufferedAsrStream(self)

    def close(self) -> None:
        self.closed = True


def test_disabled_service_does_not_load_engine() -> None:
    factory_calls: list[object] = []
    service = AsrService(
        lambda: settings(enabled=False),
        engine_factory=lambda s: factory_calls.append(s) or FakeAsrEngine(s),  # type: ignore[func-returns-value]
    )
    service.start()
    assert not is_running(service)
    assert factory_calls == []  # engine never even constructed
    service.stop()  # must be a safe no-op


def test_enabled_service_loads_engine_recognizes_and_stops() -> None:
    engine = FakeAsrEngine()
    service = AsrService(
        lambda: settings(enabled=True),
        engine_factory=lambda s: engine,
    )
    service.start()
    assert is_running(service)
    assert not engine.closed

    result = service.recognize(b"\x00" * 64)
    assert result.is_final
    assert result.text == "decoded:64"

    service.stop()
    assert not is_running(service)
    assert engine.closed


def test_recognize_before_start_raises() -> None:
    service = AsrService(
        lambda: settings(enabled=False),
        engine_factory=lambda s: FakeAsrEngine(s),
    )
    with pytest.raises(AsrError, match="not loaded"):
        service.recognize(b"\x00" * 16)


def test_transcribe_segment_slices_audio_by_vad_span() -> None:
    engine = FakeAsrEngine()
    service = AsrService(
        lambda: settings(enabled=True),
        engine_factory=lambda s: engine,
    )
    service.start()

    # 300 samples of audio; VAD detected speech in samples 100..200.
    audio = bytes(range(256)) + bytes(range(256)) + bytes(range(88))  # 600 bytes
    segment = SpeechSegment(
        start_sample=100,
        end_sample=200,
        start_time=100 / 16_000,
        end_time=200 / 16_000,
        sample_rate=16_000,
    )
    result = service.transcribe_segment(audio, segment)
    # The engine must receive exactly the sliced PCM (bytes 200..400).
    assert engine.last_audio == audio[200:400]
    assert engine.last_segment is segment
    assert result.segment is segment
    assert result.is_final

    service.stop()


def test_stream_requires_started_engine() -> None:
    service = AsrService(
        lambda: settings(enabled=False),
        engine_factory=lambda s: FakeAsrEngine(s),
    )
    with pytest.raises(AsrError, match="not loaded"):
        service.stream()


def test_open_stream_then_recognize_and_release() -> None:
    engine = FakeAsrEngine()
    service = AsrService(
        lambda: settings(enabled=True),
        engine_factory=lambda s: engine,
    )
    service.start()
    stream = service.stream()
    assert stream.push(b"abc") == []
    final = stream.finish()
    assert final.is_final
    assert engine.last_audio == b"abc"
    service.stop()


def test_stop_is_idempotent() -> None:
    service = AsrService(
        lambda: settings(enabled=True),
        engine_factory=lambda s: FakeAsrEngine(s),
    )
    service.start()
    service.stop()
    service.stop()  # second stop must not raise
    assert not is_running(service)


def test_default_engine_factory_unknown_engine_hard_fails() -> None:
    # Schema rejects this at validation time, but the factory is defensive too
    # for any caller that builds a section directly (bypassing validation).
    bad_section = AsrSection(
        enabled=True,
        engine="whisper-x",
        model="x",
        language="auto",
        temperature=0.0,
        beam_size=5,
        device="cpu",
    )
    with pytest.raises(AsrError, match="unknown asr engine"):
        default_engine_factory(bad_section)
