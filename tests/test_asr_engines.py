"""Tests for ASR engines: fake recognise/stream + missing-dependency path."""

from __future__ import annotations

import os

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
    """Construct the real SenseVoice model. Off by default, deliberately.

    The reason is measured, not theoretical. With ``MODELSCOPE_CACHE`` unset -- which
    is exactly what a bare ``pytest`` gives you -- funasr goes to the hub for ~900 MB,
    lands it on the system drive against this project's own "nothing heavy on C:"
    rule, and on a network that blocks those downloads the suite does not *fail*, it
    just stops. A frozen run is far more expensive than a skipped one, because a skip
    tells you what did not run and a hang tells you nothing.

    The real gate for this path is ``scripts/verify_wake_words.py``, which says which
    cache it uses and exits non-zero on a miss.
    """
    if os.environ.get("JARVIS_RUN_MODEL_TESTS") != "1":
        pytest.skip("set JARVIS_RUN_MODEL_TESTS=1 to load the real ~900 MB weights")
    pytest.importorskip("funasr")
    engine = SenseVoiceAsrEngine(asr_section())
    assert engine.name == "sensevoice"
    assert engine.sample_rate == 16_000
    engine.close()


class _FakeTorch:
    """Stands in for the module attribute funasr actually fights over."""

    def __init__(self, threads: int = 1) -> None:
        self.threads = threads

    def get_num_threads(self) -> int:
        return self.threads

    def set_num_threads(self, count: int) -> None:
        self.threads = count


def _sensevoice_with_fakes(torch: _FakeTorch, model: object) -> object:
    """An engine assembled by hand, so no weights and no funasr are needed."""
    import numpy

    from jarvis.asr.engines import SenseVoiceAsrEngine

    engine = object.__new__(SenseVoiceAsrEngine)
    engine._model = model  # type: ignore[attr-defined]
    engine._torch = torch  # type: ignore[attr-defined]
    engine._numpy = numpy  # type: ignore[attr-defined]
    engine._language = "zh"  # type: ignore[attr-defined]
    engine._temperature = 0.0  # type: ignore[attr-defined]
    engine._beam_size = 1  # type: ignore[attr-defined]
    return engine


class _FunasrLikeModel:
    """Raises the thread count on the way in, the way ``_reset_runtime_configs`` does."""

    def __init__(self, torch: _FakeTorch, *, fails: bool = False) -> None:
        self._torch = torch
        self._fails = fails
        self.threads_during = -1

    def generate(self, **_kwargs: object) -> list[dict[str, object]]:
        self._torch.set_num_threads(4)
        self.threads_during = self._torch.get_num_threads()
        if self._fails:
            raise RuntimeError("inference exploded")
        return [{"text": "<|zh|><|NEUTRAL|>你好"}]


def test_recognize_hands_the_thread_budget_back_to_the_vad() -> None:
    """The idle burn was funasr's global thread count, left raised after inference.

    The capture loop runs *between* inferences. Leaving it at ncpu pushes a 32 ms VAD
    frame through a four-thread pool, which measured as most of a core spent on
    nothing at all.
    """
    torch = _FakeTorch(1)
    model = _FunasrLikeModel(torch)
    engine = _sensevoice_with_fakes(torch, model)

    result = engine.recognize(b"\x00\x00" * 160)  # type: ignore[attr-defined]

    assert result.text == "你好"
    assert model.threads_during == 4, "inference must still get funasr's own threads"
    assert torch.threads == 1, "and hand them back afterwards"


def test_thread_budget_is_restored_even_when_inference_fails() -> None:
    import pytest

    from jarvis.core.exceptions import AsrError

    torch = _FakeTorch(1)
    engine = _sensevoice_with_fakes(torch, _FunasrLikeModel(torch, fails=True))

    with pytest.raises(AsrError):
        engine.recognize(b"\x00\x00" * 160)  # type: ignore[attr-defined]

    assert torch.threads == 1, (
        "a failed turn that leaves the pool raised is the expensive case: the loop "
        "keeps listening, and keeps paying for four threads"
    )


def test_returning_threads_never_breaks_a_transcription() -> None:
    from jarvis.asr.engines import _return_threads_to_vad

    class Broken:
        @staticmethod
        def get_num_threads() -> int:
            raise RuntimeError("no torch here")

    _return_threads_to_vad(Broken())  # must not raise
