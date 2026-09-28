"""Speech synthesis: TTS (phase 9).

Responsibility (delivered in phase 9):
    * :class:`SpeechSynthesizer` protocol turning text into a stream of
      :class:`AudioChunk` (CosyVoice offline / Edge-TTS cloud adapters).
    * :class:`AudioChunk` — engine-agnostic audio (carries sample rate +
      format so a later playback layer can decode / resample).
    * :class:`TtsService` — lifecycle component owning the model; exposes a
      streaming ``synthesize`` generator (play-while-synthesize) plus a
      ``should_stop`` predicate hook reserved for Barge-In interruption.

Allowed dependencies: ``core``, ``config``.
"""

from jarvis.tts.engines import CosyVoiceTtsEngine, EdgeTtsEngine
from jarvis.tts.service import TtsService, TtsSettings, default_engine_factory
from jarvis.tts.types import (
    FORMAT_MP3,
    FORMAT_PCM_S16LE,
    FORMAT_WAV,
    AudioChunk,
    SpeechSynthesizer,
)

__all__ = [
    "FORMAT_MP3",
    "FORMAT_PCM_S16LE",
    "FORMAT_WAV",
    "AudioChunk",
    "CosyVoiceTtsEngine",
    "EdgeTtsEngine",
    "SpeechSynthesizer",
    "TtsService",
    "TtsSettings",
    "default_engine_factory",
]
