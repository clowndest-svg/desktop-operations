"""Streaming voice-activity endpointing on top of a raw VAD scorer.

A :class:`~jarvis.vad.types.VoiceActivityDetector` only scores frames. This
class turns that score stream into utterance boundaries:

    SILENCE --(prob >= threshold, sustained min_speech)--> SPEECH_START
    SPEECH  --(prob <  threshold, sustained max_silence)-> SPEECH_END
                                              (or max_speech hard cap)

Key behaviours:

* **Min-speech filter** — a hit shorter than ``min_speech_ms`` is treated as
  a blip (a click, a breath) and discarded *before* any SPEECH_START is
  emitted, so consumers never see an orphan start without an end.
* **Padding** — ``speech_pad_ms`` extends the segment a little past the
  detected edges so ASR gets clean attack/decay, not a chopped phoneme.
* **Max-speech cap** — a single utterance longer than ``max_speech_ms`` is
  force-cut into segments (``0`` disables the cap).
* **Injectable clock** — tests drive time deterministically.

The assembler that turns arbitrary capture chunks into exact frames is the
shared :class:`jarvis.audio.frames.FrameAssembler` from phase 6.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable

from jarvis.audio.frames import FrameAssembler
from jarvis.vad.types import (
    DEFAULT_SAMPLE_RATE,
    SpeechSegment,
    VadEvent,
    VadEventType,
    VadState,
    VoiceActivityDetector,
)

logger = logging.getLogger("jarvis.vad.segmenter")


class VoiceActivitySegmenter:
    """Feed PCM chunks in, get :class:`VadEvent` s (speech start/end) out.

    Wraps one scorer instance; the segmenter owns all temporal state, so one
    segmenter = one continuous listening session.
    """

    def __init__(
        self,
        engine: VoiceActivityDetector,
        *,
        sample_rate: int = DEFAULT_SAMPLE_RATE,
        threshold: float = 0.5,
        min_speech_ms: int = 250,
        max_silence_ms: int = 500,
        speech_pad_ms: int = 100,
        max_speech_ms: int = 0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not 0.0 <= threshold <= 1.0:
            raise ValueError(f"threshold must be within [0, 1], got {threshold}")
        if min_speech_ms < 0:
            raise ValueError(f"min_speech_ms must be >= 0, got {min_speech_ms}")
        if max_silence_ms < 1:
            raise ValueError(f"max_silence_ms must be >= 1, got {max_silence_ms}")
        if speech_pad_ms < 0:
            raise ValueError(f"speech_pad_ms must be >= 0, got {speech_pad_ms}")
        if max_speech_ms < 0:
            raise ValueError(f"max_speech_ms must be >= 0, got {max_speech_ms}")
        if sample_rate <= 0:
            raise ValueError(f"sample_rate must be positive, got {sample_rate}")

        self._engine = engine
        self._sample_rate = sample_rate
        self._threshold = threshold
        self._min_speech_samples = int(min_speech_ms * sample_rate / 1000)
        self._max_silence_samples = int(max_silence_ms * sample_rate / 1000)
        self._pad_samples = int(speech_pad_ms * sample_rate / 1000)
        self._max_speech_samples = (
            int(max_speech_ms * sample_rate / 1000) if max_speech_ms > 0 else 0
        )
        self._clock = clock
        self._frame_samples = engine.frame_samples
        self._assembler = FrameAssembler(self._frame_samples * 2)  # s16le = 2 bytes
        self._total_samples = 0
        self._reset_state()

    @property
    def engine(self) -> VoiceActivityDetector:
        """The wrapped scorer (exposed for diagnostics / UI)."""
        return self._engine

    @property
    def state(self) -> VadState:
        """Current high-level state."""
        return VadState.SPEECH if self._in_speech else VadState.SILENCE

    def feed(self, chunk: bytes) -> list[VadEvent]:
        """Process an arbitrary-size PCM chunk; return any new events."""
        events: list[VadEvent] = []
        for frame in self._assembler.push(chunk):
            prob = self._engine.process(frame)
            events.extend(self._consume_frame(prob))
        return events

    def flush(self) -> list[VadEvent]:
        """Force-end any in-progress utterance (call on stream stop / handoff).

        A provisional blip (below min-speech) is dropped silently; a confirmed
        utterance emits its SPEECH_END. Returns the produced events.
        """
        if not self._in_speech or not self._confirmed:
            self._reset_state()
            return []
        events = [self._emit_end(self._total_samples)]
        self._reset_state()
        return events

    def reset(self) -> None:
        """Drop buffered audio and all temporal state (stream restart)."""
        self._assembler.reset()
        self._reset_state()
        self._total_samples = 0

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _reset_state(self) -> None:
        """Close the current utterance.

        Deliberately does *not* rewind ``_total_samples``: that counter is the
        position in the audio stream, and segment bounds are contractually
        absolute indices into it. Zeroing it per utterance would make every
        segment after the first address the wrong span of the caller's buffer.
        """
        self._in_speech = False
        self._confirmed = False
        self._spoken_samples = 0
        self._silence_samples = 0
        self._start_sample = 0
        self._last_speech_sample = 0
        self._start_time = 0.0

    def _consume_frame(self, prob: float) -> list[VadEvent]:
        events: list[VadEvent] = []
        frame = self._frame_samples
        self._total_samples += frame
        cursor = self._total_samples  # end-sample index of this frame
        is_speech = prob >= self._threshold

        if is_speech:
            if not self._in_speech:
                # First speech frame: open a *provisional* utterance.
                self._in_speech = True
                self._confirmed = False
                self._start_sample = cursor - frame
                self._start_time = self._clock()
                self._spoken_samples = frame
                self._silence_samples = 0
                self._last_speech_sample = cursor
            else:
                self._spoken_samples += frame
                self._silence_samples = 0
                self._last_speech_sample = cursor
                if not self._confirmed and self._spoken_samples >= self._min_speech_samples:
                    self._confirmed = True
                    events.append(VadEvent(VadEventType.SPEECH_START, timestamp=self._start_time))
                if (
                    self._confirmed
                    and self._max_speech_samples
                    and self._spoken_samples >= self._max_speech_samples
                ):
                    events.append(self._emit_end(cursor))
                    self._reset_state()
        else:
            if self._in_speech:
                if self._confirmed:
                    self._silence_samples += frame
                    if self._silence_samples >= self._max_silence_samples:
                        events.append(self._emit_end(cursor))
                        self._reset_state()
                else:
                    # Blip shorter than min-speech: abandon it, no start emitted.
                    self._reset_state()
            # else: silence during silence — nothing to do.

        return events

    def _emit_end(self, cursor: int) -> VadEvent:
        start_sample = max(0, self._start_sample - self._pad_samples)
        end_sample = self._last_speech_sample + self._pad_samples
        end_time = self._clock()
        segment = SpeechSegment(
            start_sample=start_sample,
            end_sample=end_sample,
            start_time=self._start_time,
            end_time=end_time,
            sample_rate=self._sample_rate,
        )
        logger.info(
            "speech segment end: %d..%d samples (%.0f ms)",
            start_sample,
            end_sample,
            segment.duration_ms,
        )
        return VadEvent(VadEventType.SPEECH_END, segment=segment, timestamp=end_time)
