"""Audio output: drains synthesized chunks to a speaker, or somewhere else.

The pipeline produces :class:`~jarvis.tts.types.AudioChunk` objects; a player
decides what to do with them. :class:`NullAudioPlayer` records them (tests /
headless runs), :class:`SounddevicePlayer` writes PCM to PortAudio, and
:class:`BridgePlayer` hands the samples to a receiver chosen at runtime -- the
desktop HUD's page, which needs the bytes rather than just the sound. Engines are
responsible for handing over ``pcm_s16le`` (Edge-TTS decodes its own MP3), so a
chunk in another format is a bug and gets dropped loudly.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any, Protocol, runtime_checkable

from jarvis.tts.types import AudioChunk

logger = logging.getLogger("jarvis.orchestration.player")


@runtime_checkable
class AudioPlayer(Protocol):
    """Drains synthesized audio chunks to a speaker."""

    def play(self, chunk: AudioChunk) -> None:
        """Emit one chunk of audio."""
        ...

    def close(self) -> None:
        """Release the output device (safe to call twice)."""
        ...


class NullAudioPlayer:
    """Records every chunk instead of playing — tests / headless runs."""

    name = "null"

    def __init__(self) -> None:
        self.chunks: list[AudioChunk] = []

    def play(self, chunk: AudioChunk) -> None:
        self.chunks.append(chunk)

    def close(self) -> None:
        pass

    @property
    def total_bytes(self) -> int:
        """Total raw audio bytes received (handy in tests)."""
        return sum(len(chunk.audio) for chunk in self.chunks)


class SounddevicePlayer:
    """Best-effort speaker output via PortAudio (``sounddevice``)."""

    name = "sounddevice"

    def __init__(self, sample_rate: int | None = None) -> None:
        # ``None`` adopts each chunk's own rate. The TTS engines synthesize at
        # 24 kHz, so pinning a 16 kHz device here would pitch-shift everything.
        self._sample_rate = sample_rate
        self._stream: Any | None = None
        self._stream_rate = 0

    def play(self, chunk: AudioChunk) -> None:
        import sounddevice as sd

        if chunk.format != "pcm_s16le":
            logger.warning("skipping non-PCM chunk (format=%s)", chunk.format)
            return
        rate = self._sample_rate or chunk.sample_rate
        if self._stream is None or self._stream_rate != rate:
            self._teardown()
            self._stream = sd.RawOutputStream(samplerate=rate, channels=1, dtype="int16")
            self._stream.start()
            self._stream_rate = rate
        self._stream.write(chunk.audio)

    def _teardown(self) -> None:
        stream, self._stream = self._stream, None
        self._stream_rate = 0
        if stream is not None:
            try:
                stream.stop()
                stream.close()
            except Exception:  # pragma: no cover - device teardown must not raise
                pass

    def close(self) -> None:
        self._teardown()


class BridgePlayer:
    """Routes synthesized PCM to a receiver chosen at runtime, speaker as backup.

    The desktop HUD wants the samples *in the page* -- that is the only way the
    rhythm and the avatar's mouth can be read off the audio that is actually
    playing rather than invented from a state machine. But the page is a separate
    process that may be loading, reloaded, or simply unable to start an audio
    context, and an assistant whose answers are silently swallowed by a webview is
    a worse assistant than one whose visuals are flat.

    So ``emit`` is asked for permission on every chunk, and a ``False`` answer
    means the chunk goes out of the speaker instead. Nothing here decides policy:
    the receiver owns the "can I play right now" question (see
    :class:`jarvis.ui.audio_bridge.AudioPusher`, which the composition root injects
    as ``emit``).
    """

    name = "bridge"

    def __init__(
        self,
        emit: Callable[[bytes, int, bool], bool],
        *,
        fallback_factory: Callable[[], AudioPlayer] = SounddevicePlayer,
    ) -> None:
        self._emit = emit
        self._fallback_factory = fallback_factory
        self._fallback: AudioPlayer | None = None

    def play(self, chunk: AudioChunk) -> None:
        if chunk.format != "pcm_s16le":
            logger.warning("skipping non-PCM chunk (format=%s)", chunk.format)
            return
        if self._emit(chunk.audio, chunk.sample_rate, chunk.is_final):
            return
        self._speaker().play(chunk)

    def _speaker(self) -> AudioPlayer:
        """Build the fallback lazily: a page that works never pays for it."""
        if self._fallback is None:
            self._fallback = self._fallback_factory()
        return self._fallback

    def close(self) -> None:
        fallback, self._fallback = self._fallback, None
        if fallback is not None:
            try:
                fallback.close()
            except Exception:  # pragma: no cover - teardown must not raise
                logger.exception("audio fallback failed to close")
