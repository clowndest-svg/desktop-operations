"""Speech recognition: streaming ASR (phase 8).

Responsibility (delivered in phase 8):
    * :class:`SpeechRecognizer` protocol with a SenseVoice / FunASR adapter
      (offline, ONNX, default).
    * :class:`RecognitionResult` — ``PARTIAL`` / ``FINAL`` streaming results,
      plus :class:`AsrStream` for incremental decoding.
    * :func:`slice_segment_audio` — the VAD -> ASR handoff: extract a
      detected speech span's PCM from the mic buffer.
    * :class:`AsrService` — lifecycle component owning the model; exposes
      ``recognize`` / ``transcribe_segment`` / ``stream`` for the
      VAD -> ASR wiring (orchestrated in a later phase).

Allowed dependencies: ``core``, ``config``, ``vad``.
"""

from jarvis.asr.engines import SenseVoiceAsrEngine
from jarvis.asr.service import AsrService, AsrSettings, default_engine_factory
from jarvis.asr.types import (
    AsrResultType,
    AsrStream,
    RecognitionResult,
    SpeechRecognizer,
    slice_segment_audio,
)

__all__ = [
    "AsrResultType",
    "AsrService",
    "AsrSettings",
    "AsrStream",
    "RecognitionResult",
    "SenseVoiceAsrEngine",
    "SpeechRecognizer",
    "default_engine_factory",
    "slice_segment_audio",
]
