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
import threading
from collections.abc import Callable, Iterator
from enum import StrEnum
from typing import Protocol, runtime_checkable

from jarvis.asr.types import RecognitionResult
from jarvis.audio.source import AudioSource, SounddeviceSource
from jarvis.core.events import PIPELINE_STATE_KIND, PipelineEvent
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

        self._source: AudioSource | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._interrupt = threading.Event()
        self._state = PipelineState.IDLE
        self._state_lock = threading.Lock()
        self._buffer = bytearray()
        self._history: list[ChatMessage] = []

    @property
    def running(self) -> bool:
        """Whether the listening loop is currently alive."""
        return self._thread is not None and self._thread.is_alive()

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
        except Exception:
            logger.exception("processing turn failed")
            self._emit(PipelineEvent(kind="error", text="processing failed"))
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
        try:
            result = self._asr.recognize(audio, segment=segment)
            text = result.text.strip()
            if not text:
                return
            self._respond(text)
        except Exception:
            logger.exception("processing turn failed")
            self._emit(PipelineEvent(kind="error", text="processing failed"))
        finally:
            self._end_turn()

    def _respond(self, text: str) -> None:
        """Answer one user utterance: agent turn, history, then spoken output."""
        self._emit(PipelineEvent(kind="reply", text=text))
        reply = self._graph.run(text, self._history)
        self._append_history(ChatMessage.user(text))
        self._append_history(ChatMessage.assistant(reply))
        self._emit(PipelineEvent(kind="reply", text=reply))
        for chunk in self._tts.synthesize(reply, should_stop=self._should_stop):
            if self._stop.is_set():
                break
            self._player.play(chunk)

    def _should_stop(self) -> bool:
        return self._interrupt.is_set() or self._stop.is_set()

    def _append_history(self, message: ChatMessage) -> None:
        self._history.append(message)
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
