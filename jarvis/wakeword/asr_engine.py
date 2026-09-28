"""Chinese wake-word detection by transcript matching, reusing the ASR model.

OpenWakeWord's shipped keywords are English-only, and training a Chinese
classifier needs many speaker-varied positives plus hours of negative audio to
measure false-positive rates — neither of which can be produced and *verified*
offline. This engine takes the other route: it segments speech with the same
VAD the listening stage uses, transcribes each utterance with the already-loaded
SenseVoice model, and wakes when a configured keyword appears in the text.

Consequences worth knowing:

* **No extra model, no extra process.** It borrows :class:`~jarvis.asr.AsrService`
  that the orchestrator already started, so recognition stays offline.
* **Every utterance costs a recognition pass** (~1.5 s CPU for an 8 s phrase).
  Silence is free — only VAD frames run while nobody talks.
* **Wake and command can share one breath.** Since the whole utterance is
  transcribed anyway, whatever follows the keyword rides along on
  :attr:`~jarvis.wakeword.types.WakeHit.command`, so "贾维斯, 帮我看下订单"
  is handled as a single turn instead of making the user speak twice.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from jarvis.core.exceptions import AsrError
from jarvis.wakeword.types import WakeHit

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Sequence

logger = logging.getLogger("jarvis.wakeword.asr_engine")

_FRAME_SAMPLES = 512
"""Mic framing for this engine. Small for low latency; the injected segmenter
reframes internally, so this value does not have to match its own frame size."""

_SAMPLE_RATE = 16_000
_BYTES_PER_SAMPLE = 2

_WINDOW_SECONDS = 30.0
"""How much recent audio to keep. Utterances longer than this are untranscribable,
and `vad.max_speech_ms` should stay well under it."""


@runtime_checkable
class WakeTranscriber(Protocol):
    """The recognition surface this engine needs — :class:`AsrService` satisfies it."""

    def recognize(
        self, audio: bytes, *, segment: Any | None = None, language: Any | None = None
    ) -> Any:
        """Transcribe raw 16 kHz mono s16le PCM."""
        ...


@runtime_checkable
class WakeSegmenter(Protocol):
    """The endpointing surface this engine needs — ``VoiceActivitySegmenter`` satisfies it."""

    def feed(self, chunk: bytes) -> Sequence[Any]:
        """Consume PCM bytes and return any new events."""
        ...

    def flush(self) -> Sequence[Any]:
        """Close off the current utterance and return any trailing event."""
        ...

    def reset(self) -> None:
        """Drop all buffered audio and temporal state."""
        ...


_MATCH_STRIP = re.compile(r"[^0-9a-z\u4e00-\u9fff]+")
"""Everything a transcript and a keyword must agree to ignore: spacing, case,
punctuation, and the ``_``/``-`` of model-name-style keywords."""


def normalize_for_match(text: str) -> str:
    """Fold text so a configured keyword matches what the recogniser produced.

    ``hey_jarvis`` has to match a transcript reading ``Hey Jarvis``, and
    ``贾维斯`` has to match the spaced-out, comma-punctuated form a recogniser
    may emit. Without this, pointing ``wakeword.engine`` at ``asr`` with the
    shipped default keyword would simply never wake while everything kept
    reporting success.
    """
    return _MATCH_STRIP.sub("", text.casefold())


_SEPARATORS = r"[\s，,。.、；;：:！!？?]*"
"""What ASR may leave between the characters of a spoken keyword."""


def _command_after_keyword(raw: str, normalized_keyword: str) -> str:
    """Return what was said *after* the wake keyword, as readable text.

    Matching happens character-by-character against the raw transcript: only the
    folded form is guaranteed to have no separators, but the command handed to
    the model should still read like language, not like ``normalize_for_match``
    output.
    """
    pattern = _SEPARATORS.join(re.escape(char) for char in normalized_keyword)
    match = re.search(pattern, raw, re.IGNORECASE)
    if match is None:
        return ""
    return re.sub(f"^{_SEPARATORS}", "", raw[match.end() :]).strip()


class AsrWakeWordEngine:
    """Wake on any configured keyword appearing in a transcribed utterance."""

    name = "asr"
    frame_samples = _FRAME_SAMPLES

    def __init__(
        self,
        *,
        keywords: Sequence[str],
        transcriber: WakeTranscriber,
        segmenter: WakeSegmenter,
    ) -> None:
        pairs: list[tuple[str, str]] = []
        for item in keywords:
            normalized = normalize_for_match(item)
            if normalized:
                pairs.append((normalized, item))
        if not pairs:
            raise AsrError(
                "the 'asr' wake-word engine needs at least one non-empty keyword",
                details={"keywords": list(keywords)},
            )
        # Match on the folded form, report the configured one — a wake event
        # naming ``heyjarvis`` instead of ``hey_jarvis`` is just confusing.
        self._keywords = pairs
        self._transcriber = transcriber
        self._segmenter = segmenter
        self._window_bytes = int(_WINDOW_SECONDS * _SAMPLE_RATE * _BYTES_PER_SAMPLE)
        self._audio = bytearray()
        self._audio_start_sample = 0

    def process(self, frame: bytes) -> tuple[WakeHit, ...]:
        """Score one mic frame; returns a hit when an utterance names a keyword."""
        self._audio.extend(frame)
        self._trim()

        for event in self._segmenter.feed(frame):
            segment = getattr(event, "segment", None)
            if segment is None:
                continue
            matched = self._match(segment)
            if matched is not None:
                keyword, command = matched
                self._restart_window()
                return (WakeHit(keyword=keyword, score=1.0, command=command),)
        return ()

    def close(self) -> None:
        """Drop buffered audio. The transcriber and segmenter are not ours to free."""
        self._segmenter.reset()
        self._audio.clear()
        self._audio_start_sample = 0

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _trim(self) -> None:
        """Keep only the last window of audio, tracking where it starts."""
        overflow = len(self._audio) - self._window_bytes
        if overflow <= 0:
            return
        # Drop whole frames so ``_audio_start_sample`` stays sample-aligned.
        drop = min(overflow, len(self._audio))
        del self._audio[:drop]
        self._audio_start_sample += drop // _BYTES_PER_SAMPLE

    def _match(self, segment: Any) -> tuple[str, str] | None:
        """Return ``(keyword, command)`` when ``segment`` names a keyword."""
        audio = self._clip(segment)
        if not audio:
            return None
        try:
            result = self._transcriber.recognize(audio)
        except AsrError as exc:
            # A recognition failure must not wedge the loop; report and keep going.
            logger.warning("wake-word recognition failed; ignoring utterance: %s", exc)
            return None
        raw = str(getattr(result, "text", ""))
        text = normalize_for_match(raw)
        for normalized, configured in self._keywords:
            if normalized in text:
                command = _command_after_keyword(raw, normalized)
                logger.info("wake keyword %r heard in %r (command=%r)", configured, text, command)
                return configured, command
        logger.debug("no wake keyword in %r", text)
        return None

    def _clip(self, segment: Any) -> bytes:
        """Slice the segment out of the audio window, clamped to what we hold."""
        end = min(int(segment.end_sample), self._consumed_samples)
        start = max(int(segment.start_sample), self._audio_start_sample)
        if end <= start:
            return b""
        lo = (start - self._audio_start_sample) * _BYTES_PER_SAMPLE
        hi = (end - self._audio_start_sample) * _BYTES_PER_SAMPLE
        return bytes(self._audio[lo:hi])

    def _restart_window(self) -> None:
        """Forget everything before now, so a wake phrase is not re-transcribed."""
        self._segmenter.reset()
        self._audio.clear()
        self._audio_start_sample = 0

    @property
    def _consumed_samples(self) -> int:
        return self._audio_start_sample + len(self._audio) // _BYTES_PER_SAMPLE
