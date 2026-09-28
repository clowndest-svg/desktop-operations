"""Tests for ASR engines: fake recognise/stream + missing-dependency path."""

from __future__ import annotations

import pytest

from jarvis.asr.engines import BufferedAsrStream, SenseVoiceAsrEngine
from jarvis.asr.types import AsrResultType, AsrStream, RecognitionResult, SpeechRecognizer
from jarvis.config.loader import load_defaults
from jarvis.config.schema import AsrSection
from jarvis.core.exceptions import AsrError
from jarvis.vad.types import SpeechSegment


def asr_section(**overrides: object) -> AsrSection:
    from collections.abc import Mapping
    from typing import cast

    raw = dict(cast(Mapping[str, object], load_defaults()["asr"]))
    raw.update(overrides)
    return AsrSection.from_mapping(raw)


class FakeAsrStream:
    """Emits one PARTIAL mid-stream, then the FINAL on finish."""

    def __init__(self, engine: SpeechRecognizer) -> None:
        self._engine = engine
        self._buffer = bytearray()
        self._pushed = 0

    def push(self, chunk: bytes) -> list[RecognitionResult]:
        self._buffer.extend(chunk)
        self._pushed += 1
        # Emit a partial after the first chunk, echoing what we have so far.
        return [
            RecognitionResult(
                text=f"partial:{len(self._buffer)}",
                type=AsrResultType.PARTIAL,
            )
        ]

    def finish(self) -> RecognitionResult:
        return RecognitionResult(
            text=f"final:{len(self._buffer)}",
            type=AsrResultType.FINAL,
        )


class FakeAsrEngine:
    """Records the PCM it is asked to decode; hands back deterministic text."""

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
        return FakeAsrStream(self)

    def close(self) -> None:
        self.closed = True


def test_fake_engine_recognize_returns_final() -> None:
    engine = FakeAsrEngine()
    result = engine.recognize(b"\x00" * 100)
    assert result.is_final
    assert result.text == "decoded:100"
    assert engine.last_audio == b"\x00" * 100


def test_fake_stream_emits_partial_then_final() -> None:
    engine = FakeAsrEngine()
    stream = engine.stream()
    partials = stream.push(b"abc")
    assert len(partials) == 1
    assert partials[0].type is AsrResultType.PARTIAL
    final = stream.finish()
    assert final.is_final
    assert final.text == "final:3"


def test_buffered_stream_accumulates_and_finishes() -> None:
    engine = FakeAsrEngine()
    stream = BufferedAsrStream(engine)
    assert stream.push(b"hello") == []  # SenseVoice variant emits no partials
    assert stream.push(b"world") == []
    final = stream.finish()
    assert final.is_final
    assert engine.last_audio == b"helloworld"


def test_sensevoice_missing_dependency_raises_asr_error() -> None:
    # Only meaningful where the heavy ML stack is absent: a real ``funasr``
    # import succeeds and the constructor starts loading actual model weights.
    try:
        import funasr  # noqa: F401
    except ImportError:
        pass
    else:  # pragma: no cover - environment has the package installed
        pytest.skip("funasr installed; cannot exercise missing path")
    # In the sandbox `funasr` is not installed, so constructing the engine
    # must fail fast with a precise, actionable error (never a raw ImportError).
    with pytest.raises(AsrError) as exc_info:
        SenseVoiceAsrEngine(asr_section())
    assert exc_info.value.details.get("missing_package") == "funasr"
    assert exc_info.value.details.get("engine") == "sensevoice"


def test_sensevoice_engine_when_available() -> None:
    # Only runs where the heavy ML stack is actually installed.
    pytest.importorskip("funasr")
    engine = SenseVoiceAsrEngine(asr_section())
    assert engine.name == "sensevoice"
    assert engine.sample_rate == 16_000
    engine.close()
