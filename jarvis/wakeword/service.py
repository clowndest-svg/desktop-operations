"""Wake-word listening service (lifecycle component).

Owns the always-on microphone loop: a single daemon thread pulls PCM from
an :class:`AudioSource`, feeds the :class:`WakeWordDetector`, and invokes
the wake callback for every confirmed event. Later phases hook VAD/ASR
into that callback; in phase 6 the default callback logs the event.

Design notes:
    * Disabled by default (``wakeword.enabled: false``): a background
      service that grabs the microphone must be an explicit user choice.
    * If the user *did* enable it, a missing optional dependency or an
      unavailable microphone is a hard startup error — silently degrading
      an explicitly requested feature would hide real problems.
    * Factories for engine and audio source are injectable, so the full
      lifecycle is unit-testable without hardware or native packages.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass

from jarvis.audio.source import AudioSource, SounddeviceSource
from jarvis.config.schema import WakeWordSection
from jarvis.core.exceptions import WakeWordError
from jarvis.wakeword.detector import WakeWordDetector
from jarvis.wakeword.engines import OpenWakeWordEngine, PorcupineEngine
from jarvis.wakeword.types import WakeEvent, WakeWordEngine

logger = logging.getLogger("jarvis.wakeword.service")

WakeCallback = Callable[[WakeEvent], None]
EngineFactory = Callable[[WakeWordSection], WakeWordEngine]
SourceFactory = Callable[[], AudioSource]

_READ_CHUNK_SAMPLES = 512
"""Samples per blocking read — small enough for low latency, large enough
to keep per-call overhead negligible."""


@dataclass(frozen=True, slots=True)
class WakeWordSettings:
    """Everything the service needs, resolved at start() time."""

    section: WakeWordSection
    """Validated ``wakeword.*`` configuration."""

    environ: dict[str, str]
    """Process environment (Porcupine access-key lookup)."""


def default_engine_factory(settings: WakeWordSettings) -> WakeWordEngine:
    """Build the engine selected by configuration."""
    section = settings.section
    if section.engine == "openwakeword":
        return OpenWakeWordEngine(section.keywords)
    if section.engine == "porcupine":
        access_key = settings.environ.get(section.porcupine.access_key_env, "")
        return PorcupineEngine(
            section.keywords,
            access_key=access_key,
            sensitivity=section.porcupine.sensitivity,
        )
    if section.engine == "asr":
        # The transcript-matching engine borrows a loaded recognition model, and
        # this standalone loop deliberately owns no ASR — only the orchestrator,
        # which drives one shared microphone across every stage, can wire it.
        raise WakeWordError(
            "wakeword.engine 'asr' is only available with orchestration.enabled=true",
            details={
                "engine": "asr",
                "hint": "enable orchestration (plus asr.enabled and tts.enabled)",
            },
        )
    raise WakeWordError(  # pragma: no cover - schema already rejects this
        "unknown wake-word engine",
        details={"engine": section.engine},
    )


class WakeWordService:
    """Lifecycle component running the always-on wake-word loop."""

    name = "wakeword"

    def __init__(
        self,
        settings_provider: Callable[[], WakeWordSettings],
        *,
        on_wake: WakeCallback | None = None,
        engine_factory: Callable[[WakeWordSettings], WakeWordEngine] = default_engine_factory,
        source_factory: SourceFactory = SounddeviceSource,
    ) -> None:
        self._settings_provider = settings_provider
        self._on_wake = on_wake if on_wake is not None else self._log_wake
        self._engine_factory = engine_factory
        self._source_factory = source_factory
        self._engine: WakeWordEngine | None = None
        self._source: AudioSource | None = None
        self._detector: WakeWordDetector | None = None
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
            logger.info("wake word disabled by configuration; not listening")
            return
        if self.running:  # pragma: no cover - Application never double-starts
            return

        engine = self._engine_factory(settings)
        detector = WakeWordDetector(
            engine,
            threshold=settings.section.threshold,
            cooldown_seconds=settings.section.cooldown_seconds,
        )
        source = self._source_factory()
        try:
            source.open()
        except Exception:
            engine.close()
            raise

        self._engine = engine
        self._detector = detector
        self._source = source
        self._stop_flag.clear()
        self._thread = threading.Thread(
            target=self._listen_loop,
            name="jarvis-wakeword",
            daemon=True,
        )
        self._thread.start()
        logger.info(
            "wake word listening (engine=%s, keywords=%s, threshold=%.2f)",
            engine.name,
            ",".join(settings.section.keywords),
            settings.section.threshold,
        )

    def stop(self) -> None:
        """Stop the loop and release engine + microphone (idempotent)."""
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
        self._detector = None

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _listen_loop(self) -> None:
        source = self._source
        detector = self._detector
        if source is None or detector is None:  # pragma: no cover - defensive
            return
        while not self._stop_flag.is_set():
            try:
                chunk = source.read(_READ_CHUNK_SAMPLES)
            except Exception:
                if self._stop_flag.is_set():
                    break  # normal teardown race: device closed under us
                logger.exception("audio capture failed; wake word loop exiting")
                break
            for event in detector.feed(chunk):
                try:
                    self._on_wake(event)
                except Exception:
                    logger.exception("wake callback raised; event dropped")

    @staticmethod
    def _log_wake(event: WakeEvent) -> None:
        logger.info("WAKE: %s (score=%.3f)", event.keyword, event.score)
