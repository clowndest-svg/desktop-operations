"""Thread-safe bridge from voice ``PipelineEvent``s to UI state.

This module deliberately imports neither ``webview`` nor Qt, so it can be
unit-tested and reused headlessly. The desktop HUD subscribes to immutable
:class:`UiState` snapshots and renders them; it never reaches into the pipeline
directly. ``PipelineEvent`` lives in :mod:`jarvis.core.events` for the same reason:
its producer is L3 orchestration and its consumer is L5 ui, and ui may not import
orchestration (architecture rule 6).

The pipeline emits two ``reply`` events per turn — first the user transcript
(ASR result), then the assistant answer (agent-graph result). We disambiguate
them with an ``_awaiting_assistant`` flag rather than leaking that ordering
knowledge into the caller.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum

from jarvis.core.events import (
    PIPELINE_STATE_KIND,
    VOICE_STATUS_KIND,
    PipelineEvent,
    VoicePhase,
)

logger = logging.getLogger("jarvis.ui.state_bridge")

# PipelineEvent.kind values we understand.
_KIND_WAKE = "wake"
_KIND_SPEECH_START = "speech_start"
_KIND_SPEECH_END = "speech_end"
_KIND_REPLY = "reply"
_KIND_BARGE_IN = "barge_in"
_KIND_ERROR = "error"
_KIND_STATE = PIPELINE_STATE_KIND
_KIND_VOICE_STATUS = VOICE_STATUS_KIND


class UiVoiceState(StrEnum):
    """High-level voice state mirrored from the pipeline.

    Values match :class:`jarvis.orchestration.voice_pipeline.PipelineState` by
    string, not by import: the pipeline's enum is an L3 type and the bridge may
    only depend on core. The mapping is asserted in the tests, so a rename on
    either side fails loudly instead of silently pinning the indicator to idle.
    """

    IDLE = "idle"
    LISTENING = "listening"
    PROCESSING = "processing"


_PIPELINE_STATE_NAMES = {state.value: state for state in UiVoiceState}
"""Pipeline state names mapped onto what the HUD can show."""


@dataclass(frozen=True, slots=True)
class ChatTurn:
    """A single user / assistant message in the conversation history."""

    role: str  # "user" | "assistant"
    text: str


@dataclass(frozen=True, slots=True)
class UiState:
    """Immutable snapshot of everything the UI needs to render."""

    voice_state: UiVoiceState
    history: tuple[ChatTurn, ...]
    last_event: str
    interrupted: bool
    voice: VoicePhase = VoicePhase.OFF
    """Availability of the voice feature, as :class:`jarvis.app.voice_service` says.

    Separate from ``voice_state`` on purpose: "语音加载失败" and "正在听" are two
    different questions, and collapsing them is how a failed start ends up looking
    like a microphone that is merely idle.
    """

    voice_detail: str = ""
    """Why the voice feature is in this state, shown when it is not running."""

    @classmethod
    def idle(cls) -> UiState:
        """The state to show before a single event has arrived."""
        return cls(voice_state=UiVoiceState.IDLE, history=(), last_event="", interrupted=False)

    def to_dict(self) -> dict[str, object]:
        """JSON-ready form, for the one-way trip into the page."""
        return {
            "voice_state": self.voice_state.value,
            "voice": self.voice.value,
            "voice_detail": self.voice_detail,
            "history": [{"role": turn.role, "text": turn.text} for turn in self.history],
            "last_event": self.last_event,
            "interrupted": self.interrupted,
        }


class StateBridge:
    """Converts a stream of ``PipelineEvent``s into :class:`UiState` snapshots."""

    def __init__(self, max_history_turns: int = 100) -> None:
        self._lock = threading.RLock()
        self._voice_state = UiVoiceState.IDLE
        self._voice = VoicePhase.OFF
        self._voice_detail = ""
        self._history: list[ChatTurn] = []
        self._last_event = ""
        self._interrupted = False
        self._awaiting_assistant = False
        self._max_history = max_history_turns
        self._subscribers: list[Callable[[UiState], None]] = []

    # -- public API ----------------------------------------------------

    def push_event(self, event: PipelineEvent) -> None:
        """Ingest one pipeline event and notify subscribers with a snapshot."""
        with self._lock:
            self._last_event = event.kind
            self._apply(event)
            snapshot = self._build()
        self._notify(snapshot)

    def snapshot(self) -> UiState:
        """Return a thread-safe copy of the current UI state."""
        with self._lock:
            return self._build()

    def subscribe(self, callback: Callable[[UiState], None]) -> Callable[[], None]:
        """Register a callback fired on every new snapshot.

        Returns an unsubscribe callable. The callback is invoked outside the
        bridge lock, so it may safely call back into the bridge.
        """
        with self._lock:
            self._subscribers.append(callback)
        return lambda: self._unsubscribe(callback)

    def reset(self) -> None:
        """Clear all state (call when (re)starting the pipeline)."""
        with self._lock:
            self._voice_state = UiVoiceState.IDLE
            self._voice = VoicePhase.OFF
            self._voice_detail = ""
            self._history.clear()
            self._last_event = ""
            self._interrupted = False
            self._awaiting_assistant = False

    def add_turn(self, role: str, text: str) -> None:
        """Append one message that did not come from the microphone.

        The typed question and its answer have to land in the same transcript as
        spoken ones, or the panel shows two histories and the reader has to work
        out which one is the conversation.
        """
        with self._lock:
            self._add_turn(role, text)
            snapshot = self._build()
        self._notify(snapshot)

    def clear_history(self) -> None:
        """Empty the transcript, leaving the voice indicator alone.

        「清空」 is a request about the *log*. Waking-state with it would be the
        classic bug where clearing a chat panel also releases the microphone.
        """
        with self._lock:
            self._history.clear()
            self._awaiting_assistant = False
            snapshot = self._build()
        self._notify(snapshot)

    # -- internals -----------------------------------------------------

    def _apply(self, event: PipelineEvent) -> None:
        kind = event.kind
        if kind == _KIND_WAKE:
            self._voice_state = UiVoiceState.LISTENING
            self._interrupted = False
            self._awaiting_assistant = False
        elif kind == _KIND_SPEECH_START:
            self._voice_state = UiVoiceState.LISTENING
        elif kind == _KIND_SPEECH_END:
            self._voice_state = UiVoiceState.PROCESSING
            self._interrupted = False
        elif kind == _KIND_REPLY:
            if self._awaiting_assistant:
                self._add_turn("assistant", event.text)
                self._awaiting_assistant = False
            else:
                self._add_turn("user", event.text)
                self._awaiting_assistant = True
        elif kind == _KIND_BARGE_IN:
            self._interrupted = True
            self._awaiting_assistant = False
        elif kind == _KIND_ERROR:
            self._interrupted = False
        elif kind == _KIND_STATE:
            mapped = _PIPELINE_STATE_NAMES.get(event.text)
            if mapped is not None:
                self._voice_state = mapped
        elif kind == _KIND_VOICE_STATUS:
            try:
                self._voice = VoicePhase(event.text)
            except ValueError:
                logger.warning("ignoring unknown voice phase %r", event.text)
                return
            self._voice_detail = event.detail if isinstance(event.detail, str) else ""
            if self._voice is not VoicePhase.RUNNING:
                # A stopped or failed voice loop cannot also be "listening".
                self._voice_state = UiVoiceState.IDLE
                self._awaiting_assistant = False
        # Unknown kinds are ignored but still recorded in ``last_event``.

    def _add_turn(self, role: str, text: str) -> None:
        if not text:
            return
        self._history.append(ChatTurn(role=role, text=text))
        limit = self._max_history * 2
        if len(self._history) > limit:
            self._history = self._history[-limit:]

    def _build(self) -> UiState:
        return UiState(
            voice_state=self._voice_state,
            history=tuple(self._history),
            last_event=self._last_event,
            interrupted=self._interrupted,
            voice=self._voice,
            voice_detail=self._voice_detail,
        )

    def _notify(self, snapshot: UiState) -> None:
        with self._lock:
            callbacks = list(self._subscribers)
        for cb in callbacks:
            cb(snapshot)

    def _unsubscribe(self, callback: Callable[[UiState], None]) -> None:
        with self._lock:
            if callback in self._subscribers:
                self._subscribers.remove(callback)
