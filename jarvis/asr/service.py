"""ASR recognition service (lifecycle component).

Owns the (heavy) recognition model: :meth:`start` constructs the configured
engine — which lazily loads the model — and :meth:`stop` releases it. The
service is *passive*: it does not grab the microphone itself (VAD owns the
capture loop and emits :class:`~jarvis.vad.types.SpeechSegment` spans). The
orchestration layer (later phases) wires VAD's ``SPEECH_END`` events to
:meth:`transcribe_segment`, handing ASR the full mic buffer plus the detected
span to decode one utterance.

Design notes (mirrors phases 6/7 voice services):
    * Disabled by default (``asr.enabled: false``): loading the model is an
      explicit, opt-in choice — it is heavy and pulls native ML deps.
    * If enabled, a missing dependency or model-load failure is a hard error
      at :meth:`start` — silently degrading an explicitly requested feature
      would hide real problems.
    * The engine factory is injectable, so the full lifecycle (load / recognize
      / transcribe / stream / release) is unit-testable without hardware or
      native packages.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass

from jarvis.asr.engines import SenseVoiceAsrEngine
from jarvis.asr.types import (
    AsrStream,
    RecognitionResult,
    SpeechRecognizer,
    slice_segment_audio,
)
from jarvis.config.schema import AsrSection
from jarvis.core.exceptions import AsrError
from jarvis.vad.types import SpeechSegment

logger = logging.getLogger("jarvis.asr.service")

AsrCallback = Callable[[RecognitionResult], None]
EngineFactory = Callable[[AsrSection], SpeechRecognizer]


@dataclass(frozen=True, slots=True)
class AsrSettings:
    """Everything the service needs, resolved at start() time."""

    section: AsrSection
    """Validated ``asr.*`` configuration."""

    sample_rate: int = 16_000
    """Capture rate; must match the engine (16 kHz)."""


def default_engine_factory(section: AsrSection) -> SpeechRecognizer:
    """Build the engine selected by configuration."""
    if section.engine == "sensevoice":
        return SenseVoiceAsrEngine(section)
    raise AsrError(  # pragma: no cover - schema already rejects this
        "unknown asr engine",
        details={"engine": section.engine},
    )


class AsrService:
    """Lifecycle component owning the recognition model."""

    name = "asr"

    def __init__(
        self,
        settings_provider: Callable[[], AsrSettings],
        *,
        engine_factory: EngineFactory = default_engine_factory,
    ) -> None:
        self._settings_provider = settings_provider
        self._engine_factory = engine_factory
        self._engine: SpeechRecognizer | None = None

    @property
    def running(self) -> bool:
        """Whether the recognition model is currently loaded."""
        return self._engine is not None

    def start(self) -> None:
        """Load the model (no-op when disabled by configuration)."""
        settings = self._settings_provider()
        if not settings.section.enabled:
            logger.info("asr disabled by configuration; model not loaded")
            return
        if self.running:  # pragma: no cover - Application never double-starts
            return
        # Constructing the engine lazily imports + loads the model.
        engine = self._engine_factory(settings.section)
        self._engine = engine
        logger.info("asr model loaded (engine=%s)", engine.name)

    def stop(self) -> None:
        """Release the model (idempotent)."""
        engine, self._engine = self._engine, None
        if engine is not None:
            try:
                engine.close()
            except Exception:  # pragma: no cover - defensive
                logger.exception("asr engine close failed")

    # ------------------------------------------------------------------
    # Recognition API (called by the VAD -> ASR wiring in a later phase)
    # ------------------------------------------------------------------

    def recognize(
        self,
        audio: bytes,
        *,
        segment: SpeechSegment | None = None,
        language: str | None = None,
    ) -> RecognitionResult:
        """Decode a complete PCM span into a final result."""
        engine = self._require_engine()
        return engine.recognize(audio, segment=segment, language=language)

    def transcribe_segment(
        self,
        audio: bytes,
        segment: SpeechSegment,
    ) -> RecognitionResult:
        """Slice ``audio`` by ``segment`` and recognize the spoken span.

        This is the exact handoff contract the VAD -> ASR pipeline uses:
        feed the full mic buffer plus the :class:`SpeechSegment` VAD emitted
        on ``SPEECH_END``, get back one transcribed utterance.
        """
        engine = self._require_engine()
        clipped = slice_segment_audio(audio, segment)
        return engine.recognize(clipped, segment=segment)

    def stream(self) -> AsrStream:
        """Open an incremental-decoding session."""
        engine = self._require_engine()
        return engine.stream()

    def _require_engine(self) -> SpeechRecognizer:
        if self._engine is None:
            raise AsrError("asr engine is not loaded; call start() first")
        return self._engine
