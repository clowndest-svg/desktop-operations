"""ASR types: the recognizer protocol, streaming results, and the VAD→ASR slicer.

The voice pipeline standard is 16 kHz / mono / s16le (see :mod:`jarvis.audio`).
An ASR engine turns a span of that PCM into text. Two flavours are supported:

* **Whole-utterance**: hand the engine a finished segment's PCM, get one
  final :class:`RecognitionResult` back (``is_final is True``).
* **Streaming**: open an :class:`AsrStream`, push PCM chunks as they arrive,
  collect ``PARTIAL`` results for live display, and call ``finish`` to get the
  final ``FINAL`` result (handed to the LLM).

:class:`SpeechSegment` (from :mod:`jarvis.vad`) carries the exact sample range
VAD detected; :func:`slice_segment_audio` turns that range into the PCM bytes
an engine needs — this is the precise handoff contract between VAD and ASR.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, runtime_checkable

from jarvis.vad.types import SpeechSegment

# The whole pipeline speaks s16le, so every sample is exactly 2 bytes. Kept as
# a literal here (instead of importing AudioFormat) to honour the architecture
# rule that `asr` depends on `vad`, not directly on `audio`.
_SAMPLE_WIDTH = 2


class AsrResultType(StrEnum):
    """Whether a result is a live partial or the finalised transcription."""

    PARTIAL = "partial"
    """Intermediate hypotheses shown while the user is still speaking."""

    FINAL = "final"
    """Definitive transcription; safe to send to the LLM / agent."""


@dataclass(frozen=True, slots=True)
class RecognitionResult:
    """One transcription result off a recognizer or stream.

    A ``PARTIAL`` result carries the best-so-far hypothesis; a ``FINAL``
    result is the settled text. ``segment`` links the text back to the VAD
    span it came from (handy for logging and Barge-In correlation).
    """

    text: str
    """Transcribed text (may be empty for a silent / unrecognised span)."""

    type: AsrResultType
    """``PARTIAL`` or ``FINAL``."""

    language: str | None = None
    """Detected / forced language tag, e.g. ``zh`` / ``en`` (``None`` = auto)."""

    confidence: float | None = None
    """Optional 0..1 confidence from the engine (``None`` if unavailable)."""

    segment: SpeechSegment | None = None
    """The VAD span this text was decoded from, if known."""

    timestamp: float = 0.0
    """Wall-clock reading when the result was produced."""

    @property
    def is_final(self) -> bool:
        """``True`` when this is the settled transcription."""
        return self.type is AsrResultType.FINAL


@runtime_checkable
class SpeechRecognizer(Protocol):
    """PCM-in, text-out speech recognizer.

    Contract:
        * ``name`` identifies the engine (e.g. ``sensevoice``).
        * ``sample_rate`` is the rate the engine consumes (16 kHz here).
        * ``recognize(audio, ...)`` decodes a complete utterance and returns
          one ``FINAL`` :class:`RecognitionResult`.
        * ``stream()`` opens an :class:`AsrStream` for incremental decoding.
        * ``close()`` releases model resources (idempotent).
    """

    @property
    def name(self) -> str:
        """Engine identifier."""
        ...

    @property
    def sample_rate(self) -> int:
        """Sample rate the engine expects."""
        ...

    def recognize(
        self,
        audio: bytes,
        *,
        segment: SpeechSegment | None = None,
        language: str | None = None,
    ) -> RecognitionResult:
        """Decode a complete PCM span into a final result."""
        ...

    def stream(self) -> AsrStream:
        """Open an incremental-decoding session."""
        ...

    def close(self) -> None:
        """Release model resources (safe to call twice)."""
        ...


@runtime_checkable
class AsrStream(Protocol):
    """Incremental decoding session opened by :meth:`SpeechRecognizer.stream`."""

    def push(self, chunk: bytes) -> list[RecognitionResult]:
        """Feed more PCM; return any new ``PARTIAL`` results."""
        ...

    def finish(self) -> RecognitionResult:
        """End the utterance and return the ``FINAL`` result."""
        ...


def slice_segment_audio(
    audio: bytes,
    segment: SpeechSegment,
    *,
    sample_width: int = _SAMPLE_WIDTH,
) -> bytes:
    """Extract the raw PCM bytes for a detected speech span.

    ``segment.start_sample`` / ``segment.end_sample`` are frame indices into
    the 16 kHz mono s16le stream ``audio``. The slice is clamped to the actual
    buffer so a slightly-overflowing segment (rare, from padding) is never
    fatal, and a reversed range yields an empty buffer instead of an error.
    """
    start = segment.start_sample * sample_width
    end = segment.end_sample * sample_width
    if start < 0:
        start = 0
    if end > len(audio):
        end = len(audio)
    if start > end:
        return b""
    return audio[start:end]
