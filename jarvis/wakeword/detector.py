"""Threshold + cooldown debouncing on top of a raw wake-word engine.

Engines score every frame; a spoken "Jarvis" therefore produces a *burst*
of consecutive high-score frames. Without debouncing one utterance would
fire dozens of wake events. The detector applies:

* a confidence ``threshold`` (raw hits below it are ignored), and
* a ``cooldown``: after a confirmed event, further hits are suppressed for
  a configurable number of seconds.

The clock is injectable so tests control time explicitly.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable

from jarvis.audio.frames import FrameAssembler
from jarvis.wakeword.types import WakeEvent, WakeWordEngine

logger = logging.getLogger("jarvis.wakeword.detector")


class WakeWordDetector:
    """Feed PCM chunks in, get debounced :class:`WakeEvent` s out.

    Combines a :class:`FrameAssembler` (arbitrary chunks -> exact engine
    frames) with threshold/cooldown filtering. One detector wraps one
    engine instance.
    """

    def __init__(
        self,
        engine: WakeWordEngine,
        *,
        threshold: float,
        cooldown_seconds: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not 0.0 <= threshold <= 1.0:
            raise ValueError(f"threshold must be within [0, 1], got {threshold}")
        if cooldown_seconds < 0:
            raise ValueError(f"cooldown_seconds must be >= 0, got {cooldown_seconds}")
        self._engine = engine
        self._threshold = threshold
        self._cooldown = cooldown_seconds
        self._clock = clock
        self._assembler = FrameAssembler(engine.frame_samples * 2)  # s16le = 2 bytes
        self._last_event_at: float | None = None

    @property
    def engine(self) -> WakeWordEngine:
        """The wrapped engine (exposed for diagnostics/UI)."""
        return self._engine

    def feed(self, chunk: bytes) -> list[WakeEvent]:
        """Process an arbitrary-size PCM chunk; return confirmed events."""
        events: list[WakeEvent] = []
        for frame in self._assembler.push(chunk):
            for hit in self._engine.process(frame):
                if hit.score < self._threshold:
                    continue
                now = self._clock()
                if self._last_event_at is not None and now - self._last_event_at < self._cooldown:
                    logger.debug(
                        "wake hit suppressed by cooldown: %s (score=%.3f)",
                        hit.keyword,
                        hit.score,
                    )
                    continue
                self._last_event_at = now
                events.append(
                    WakeEvent(
                        keyword=hit.keyword,
                        score=hit.score,
                        timestamp=now,
                        command=hit.command,
                    )
                )
                logger.info("wake word detected: %s (score=%.3f)", hit.keyword, hit.score)
        return events

    def reset(self) -> None:
        """Clear buffered audio and the cooldown window (stream restart)."""
        self._assembler.reset()
        self._last_event_at = None
