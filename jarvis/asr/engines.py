"""Concrete ASR engine adapters.

The default engine is SenseVoice via FunASR (offline, ONNX, key-free). It is
an *optional* runtime dependency (installed via the ``voice`` extra). Imports
happen lazily inside ``__init__`` so that the rest of JARVIS — and the whole
test suite — works on machines without the heavy ML stack; a missing package
surfaces as a precise :class:`AsrError` the moment the engine is constructed.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from jarvis.asr.types import (
    AsrResultType,
    AsrStream,
    RecognitionResult,
    SpeechRecognizer,
)
from jarvis.config.schema import AsrSection
from jarvis.core.exceptions import AsrError

if TYPE_CHECKING:  # pragma: no cover - typing only
    from jarvis.vad.types import SpeechSegment

_SENSEVOICE_TAG = re.compile(r"<\|[^|]*\|>")
"""SenseVoice rich-transcription control tokens (language / emotion / itn flags)."""


def _missing_dependency(package: str, engine: str, exc: ImportError) -> AsrError:
    return AsrError(
        f"the '{engine}' engine requires the optional '{package}' package "
        "(install with: pip install jarvis-assistant[voice])",
        details={"missing_package": package, "engine": engine},
    )


class SenseVoiceAsrEngine:
    """SenseVoice / FunASR adapter — offline, ONNX (default engine).

    Loads the model lazily inside ``__init__`` (FunASR pulls in ``torch``,
    so construction is the only heavy, import-bearing step). Expects 16 kHz
    mono s16le PCM — the pipeline standard shared with wake-word and VAD.
    """

    _SAMPLE_RATE = 16_000

    def __init__(self, section: AsrSection) -> None:
        try:
            import numpy
            import torch
            from funasr import AutoModel
        except ImportError as exc:
            raise _missing_dependency("funasr", "sensevoice", exc) from exc
        self._numpy = numpy
        self._torch = torch
        try:
            self._model = AutoModel(
                model=section.model,
                vad_model="fsmn-vad",
                # ct-punc is a second ~850 MB model. SenseVoice already emits
                # well-segmented text, so it stays opt-in via ``asr.punctuation``.
                punc_model="ct-punc" if section.punctuation else None,
                spk_model=None,
                disable_update=True,
                device=section.device,
            )
        except Exception as exc:
            raise AsrError(
                "failed to load the SenseVoice model",
                details={"model": section.model, "cause": repr(exc)},
            ) from exc
        self._language = section.language
        self._temperature = section.temperature
        self._beam_size = section.beam_size

    @property
    def name(self) -> str:
        return "sensevoice"

    @property
    def sample_rate(self) -> int:
        return self._SAMPLE_RATE

    def recognize(
        self,
        audio: bytes,
        *,
        segment: SpeechSegment | None = None,
        language: str | None = None,
    ) -> RecognitionResult:
        """Decode a complete PCM span into a final result."""
        if not audio:
            return RecognitionResult(
                text="",
                type=AsrResultType.FINAL,
                language=self._resolve_language(language),
                segment=segment,
            )
        # FunASR wants normalised float32 samples. Handing it the raw int16
        # array reaches a torch distribution op and dies with
        # ``NotImplementedError: 'normal_kernel_cpu' not implemented for 'Short'``.
        samples = (
            self._numpy.frombuffer(audio, dtype=self._numpy.int16).astype(self._numpy.float32)
            / 32768.0
        )
        lang = self._resolve_language(language)
        try:
            outputs = self._model.generate(
                input=samples,
                language=lang if lang != "auto" else "auto",
                batch_size_s=300,
                temperature=self._temperature,
                beam_size=self._beam_size,
            )
        except Exception as exc:
            raise AsrError(
                "sensevoice inference failed",
                details={"cause": repr(exc)},
            ) from exc
        text = self._extract_text(outputs)
        return RecognitionResult(
            text=text,
            type=AsrResultType.FINAL,
            language=lang if lang != "auto" else None,
            segment=segment,
        )

    def stream(self) -> AsrStream:
        """Open a streaming session (buffered; emits final on finish)."""
        return BufferedAsrStream(self)

    def close(self) -> None:
        # Release the torch model handle; recognition after close is invalid.
        self._model = None

    def _resolve_language(self, language: str | None) -> str:
        return language or self._language

    @classmethod
    def _extract_text(cls, outputs: list[dict[str, Any]]) -> str:
        if not outputs:
            return ""
        # SenseVoice generate returns [{"text": "...", "timestamp": ..., ...}]
        text = outputs[0].get("text", "")
        # The raw text is prefixed with rich-transcription control tokens
        # (``<|zh|><|ANGRY|><|Speech|>…``). Dropping them here keeps every
        # consumer — the LLM prompt above all — from inheriting the markup.
        return _SENSEVOICE_TAG.sub("", str(text)).strip()


class BufferedAsrStream:
    """Whole-utterance streaming wrapper.

    Buffers every pushed chunk and emits the single final
    :class:`RecognitionResult` on :meth:`finish`. ``push`` returns no partials
    because SenseVoice decodes the whole utterance at once; engines with
    native incremental support can subclass this and emit ``PARTIAL`` results
    from :meth:`push`.

    The accumulated PCM is exactly what was pushed (the pipeline already
    delivers 16 kHz mono s16le, so no resampling happens here).
    """

    def __init__(self, engine: SpeechRecognizer) -> None:
        self._engine = engine
        self._buffer = bytearray()

    def push(self, chunk: bytes) -> list[RecognitionResult]:
        self._buffer.extend(chunk)
        return []

    def finish(self) -> RecognitionResult:
        return self._engine.recognize(bytes(self._buffer))
