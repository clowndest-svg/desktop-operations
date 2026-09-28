"""Wake-word engine protocol and event types."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True, slots=True)
class WakeHit:
    """A single raw engine hit for one frame (pre-debounce)."""

    keyword: str
    """Which keyword/model fired (e.g. ``jarvis``)."""

    score: float
    """Engine confidence in [0, 1]. Engines with built-in thresholding
    (Porcupine) report 1.0 on a hit."""

    command: str = ""
    """Text spoken after the keyword, when the engine knows it.

    Frame-scoring engines (OpenWakeWord, Porcupine) cannot know what follows the
    wake word, so they leave this empty. The transcript-matching engine fills it
    in, letting the pipeline act on "贾维斯, 帮我看下订单" as a single breath.
    """


@dataclass(frozen=True, slots=True)
class WakeEvent:
    """A debounced, confirmed wake-up (what downstream consumers receive)."""

    keyword: str
    """Which keyword woke the assistant."""

    score: float
    """Confidence of the winning frame."""

    timestamp: float
    """Monotonic time (``time.monotonic()``) of the detection."""

    command: str = ""
    """See :attr:`WakeHit.command`. Carried through the debouncing untouched."""


@runtime_checkable
class WakeWordEngine(Protocol):
    """Frame-in, hits-out keyword spotter.

    Contract:
        * ``frame_samples`` is the exact number of 16 kHz mono s16le samples
          ``process()`` expects per call (engine-specific: OpenWakeWord 1280,
          Porcupine 512).
        * ``process(frame)`` scores one frame and returns raw hits (empty
          tuple almost always). Thresholding/debouncing is NOT the engine's
          job — :class:`jarvis.wakeword.detector.WakeWordDetector` owns that.
        * ``close()`` releases native resources (idempotent).
    """

    @property
    def name(self) -> str:
        """Engine identifier, e.g. ``openwakeword``."""
        ...

    @property
    def frame_samples(self) -> int:
        """Exact samples per ``process()`` call."""
        ...

    def process(self, frame: bytes) -> tuple[WakeHit, ...]:
        """Score one PCM frame; return raw (unthresholded) hits."""
        ...

    def close(self) -> None:
        """Release engine resources (safe to call twice)."""
        ...
