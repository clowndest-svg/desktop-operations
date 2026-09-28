"""VAD types: the scorer protocol and the endpoint events.

The pipeline standard is 16 kHz / mono / s16le (see :mod:`jarvis.audio`).
A VAD engine is a *probabilistic scorer*: give it one fixed-size PCM frame,
get back a speech probability in ``[0, 1]``. All endpointing decisions
(start / end of an utterance, padding, min/max duration filtering) live in
:class:`jarvis.vad.segmenter.VoiceActivitySegmenter` — the engine stays
dumb, exactly like the wake-word engines stay dumb about debouncing.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, runtime_checkable

DEFAULT_SAMPLE_RATE: int = 16_000
"""The one true capture rate: every VAD engine consumes 16 kHz audio."""


class VadEventType(StrEnum):
    """Kind of endpoint event emitted by the segmenter."""

    SPEECH_START = "speech_start"
    """Speech onset detected (after the min-speech filter passed)."""

    SPEECH_END = "speech_end"
    """Utterance boundary detected; carries the finished segment."""


class VadState(StrEnum):
    """High-level detector state (handy for diagnostics / UI)."""

    SILENCE = "silence"
    SPEECH = "speech"


@dataclass(frozen=True, slots=True)
class SpeechSegment:
    """A detected span of speech, in both sample and wall-clock space.

    ``start_sample`` / ``end_sample`` are frame indices into the input
    stream (already padded by ``speech_pad_ms``). ``start_time`` /
    ``end_time`` are the injector clock readings captured at the
    corresponding frames.
    """

    start_sample: int
    end_sample: int
    start_time: float
    end_time: float
    sample_rate: int

    @property
    def duration_samples(self) -> int:
        """Number of samples spanned (inclusive of padding)."""
        return self.end_sample - self.start_sample

    @property
    def duration_ms(self) -> float:
        """Span duration in milliseconds."""
        return self.duration_samples * 1000.0 / self.sample_rate


@dataclass(frozen=True, slots=True)
class VadEvent:
    """One endpoint event off the segmenter.

    ``SPEECH_START`` carries no segment yet (``segment is None``);
    ``SPEECH_END`` carries the completed :class:`SpeechSegment`.
    """

    type: VadEventType
    segment: SpeechSegment | None = None
    timestamp: float = 0.0


@runtime_checkable
class VoiceActivityDetector(Protocol):
    """Frame-in, speech-probability-out scorer.

    Contract:
        * ``frame_samples`` is the exact number of 16 kHz mono s16le samples
          ``process()`` expects per call (engine-specific: Silero = 512).
        * ``process(frame)`` returns a speech probability in ``[0, 1]``.
          Endpointing (thresholds, padding, utterance boundaries) is NOT the
          engine's job — :class:`jarvis.vad.segmenter.VoiceActivitySegmenter`
          owns that, exactly as the wake-word detector owns debouncing.
        * ``close()`` releases native resources (idempotent).
    """

    @property
    def name(self) -> str:
        """Engine identifier, e.g. ``silero``."""
        ...

    @property
    def frame_samples(self) -> int:
        """Exact samples per ``process()`` call."""
        ...

    def process(self, frame: bytes) -> float:
        """Score one PCM frame; return speech probability in [0, 1]."""
        ...

    def close(self) -> None:
        """Release engine resources (safe to call twice)."""
        ...
