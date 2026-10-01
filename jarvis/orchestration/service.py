"""Orchestration service: the end-to-end voice assistant component.

This is the component that finally *connects* the pieces built in phases 6-9:
wake word -> VAD -> ASR -> multi-agent (LangGraph) -> TTS. It owns a single
microphone loop (via :class:`~jarvis.orchestration.voice_pipeline.VoicePipeline`)
and reuses the already-loaded :class:`~jarvis.asr.service.AsrService` /
:class:`~jarvis.tts.service.TtsService` models plus the LLM client.

When this service is enabled, the standalone wake-word / VAD loops must NOT be
started (they would fight over the same capture device); that decision lives
in the composition root (``jarvis.__main__``).
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from jarvis.asr.types import RecognitionResult
from jarvis.audio.source import AudioSource, SounddeviceSource
from jarvis.config.schema import OrchestrationSection, VadSection, WakeWordSection
from jarvis.core.exceptions import ConfigurationError
from jarvis.llm.client import LlmClient
from jarvis.orchestration.player import AudioPlayer, SounddevicePlayer
from jarvis.orchestration.types import PipelineEvent
from jarvis.orchestration.voice_pipeline import VoicePipeline, _GraphPort
from jarvis.tts.types import AudioChunk, ShouldStop
from jarvis.vad.segmenter import VoiceActivitySegmenter
from jarvis.vad.types import SpeechSegment, VoiceActivityDetector
from jarvis.wakeword.detector import WakeWordDetector
from jarvis.wakeword.types import WakeWordEngine

# Heavy engine / graph imports are deferred to ``start()`` so that merely
# *importing* this module (e.g. for tests or ``--help``) never pulls in
# ``langgraph`` / ``openwakeword`` / ``silero`` / ``torch``.


@runtime_checkable
class _AsrServicePort(Protocol):
    """What the orchestration service needs from an ASR provider."""

    @property
    def running(self) -> bool:
        """Whether the recognition model is loaded."""
        ...

    def recognize(
        self,
        audio: bytes,
        *,
        segment: SpeechSegment | None = None,
        language: str | None = None,
    ) -> RecognitionResult: ...


@runtime_checkable
class _TtsServicePort(Protocol):
    """What the orchestration service needs from a TTS provider."""

    @property
    def running(self) -> bool:
        """Whether the synthesis engine is loaded."""
        ...

    def synthesize(
        self,
        text: str,
        *,
        voice: str | None = None,
        speed: float | None = None,
        volume: float | None = None,
        should_stop: ShouldStop | None = None,
    ) -> Iterator[AudioChunk]: ...


logger = logging.getLogger("jarvis.orchestration.service")


@dataclass(frozen=True, slots=True)
class OrchestrationSettings:
    """Everything the service needs, resolved at start() time."""

    section: OrchestrationSection
    """Validated ``orchestration.*`` configuration."""

    wakeword: WakeWordSection
    """``wakeword.*`` — supplies keywords / threshold / cooldown."""

    vad: VadSection
    """``vad.*`` — supplies endpointing parameters."""

    sample_rate: int = 16_000
    """Capture rate shared with every engine (16 kHz)."""


def default_player_factory() -> AudioPlayer:
    """Build the default speaker (best-effort PortAudio output)."""
    return SounddevicePlayer()


class OrchestrationService:
    """Lifecycle component running the end-to-end voice pipeline."""

    name = "orchestration"

    def __init__(
        self,
        settings_provider: Callable[[], OrchestrationSettings],
        *,
        asr: _AsrServicePort,
        tts: _TtsServicePort,
        llm_client_provider: Callable[[], LlmClient],
        source_factory: Callable[[], AudioSource] = SounddeviceSource,
        player_factory: Callable[[], AudioPlayer] | None = None,
        wakeword_engine_factory: Callable[[WakeWordSection], WakeWordEngine] | None = None,
        vad_engine_factory: Callable[[], VoiceActivityDetector] | None = None,
        graph_factory: Callable[[], _GraphPort] | None = None,
        on_event: Callable[[PipelineEvent], None] | None = None,
        voice_provider: Callable[[], str | None] | None = None,
        transcript_sink: Callable[[str, str], None] | None = None,
    ) -> None:
        self._settings_provider = settings_provider
        self._asr = asr
        self._tts = tts
        self._llm_client_provider = llm_client_provider
        self._source_factory = source_factory
        self._player_factory = player_factory or default_player_factory
        self._wakeword_engine_factory = wakeword_engine_factory
        self._vad_engine_factory = vad_engine_factory
        self._graph_factory = graph_factory
        self._voice_provider = voice_provider
        self._transcript_sink = transcript_sink
        self._on_event = on_event
        self._pipeline: VoicePipeline | None = None

    @property
    def pipeline(self) -> VoicePipeline | None:
        """The live pipeline, or ``None`` before start / after stop.

        The desktop HUD reads this to answer "is the microphone actually mine right
        now" without owning the pipeline: a status light that lies about the
        microphone is worse than one that says it does not know.
        """
        return self._pipeline

    @property
    def listening(self) -> bool:
        """Whether a pipeline exists and its capture loop is alive."""
        pipeline = self._pipeline
        return pipeline is not None and pipeline.running

    def start(self) -> None:
        """Wire the full chain and start listening (no-op when disabled)."""
        settings = self._settings_provider()
        if not settings.section.enabled:
            logger.info("orchestration disabled; not starting voice pipeline")
            return
        if not self._asr.running:
            raise ConfigurationError(
                "orchestration requires asr.enabled=true (model not loaded)",
                details={"hint": "set asr.enabled=true and restart"},
            )
        if not self._tts.running:
            raise ConfigurationError(
                "orchestration requires tts.enabled=true (engine not loaded)",
                details={"hint": "set tts.enabled=true and restart"},
            )

        ww = settings.wakeword
        vad = settings.vad
        # Lazy heavy imports: only loaded when the service actually starts.
        from jarvis.vad.engines import SileroVadEngine
        from jarvis.wakeword.asr_engine import AsrWakeWordEngine
        from jarvis.wakeword.engines import OpenWakeWordEngine

        def build_segmenter() -> VoiceActivitySegmenter:
            """A fresh endpointer with its own VAD model instance.

            The wake engine and the listening stage each need one: Silero keeps
            streaming state inside the model, so a shared instance would smear
            the two detection passes together.
            """
            engine: VoiceActivityDetector = (
                self._vad_engine_factory()
                if self._vad_engine_factory is not None
                else SileroVadEngine()
            )
            return VoiceActivitySegmenter(
                engine,
                sample_rate=settings.sample_rate,
                threshold=vad.threshold,
                min_speech_ms=vad.min_speech_ms,
                max_silence_ms=vad.max_silence_ms,
                speech_pad_ms=vad.speech_pad_ms,
                max_speech_ms=vad.max_speech_ms,
            )

        if self._wakeword_engine_factory is not None:
            ww_engine = self._wakeword_engine_factory(ww)
        elif ww.engine == "asr":
            # Reuses the recognition model this service already requires, and
            # gets its own segmenter so resetting on a wake cannot disturb the
            # listening stage.
            ww_engine = AsrWakeWordEngine(
                keywords=ww.keywords,
                transcriber=self._asr,
                segmenter=build_segmenter(),
            )
        elif ww.engine == "openwakeword":
            ww_engine = OpenWakeWordEngine(ww.keywords)
        else:
            raise ConfigurationError(
                f"orchestration cannot build wake-word engine '{ww.engine}'",
                details={
                    "engine": ww.engine,
                    "hint": "use 'asr' or 'openwakeword', or inject wakeword_engine_factory",
                },
            )
        detector = WakeWordDetector(
            ww_engine,
            threshold=ww.threshold,
            cooldown_seconds=ww.cooldown_seconds,
        )
        segmenter = build_segmenter()
        if self._graph_factory is not None:
            graph = self._graph_factory()
        else:
            from jarvis.orchestration.graph import AgentGraph

            graph = AgentGraph(
                self._llm_client_provider(),
                default_agent=settings.section.default_agent,
            )
        player = self._player_factory()
        self._pipeline = VoicePipeline(
            detector=detector,
            segmenter=segmenter,
            asr=self._asr,
            graph=graph,
            tts=self._tts,
            source_factory=self._source_factory,
            player=player,
            barge_in=settings.section.barge_in,
            on_event=self._on_event,
            voice_provider=self._voice_provider,
            transcript_sink=self._transcript_sink,
        )
        self._pipeline.start()
        logger.info(
            "orchestration service started (barge_in=%s, default_agent=%s)",
            settings.section.barge_in,
            settings.section.default_agent,
        )

    def stop(self) -> None:
        """Stop the pipeline and release resources (idempotent)."""
        pipeline, self._pipeline = self._pipeline, None
        if pipeline is not None:
            pipeline.stop()
