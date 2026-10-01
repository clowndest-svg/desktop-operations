"""TTS synthesis service (lifecycle component).

Owns the (heavy) synthesis model: :meth:`start` constructs the configured
engine — which lazily loads the model — and :meth:`stop` releases it. The
service is *passive*: it exposes :meth:`synthesize` (a generator yielding
:class:`~jarvis.tts.types.AudioChunk` as audio is produced) and
:meth:`synthesize_to_bytes` for callers that prefer a single buffer. The
orchestration layer (later phases) drives playback and, during playback,
polls VAD's ``SPEECH_START`` to cancel an in-flight utterance (Barge-In) via
the ``should_stop`` predicate.

Design notes (mirrors phase 8's ASR service):
    * Disabled by default (``tts.enabled: false``): loading a model / opening
      a cloud session is an explicit, opt-in choice.
    * If enabled, a missing dependency or model-load failure is a hard error
      at :meth:`start` — silently degrading an explicitly requested feature
      would hide real problems.
    * The engine factory is injectable, so the full lifecycle (load / synthesize
      / cancel / release) is unit-testable without hardware or native packages.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
from dataclasses import dataclass

from jarvis.config.schema import TtsSection
from jarvis.core.exceptions import TtsError
from jarvis.core.text import speakable
from jarvis.tts.engines import CosyVoiceTtsEngine, EdgeTtsEngine
from jarvis.tts.types import AudioChunk, ShouldStop, SpeechSynthesizer

logger = logging.getLogger("jarvis.tts.service")

EngineFactory = Callable[[TtsSection], SpeechSynthesizer]


@dataclass(frozen=True, slots=True)
class TtsSettings:
    """Everything the service needs, resolved at ``start()`` time."""

    section: TtsSection
    """Validated ``tts.*`` configuration."""


def default_engine_factory(section: TtsSection) -> SpeechSynthesizer:
    """Build the engine selected by configuration."""
    if section.engine == "cosyvoice":
        return CosyVoiceTtsEngine(section)
    if section.engine == "edge_tts":
        return EdgeTtsEngine(section)
    raise TtsError(  # pragma: no cover - schema already rejects this
        "unknown tts engine",
        details={"engine": section.engine},
    )


class TtsService:
    """Lifecycle component owning the synthesis engine."""

    name = "tts"

    def __init__(
        self,
        settings_provider: Callable[[], TtsSettings],
        *,
        engine_factory: EngineFactory = default_engine_factory,
    ) -> None:
        self._settings_provider = settings_provider
        self._engine_factory = engine_factory
        self._engine: SpeechSynthesizer | None = None

    @property
    def running(self) -> bool:
        """Whether the synthesis engine is currently loaded."""
        return self._engine is not None

    @property
    def sample_rate(self) -> int:
        """Native sample rate of the active engine (16 kHz before load)."""
        return self._engine.sample_rate if self._engine is not None else 16_000

    def start(self) -> None:
        """Load the model / open the session (no-op when disabled)."""
        settings = self._settings_provider()
        if not settings.section.enabled:
            logger.info("tts disabled by configuration; engine not loaded")
            return
        if self.running:  # pragma: no cover - Application never double-starts
            return
        # Constructing the engine lazily imports + loads the model / session.
        engine = self._engine_factory(settings.section)
        self._engine = engine
        logger.info("tts engine loaded (engine=%s)", engine.name)

    def stop(self) -> None:
        """Release the model / session (idempotent)."""
        engine, self._engine = self._engine, None
        if engine is not None:
            try:
                engine.close()
            except Exception:  # pragma: no cover - defensive
                logger.exception("tts engine close failed")

    # ------------------------------------------------------------------
    # Synthesis API
    # ------------------------------------------------------------------

    def synthesize(
        self,
        text: str,
        *,
        voice: str | None = None,
        speed: float | None = None,
        volume: float | None = None,
        should_stop: ShouldStop | None = None,
    ) -> Iterator[AudioChunk]:
        """Yield synthesized audio chunks for ``text`` as they are produced.

        This is the "边合成边播" entry point: the caller plays each chunk as
        it arrives. Pass ``should_stop`` (e.g. wired to VAD ``SPEECH_START``)
        to cancel mid-utterance for Barge-In.

        The text is reduced to what a voice can say (:func:`speakable`) here and
        nowhere else: this is the one waist every utterance passes, and the caller's
        string is also what lands in the transcript and on screen, where the
        punctuation belongs. A reply that was only ever going to be silence
        (emoji, a code block) yields nothing rather than a breath.
        """
        engine = self._require_engine()
        spoken = speakable(text)
        if not spoken:
            logger.debug("nothing speakable in a %d-character reply; staying silent", len(text))
            return
        yield from engine.synthesize(
            spoken,
            voice=voice,
            speed=speed,
            volume=volume,
            should_stop=should_stop,
        )

    def synthesize_to_bytes(
        self,
        text: str,
        *,
        voice: str | None = None,
        speed: float | None = None,
        volume: float | None = None,
        should_stop: ShouldStop | None = None,
    ) -> tuple[bytes, str, int]:
        """Convenience wrapper: collect all chunks into one buffer.

        Returns ``(audio, format, sample_rate)`` so callers that do not want
        to stream can still play the result after synthesis completes.
        """
        chunks = list(
            self.synthesize(
                text,
                voice=voice,
                speed=speed,
                volume=volume,
                should_stop=should_stop,
            )
        )
        if not chunks:
            return b"", "pcm_s16le", self.sample_rate
        fmt = chunks[0].format
        rate = chunks[0].sample_rate
        return b"".join(chunk.audio for chunk in chunks), fmt, rate

    def _require_engine(self) -> SpeechSynthesizer:
        if self._engine is None:
            raise TtsError("tts engine is not loaded; call start() first")
        return self._engine
