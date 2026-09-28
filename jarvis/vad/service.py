"""VAD listening service (lifecycle component).

Owns an on-demand microphone loop: a single daemon thread pulls PCM from an
:class:`~jarvis.audio.source.AudioSource`, feeds the
:class:`~jarvis.vad.segmenter.VoiceActivitySegmenter`, and invokes the
event callback for every detected speech start/end.

Design notes (mirrors phase 6's wake-word service):
    * Disabled by default (``vad.enabled: false``): grabbing the microphone
      is an explicit user choice.
    * If enabled, a missing dependency or unavailable microphone is a hard
      startup error — silently degrading an explicitly requested feature
      would hide real problems.
    * Factories for engine and audio source are injectable, so the full
      lifecycle is unit-testable without hardware or native packages.
    * On a clean stop the loop flushes any trailing (still-open) utterance
      so the consumer never loses the final segment.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass

from jarvis.audio.source import AudioSource, SounddeviceSource
from jarvis.config.schema import VadSection
from jarvis.core.exceptions import VadError
from jarvis.vad.engines import SileroVadEngine
from jarvis.vad.segmenter import VoiceActivitySegmenter
from jarvis.vad.types import VadEvent, VoiceActivityDetector

logger = logging.getLogger("jarvis.vad.service")

VadCallback = Callable[[VadEvent], None]
EngineFactory = Callable[[VadSection], VoiceActivityDetector]
SourceFactory = Callable[[], AudioSource]

_READ_CHUNK_SAMPLES = 512
"""Samples per blocking read — small for low latency, large enough that
per-call overhead stays negligible."""


@dataclass(frozen=True, slots=True)
class VadSettings:
    """Everything the service needs, resolved at start() time."""

    section: VadSection
    """Validated ``vad.*`` configuration."""

    sample_rate: int = 16_000
    """Capture rate; must match the engine (Silero = 16 kHz)."""


def default_engine_factory(section: VadSection) -> VoiceActivityDetector:
    """Build the engine selected by configuration."""
    if section.engine == "silero":
        return SileroVadEngine()
    raise VadError(  # pragma: no cover - schema already rejects this
        "unknown vad engine",
        details={"engine": section.engine},
    )


class VadService:
    """Lifecycle component running the VAD microphone loop."""

    name = "vad"

    def __init__(
        self,
        settings_provider: Callable[[], VadSettings],
        *,
        on_event: VadCallback | None = None,
        engine_factory: EngineFactory = default_engine_factory,
        source_factory: SourceFactory = SounddeviceSource,
    ) -> None:
        self._settings_provider = settings_provider
        self._on_event = on_event if on_event is not None else self._log_event
        self._engine_factory = engine_factory
        self._source_factory = source_factory
        self._engine: VoiceActivityDetector | None = None
        self._source: AudioSource | None = None
        self._segmenter: VoiceActivitySegmenter | None = None
        self._thread: threading.Thread | None = None
        self._stop_flag = threading.Event()

    @property
    def running(self) -> bool:
        """Whether the listening loop is currently alive."""
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        """Start listening (no-op when disabled by configuration)."""
        settings = self._settings_provider()
        if not settings.section.enabled:
            logger.info("vad disabled by configuration; not listening")
            return
        if self.running:  # pragma: no cover - Application never double-starts
            return

        engine = self._engine_factory(settings.section)
        segmenter = VoiceActivitySegmenter(
            engine,
            sample_rate=settings.sample_rate,
            threshold=settings.section.threshold,
            min_speech_ms=settings.section.min_speech_ms,
            max_silence_ms=settings.section.max_silence_ms,
            speech_pad_ms=settings.section.speech_pad_ms,
            max_speech_ms=settings.section.max_speech_ms,
        )
        source = self._source_factory()
        try:
            source.open()
        except Exception:
            engine.close()
            raise

        self._engine = engine
        self._segmenter = segmenter
        self._source = source
        self._stop_flag.clear()
        self._thread = threading.Thread(
            target=self._listen_loop,
            name="jarvis-vad",
            daemon=True,
        )
        self._thread.start()
        logger.info(
            "vad listening (engine=%s, threshold=%.2f)",
            engine.name,
            settings.section.threshold,
        )

    def stop(self) -> None:
        """Stop the loop and release the engine + microphone (idempotent)."""
        self._stop_flag.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=5.0)
        source, self._source = self._source, None
        if source is not None:
            source.close()
        engine, self._engine = self._engine, None
        if engine is not None:
            engine.close()
        self._segmenter = None

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _listen_loop(self) -> None:
        source = self._source
        segmenter = self._segmenter
        if source is None or segmenter is None:  # pragma: no cover - defensive
            return
        while not self._stop_flag.is_set():
            try:
                chunk = source.read(_READ_CHUNK_SAMPLES)
            except Exception:
                if self._stop_flag.is_set():
                    break  # normal teardown race: device closed under us
                logger.exception("audio capture failed; vad loop exiting")
                break
            for event in segmenter.feed(chunk):
                try:
                    self._on_event(event)
                except Exception:
                    logger.exception("vad event callback raised; event dropped")
        # Flush any utterance still open when we stop, so the consumer does
        # not lose the final segment.
        for event in segmenter.flush():
            try:
                self._on_event(event)
            except Exception:
                logger.exception("vad flush callback raised; event dropped")

    @staticmethod
    def _log_event(event: VadEvent) -> None:
        if event.type.value == "speech_start":
            logger.info("SPEECH START")
        elif event.segment is not None:
            logger.info(
                "SPEECH END: %d..%d samples (%.0f ms)",
                event.segment.start_sample,
                event.segment.end_sample,
                event.segment.duration_ms,
            )
        else:  # pragma: no cover - speech_end always carries a segment
            logger.info("SPEECH END")
