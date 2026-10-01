"""The voice pipeline: one microphone loop driving the full chain.

State machine (single daemon thread):

    IDLE        wait for wake word -> LISTENING
    LISTENING   feed VAD; on SPEECH_END -> slice + ASR -> PROCESSING
    PROCESSING  run ASR text -> AgentGraph -> TTS (Barge-In watches the mic)

A single :class:`~jarvis.audio.source.AudioSource` is shared across all states,
so only one capture device is opened. The standalone wakeword / vad services
are NOT used here; their engines are driven directly (detector / segmenter).
The buffer and the VAD segmenter are reset together every time we (re-)enter
LISTENING, which keeps ``SpeechSegment`` sample indices aligned with the
accumulated buffer that ASR slices from.
"""

from __future__ import annotations

import contextlib
import logging
import math
import threading
from array import array
from collections.abc import Callable, Iterator
from enum import StrEnum
from typing import Protocol, runtime_checkable

from jarvis.asr.types import RecognitionResult
from jarvis.audio.source import AudioSource, SounddeviceSource
from jarvis.core.events import PIPELINE_STATE_KIND, PipelineEvent
from jarvis.core.exceptions import JarvisError
from jarvis.llm.types import ChatMessage
from jarvis.orchestration.player import AudioPlayer, NullAudioPlayer
from jarvis.tts.types import AudioChunk, ShouldStop
from jarvis.vad.segmenter import VoiceActivitySegmenter
from jarvis.vad.types import SpeechSegment, VadEventType
from jarvis.wakeword.detector import WakeWordDetector
from jarvis.wakeword.types import WakeEvent

logger = logging.getLogger("jarvis.orchestration.pipeline")


class PipelineState(StrEnum):
    """High-level voice-pipeline state."""

    IDLE = "idle"
    LISTENING = "listening"
    PROCESSING = "processing"


_FAILURE_MAX_CHARS: int = 200
"""Longest failure line shown in the window; the log carries the rest."""


def _failure_text(exc: BaseException) -> str:
    """One line the operator can act on, for a turn that did not answer.

    A typed :class:`~jarvis.core.exceptions.JarvisError` carries a message written
    for a human -- a missing key names the *variable*, never a value -- so it is
    safe to show. Anything else is only ever named: an unexpected exception string
    can contain whatever the transport echoed back, and "processing failed" told
    nobody that the answer was one environment variable away.
    """
    if isinstance(exc, JarvisError):
        reason = " ".join(str(exc).split())[:_FAILURE_MAX_CHARS]
        return reason or f"{type(exc).__name__}"
    return f"内部错误 {type(exc).__name__}（原因在日志里）"


@runtime_checkable
class _AsrPort(Protocol):
    """Minimal ASR surface the pipeline needs."""

    def recognize(
        self,
        audio: bytes,
        *,
        segment: SpeechSegment | None = None,
        language: str | None = None,
    ) -> RecognitionResult: ...


@runtime_checkable
class _TtsPort(Protocol):
    """Minimal TTS surface the pipeline needs."""

    def synthesize(
        self,
        text: str,
        *,
        voice: str | None = None,
        speed: float | None = None,
        volume: float | None = None,
        should_stop: ShouldStop | None = None,
    ) -> Iterator[AudioChunk]: ...


@runtime_checkable
class _GraphPort(Protocol):
    """Minimal agent-graph surface the pipeline needs."""

    def run(self, user_text: str, history: list[ChatMessage] | None = None) -> str: ...


# Read this many samples per blocking capture call.
_READ_CHUNK_SAMPLES = 512


def speech_level(audio: bytes) -> tuple[int, float]:
    """Peak and RMS of an s16le mono span, without pulling in numpy.

    This runs on the turn thread once per utterance, over a few tens of thousands of
    samples; ``array`` gives that in single-digit milliseconds, and orchestration
    stays free of a numeric dependency it would only use twice.
    """
    samples = array("h")
    samples.frombytes(audio[: len(audio) - (len(audio) % 2)])
    if not samples:
        return 0, 0.0
    peak = max(max(samples), -min(samples))
    mean_square = sum(value * value for value in samples) / len(samples)
    return peak, math.sqrt(mean_square)


class VoicePipeline:
    """Drives the full voice chain through one microphone loop."""

    def __init__(
        self,
        *,
        detector: WakeWordDetector,
        segmenter: VoiceActivitySegmenter,
        asr: _AsrPort,
        graph: _GraphPort,
        tts: _TtsPort,
        source_factory: Callable[[], AudioSource] = SounddeviceSource,
        player: AudioPlayer | None = None,
        barge_in: bool = True,
        read_chunk_samples: int = _READ_CHUNK_SAMPLES,
        on_event: Callable[[PipelineEvent], None] | None = None,
        max_history_turns: int = 20,
        voice_provider: Callable[[], str | None] | None = None,
        transcript_sink: Callable[[str, str], None] | None = None,
    ) -> None:
        self._detector = detector
        self._segmenter = segmenter
        self._asr = asr
        self._graph = graph
        self._tts = tts
        self._source_factory = source_factory
        self._player = player or NullAudioPlayer()
        self._barge_in = barge_in
        self._read_chunk = read_chunk_samples
        self._on_event = on_event
        self._max_history = max_history_turns
        self._voice_provider = voice_provider
        self._transcript_sink = transcript_sink

        self._source: AudioSource | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._interrupt = threading.Event()
        self._state = PipelineState.IDLE
        self._state_lock = threading.Lock()
        self._buffer = bytearray()
        self._history: list[ChatMessage] = []
        self._reading = threading.Event()
        """Set while a read-aloud (not a microphone turn) is being played."""

        self._cancel_reading = threading.Event()
        """Raised by :meth:`cancel_utterance`; re-armed at the start of each one."""

    def _chosen_voice(self) -> str | None:
        """The voice to speak with, read at utterance time rather than at boot.

        A provider rather than a value: picking a voice in the HUD must change the
        *next* sentence, and the engine bakes nothing in per call -- ``synthesize``
        takes the id every time. ``None`` means "whatever the engine was configured
        with", which is also what an operator who never opened the picker gets.
        """
        if self._voice_provider is None:
            return None
        try:
            return self._voice_provider()
        except Exception:  # a prefs read must not kill a sentence
            logger.exception("voice provider failed; using the configured voice")
            return None

    @property
    def running(self) -> bool:
        """Whether the listening loop is currently alive."""
        return self._thread is not None and self._thread.is_alive()

    @property
    def reading(self) -> bool:
        """Whether a typed answer is currently being spoken."""
        return self._reading.is_set()

    @property
    def state(self) -> PipelineState:
        """The current turn state, readable from any thread.

        The UI pulls this when a page joins late: events tell it *when* something
        changed, this tells it *what is true now*, and without the second one a
        snapshot taken between two events can disagree with the microphone.
        """
        return self._get_state()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Open the microphone and start the listening loop (idempotent)."""
        if self.running:
            return
        source = self._source_factory()
        source.open()
        self._source = source
        self._stop.clear()
        self._interrupt.clear()
        self._set_state(PipelineState.IDLE)
        self._buffer.clear()
        self._history.clear()
        self._thread = threading.Thread(
            target=self._listen_loop, name="jarvis-pipeline", daemon=True
        )
        self._thread.start()
        logger.info("voice pipeline started (barge_in=%s)", self._barge_in)

    def stop(self) -> None:
        """Stop the loop and release the microphone / engines (idempotent)."""
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=5.0)
        source, self._source = self._source, None
        if source is not None:
            source.close()
        self._close(self._detector.engine)
        self._close(self._segmenter.engine)
        self._close(self._player)

    # ------------------------------------------------------------------
    # Manual turn
    # ------------------------------------------------------------------

    def speak_now(self) -> bool:
        """Open a turn without the wake word. ``False`` when it cannot.

        Only IDLE is interruptible this way. Mid-listening there is nothing to
        start, and mid-processing the user is already talking over an answer --
        that is barge-in's job, and having two features seize the microphone on
        the same click is how "it heard me but answered the wrong thing" happens.
        """
        if not self.running:
            # No capture thread to hear the answer; flipping the state would leave
            # the HUD showing "listening" with nothing behind it.
            return False
        with self._state_lock:
            if self._stop.is_set() or self._state is not PipelineState.IDLE:
                return False
            self._state = PipelineState.LISTENING
        # Same pairing as every other entry into LISTENING: the segmenter and the
        # buffer are reset together, or SpeechSegment sample indices stop matching
        # the buffer ASR slices from.
        self._segmenter.reset()
        self._buffer.clear()
        self._emit(PipelineEvent(kind=PIPELINE_STATE_KIND, text=PipelineState.LISTENING.value))
        logger.info("manual turn opened (wake word skipped)")
        return True

    # ------------------------------------------------------------------
    # Read-aloud
    # ------------------------------------------------------------------

    def utter(self, text: str) -> bool:
        """Speak a sentence the page already shows, without opening a turn.

        Returns ``False`` unless the loop is idle and nothing else is being read.
        The refusal is not shyness: an answer spoken on top of a spoken question is
        how "it heard me but answered the wrong thing" happens, and the user has no
        way to tell which of the two voices they are listening to.

        This is deliberately *not* a turn. The state machine stays IDLE so the
        microphone keeps hearing the wake word -- which also means barge-in does not
        cover it, so cancelling belongs to the caller: :meth:`cancel_utterance`
        stops the samples still to come, and whoever fed the page the ones already
        delivered has to retract those.
        """
        if not self.running or self._stop.is_set():
            return False
        if self._get_state() is not PipelineState.IDLE or self._reading.is_set():
            return False
        self._reading.set()
        self._cancel_reading.clear()
        threading.Thread(
            target=self._read_aloud,
            args=(text,),
            name="jarvis-utter",
            daemon=True,
        ).start()
        return True

    def _read_aloud(self, text: str) -> None:
        """Synthesize and play one sentence. Runs on its own thread, never the UI's.

        ``cancel_utterance`` may be called at any point in here; the two places it
        is checked are the synthesis predicate (stops producing more audio) and the
        loop body (stops the current chunk from reaching the player).
        """
        cancelled = self._cancel_reading
        try:
            for chunk in self._tts.synthesize(
                text,
                voice=self._chosen_voice(),
                should_stop=lambda: cancelled.is_set() or self._stop.is_set(),
            ):
                if cancelled.is_set() or self._stop.is_set():
                    break
                self._player.play(chunk)
        except Exception as exc:
            logger.exception("read-aloud failed")
            self._emit(PipelineEvent(kind="error", text=_failure_text(exc)))
        finally:
            self._reading.clear()

    def cancel_utterance(self) -> bool:
        """Stop a read-aloud in progress. ``False`` when there is none."""
        if not self._reading.is_set():
            return False
        self._cancel_reading.set()
        return True

    # ------------------------------------------------------------------
    # Listening loop
    # ------------------------------------------------------------------

    def _listen_loop(self) -> None:
        source = self._source
        if source is None:  # pragma: no cover - defensive
            return
        while not self._stop.is_set():
            try:
                chunk = source.read(self._read_chunk)
            except Exception:
                if self._stop.is_set():
                    break
                logger.exception("audio capture failed; pipeline exiting")
                break
            try:
                self._dispatch(chunk)
            except Exception:
                logger.exception("pipeline dispatch error; continuing")
        logger.info("voice pipeline stopped")

    def _dispatch(self, chunk: bytes) -> None:
        state = self._get_state()
        if state == PipelineState.IDLE:
            for event in self._detector.feed(chunk):
                self._on_wake(event)
        elif state == PipelineState.LISTENING:
            self._buffer.extend(chunk)
            for vevent in self._segmenter.feed(chunk):
                if vevent.type == VadEventType.SPEECH_END:
                    assert vevent.segment is not None
                    self._on_speech_end(vevent.segment)
                elif vevent.type == VadEventType.SPEECH_START:
                    self._emit(PipelineEvent(kind="speech_start"))
        elif state == PipelineState.PROCESSING:
            if not self._barge_in:
                return
            for vevent in self._segmenter.feed(chunk):
                if vevent.type == VadEventType.SPEECH_START and not self._interrupt.is_set():
                    self._interrupt.set()
                    self._emit(PipelineEvent(kind="barge_in"))

    # ------------------------------------------------------------------
    # State transitions
    # ------------------------------------------------------------------

    def _on_wake(self, event: WakeEvent) -> None:
        self._emit(PipelineEvent(kind="wake", text=event.keyword, detail=event.score))
        self._segmenter.reset()
        self._buffer.clear()
        if event.command:
            # The wake engine already transcribed this utterance, so the command
            # is in hand. Requiring a second sentence to say the same words the
            # user just spoke would be a bug, not a design.
            logger.info("WAKE: %s — command %r", event.keyword, event.command)
            self._start_text_turn(event.command)
            return
        self._set_state(PipelineState.LISTENING)
        logger.info("WAKE: %s — listening for command", event.keyword)

    def _start_text_turn(self, text: str) -> None:
        """Answer an already-known command without another capture round."""
        self._set_state(PipelineState.PROCESSING)
        threading.Thread(
            target=self._transcribed_turn,
            args=(text,),
            name="jarvis-process",
            daemon=True,
        ).start()

    def _transcribed_turn(self, text: str) -> None:
        """Run a turn whose user text is already known, then resume listening."""
        self._interrupt.clear()
        try:
            self._respond(text)
        except Exception as exc:
            logger.exception("processing turn failed")
            self._emit(PipelineEvent(kind="error", text=_failure_text(exc)))
        finally:
            self._end_turn()

    def _end_turn(self) -> None:
        self._segmenter.reset()
        self._buffer.clear()
        self._set_state(PipelineState.LISTENING)

    def _on_speech_end(self, segment: SpeechSegment) -> None:
        self._emit(PipelineEvent(kind="speech_end"))
        audio = bytes(self._buffer)
        self._buffer.clear()
        self._set_state(PipelineState.PROCESSING)
        threading.Thread(
            target=self._process,
            args=(audio, segment),
            name="jarvis-process",
            daemon=True,
        ).start()

    def _process(self, audio: bytes, segment: SpeechSegment) -> None:
        self._interrupt.clear()
        peak, rms = speech_level(audio)
        # One line per turn, and it is the only place a quiet microphone becomes
        # visible. SenseVoice returns confident text from near-silence, so "the
        # recognition is bad" and "the microphone delivered nothing" look identical
        # on screen -- and they are two different bugs in two different halves.
        logger.info(
            "captured %d ms of speech: peak=%d rms=%.0f%s",
            len(audio) // 32,
            peak,
            rms,
            "  <-- 几乎没收到声音，检查输入设备与录音音量" if peak < 400 else "",
        )
        try:
            result = self._asr.recognize(audio, segment=segment)
            text = result.text.strip()
            if not text:
                return
            self._respond(text)
        except Exception as exc:
            logger.exception("processing turn failed")
            self._emit(PipelineEvent(kind="error", text=_failure_text(exc)))
        finally:
            self._end_turn()

    def _respond(self, text: str) -> None:
        """Answer one user utterance: agent turn, history, then spoken output."""
        self._emit(PipelineEvent(kind="reply", text=text))
        reply = self._graph.run(text, self._history)
        self._append_history(ChatMessage.user(text))
        self._append_history(ChatMessage.assistant(reply))
        self._emit(PipelineEvent(kind="reply", text=reply))
        for chunk in self._tts.synthesize(
            reply, voice=self._chosen_voice(), should_stop=self._should_stop
        ):
            if self._stop.is_set():
                break
            self._player.play(chunk)

    def _should_stop(self) -> bool:
        return self._interrupt.is_set() or self._stop.is_set()

    def _append_history(self, message: ChatMessage) -> None:
        self._history.append(message)
        if self._transcript_sink is not None:
            # A spoken turn is a turn: it belongs in the same stored conversation
            # as a typed one. A sink that throws must not cost the reply, so the
            # failure is logged and the conversation continues in memory.
            try:
                self._transcript_sink(message.role.value, message.content or "")
            except Exception:
                logger.exception("transcript sink failed; turn kept in memory only")
        limit = self._max_history * 2
        if len(self._history) > limit:
            self._history = self._history[-limit:]

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _set_state(self, state: PipelineState) -> None:
        with self._state_lock:
            self._state = state
        # Emitted *outside* the lock: ``_emit`` runs caller code, and the first
        # thing the desktop bridge does is read ``pipeline.state`` to build its
        # snapshot. Doing that while holding the lock would deadlock the
        # microphone thread -- which is the one thread that must never stall.
        self._emit(PipelineEvent(kind=PIPELINE_STATE_KIND, text=state.value))

    def _get_state(self) -> PipelineState:
        with self._state_lock:
            return self._state

    def _emit(self, event: PipelineEvent) -> None:
        if self._on_event is not None:
            try:
                self._on_event(event)
            except Exception:  # pragma: no cover - callback is caller code
                logger.exception("pipeline event callback raised")

    @staticmethod
    def _close(obj: object) -> None:
        closer = getattr(obj, "close", None)
        if callable(closer):
            with contextlib.suppress(Exception):
                closer()
