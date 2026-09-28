"""TTS types: the synthesizer protocol and the produced audio chunk.

The voice pipeline standard is 16 kHz / mono / s16le for *capture*
(see :mod:`jarvis.audio`), but synthesis engines emit their own native
format — CosyVoice yields 24 kHz s16le PCM, Edge-TTS streams 24 kHz MP3.
To stay engine-agnostic, a synthesized sample is wrapped in
:class:`AudioChunk`, which carries the raw ``audio`` bytes together with
its ``sample_rate`` and ``format`` so a later playback layer can decode
and resample as needed.

:class:`SpeechSynthesizer` turns text into a *stream* of chunks. Yielding
chunks (rather than one big buffer) is what lets the caller play audio
**while it is still being produced** ("边合成边播") and lets a Barge-In
controller cancel mid-utterance via the ``should_stop`` predicate.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

# Descriptive format tags. The playback layer (a later phase) maps these to
# a decoder; we never hard-code engine specifics outside this module.
FORMAT_PCM_S16LE = "pcm_s16le"
FORMAT_MP3 = "mp3"
FORMAT_WAV = "wav"


@dataclass(frozen=True, slots=True)
class AudioChunk:
    """One piece of synthesized audio.

    A full utterance is produced as a sequence of chunks; the final chunk
    carries ``is_final = True`` so the consumer knows playback is complete.
    """

    audio: bytes
    """Raw encoded audio bytes (format given by ``format``)."""

    sample_rate: int
    """Sample rate of ``audio`` (engine-native, e.g. 24000)."""

    is_final: bool = False
    """``True`` on the last chunk of an utterance."""

    format: str = FORMAT_PCM_S16LE
    """Encoding of ``audio``: ``pcm_s16le`` / ``mp3`` / ``wav``."""


ShouldStop = Callable[[], bool]
"""Predicate polled between chunks; return ``True`` to cancel synthesis."""


@runtime_checkable
class SpeechSynthesizer(Protocol):
    """Text-in, audio-stream-out speech synthesizer.

    Contract:
        * ``name`` identifies the engine (e.g. ``cosyvoice``).
        * ``sample_rate`` is the rate the engine emits (24 kHz here).
        * ``synthesize(text, ...)`` is a generator yielding
          :class:`AudioChunk` objects as they are produced; the last chunk
          has ``is_final = True``.
        * ``close()`` releases model resources (idempotent).
    """

    @property
    def name(self) -> str:
        """Engine identifier."""
        ...

    @property
    def sample_rate(self) -> int:
        """Sample rate the engine emits."""
        ...

    def synthesize(
        self,
        text: str,
        *,
        voice: str | None = None,
        speed: float | None = None,
        volume: float | None = None,
        should_stop: ShouldStop | None = None,
    ) -> Iterator[AudioChunk]:
        """Yield audio chunks for ``text`` as they are produced.

        Args:
            text: The text to speak.
            voice: Engine-specific voice / speaker id; falls back to the
                engine's configured default when ``None``.
            speed: Playback speed multiplier (1.0 = normal); ``None`` uses
                the engine default.
            volume: Gain in [0, 1]; ``None`` uses the engine default.
            should_stop: When it returns ``True``, synthesis stops emitting
                further chunks (the Barge-In / interruption hook). The final
                chunk already yielded keeps ``is_final`` semantics.
        """
        ...

    def close(self) -> None:
        """Release model resources (safe to call twice)."""
        ...
