"""Voice activity detection: streaming speech endpointing.

Responsibility (delivered in phase 7):
    * :class:`VoiceActivityDetector` protocol with a Silero VAD adapter
      (offline ONNX, default).
    * :class:`VoiceActivitySegmenter` — streaming endpoint state machine
      (speech start / end, padding, min-speech + max-silence filtering,
      max-speech hard cap). Fully testable with a fake scorer.
    * :class:`VadService` — lifecycle component owning the microphone loop;
      emits :class:`VadEvent` (with a :class:`SpeechSegment` on speech end)
      to the event callback.

Allowed dependencies: ``core``, ``config``, ``audio``.
"""

from jarvis.vad.engines import SileroVadEngine
from jarvis.vad.segmenter import VoiceActivitySegmenter
from jarvis.vad.service import VadService, VadSettings, default_engine_factory
from jarvis.vad.types import (
    SpeechSegment,
    VadEvent,
    VadEventType,
    VadState,
    VoiceActivityDetector,
)

__all__ = [
    "SileroVadEngine",
    "SpeechSegment",
    "VadEvent",
    "VadEventType",
    "VadService",
    "VadSettings",
    "VadState",
    "VoiceActivityDetector",
    "VoiceActivitySegmenter",
    "default_engine_factory",
]
