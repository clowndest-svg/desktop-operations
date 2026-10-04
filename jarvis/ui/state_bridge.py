"""Thread-safe bridge from voice ``PipelineEvent``s to UI state.

This module deliberately imports neither ``webview`` nor Qt, so it can be
unit-tested and reused headlessly. The desktop HUD subscribes to immutable
:class:`UiState` snapshots and renders them; it never reaches into the pipeline
directly. ``PipelineEvent`` lives in :mod:`jarvis.core.events` for the same reason:
its producer is L3 orchestration and its consumer is L5 ui, and ui may not import
orchestration (architecture rule 6).

The pipeline says who spoke: ``user_text`` carries the ASR transcript, ``reply`` carries
the assistant answer. Nothing here infers a role from what arrived last -- an
alternating flag flips permanently the first time a turn produces only one of the two
(an error, a barge-in while she talks), and the visible symptom of that flip is the
operator's own sentence filed under 小夜.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum

from jarvis.core.events import (
    PIPELINE_STATE_KIND,
    VOICE_STATUS_KIND,
    PipelineEvent,
    VoicePhase,
)

logger = logging.getLogger("jarvis.ui.state_bridge")


def _row_count(value: object) -> int:
    """A turn count from the service's row, or 0 when it is not a count.

    Guarded rather than cast because the row crossed a layer boundary as a mapping of
    objects: a ``True`` here would become "1 turn" and a string would raise on the
    render thread, which is the worst place in this program to raise.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return value if value >= 0 else 0


def _row_queue(value: object) -> tuple[QueuedQuestion, ...]:
    """The waiting questions on a conversation row, in the order they were asked.

    Same reason as ``_row_count``: this mapping came from the registry through a service
    call, so a row that carries something else is a bug to render around, not an
    exception to raise on the thread that paints the tabs.
    """
    if not isinstance(value, (list, tuple)):
        return ()
    found: list[QueuedQuestion] = []
    for item in value:
        if not isinstance(item, Mapping):
            continue
        task_id = str(item.get("task_id") or "")
        if not task_id:
            continue
        found.append(QueuedQuestion(task_id=task_id, question=str(item.get("question") or "")))
    return tuple(found)


# PipelineEvent.kind values we understand.
_KIND_WAKE = "wake"
_KIND_SPEECH_START = "speech_start"
_KIND_SPEECH_END = "speech_end"
_KIND_REPLY = "reply"
_KIND_USER_TEXT = "user_text"
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
    reasoning: str = ""
    """What the model reported thinking, when the operator switched that on.

    Carried alongside the answer rather than inside it: the text is what gets read
    aloud and replayed into the next turn, while this is shown on demand and belongs
    to neither. A spoken turn has none, so it stays empty there.
    """

    model: str = ""
    """Which model answered this one. Empty on a user turn and on a single-model tab.

    Needed because a round table writes several models into one transcript, and "她
    说" stops identifying a speaker the moment there are three of them.
    """

    record: str = ""
    """Everything said around a round table, for the fold under the answer.

    Like ``reasoning`` it is display-only: neither is stored, so switching tabs and back
    loses both. That is a deliberate limit rather than an oversight -- the two exist so a
    person can see how an answer was arrived at, and a transcript of three models' opinions
    replayed into every later question would be the largest thing in the request.
    """


@dataclass(frozen=True, slots=True)
class QueuedQuestion:
    """One question waiting its turn in a conversation.

    On the card rather than only in the registry because the panel has to draw the stack
    the operator just built -- each question they typed while she was busy, in order, with
    something to take it back out. A page that polled the task table for this would be a
    page that is sometimes showing a queue that already drained.
    """

    task_id: str
    question: str

    def to_dict(self) -> dict[str, object]:
        return {"task_id": self.task_id, "question": self.question}


@dataclass(frozen=True, slots=True)
class ConversationCard:
    """One conversation tab: what it is called, which model it asks, what it is doing.

    Deliberately a flat record rather than a view onto the service: the page renders
    this without being able to reach into a lock, and a tab whose status is stale is
    much better than a tab that blocks the render waiting for a model call.
    """

    id: str
    title: str = ""
    status: str = "idle"
    phase: str = ""
    provider: str = ""
    model: str = ""
    turns: int = 0
    active: bool = False
    task_id: str = ""
    error: str = ""
    queued: tuple[QueuedQuestion, ...] = ()
    """What this conversation still has to ask, in the order it was typed."""

    @classmethod
    def from_row(cls, row: Mapping[str, object]) -> ConversationCard:
        """Build one from what :meth:`ChatService.conversations` returns.

        Typed rather than splatted: this crosses a layer boundary, and a key renamed on
        the service side should fail here loudly instead of silently producing a tab
        that shows nothing.
        """
        return cls(
            id=str(row.get("id") or ""),
            title=str(row.get("title") or ""),
            status=str(row.get("status") or "idle"),
            phase=str(row.get("phase") or ""),
            provider=str(row.get("provider") or ""),
            model=str(row.get("model") or ""),
            turns=_row_count(row.get("turns")),
            active=bool(row.get("active")),
            task_id=str(row.get("task_id") or ""),
            error=str(row.get("error") or ""),
            queued=_row_queue(row.get("queued")),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "title": self.title,
            "status": self.status,
            "phase": self.phase,
            "provider": self.provider,
            "model": self.model,
            "turns": self.turns,
            "active": self.active,
            "task_id": self.task_id,
            "error": self.error,
            "queued": [question.to_dict() for question in self.queued],
        }


@dataclass(frozen=True, slots=True)
class StreamingTurn:
    """The answer that is still arriving, and where it belongs.

    Carries the whole partial text, never a diff. The delivery path keeps only the
    newest snapshot, so an increment-based design would lose whichever chunks it
    coalesced away -- and a lost token in the middle of an answer is not something
    anybody can see happening.
    """

    conversation_id: str
    task_id: str
    text: str
    phase: str = ""
    model: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "conversation_id": self.conversation_id,
            "task_id": self.task_id,
            "text": self.text,
            "phase": self.phase,
            "model": self.model,
        }


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

    conversations: tuple[ConversationCard, ...] = ()
    """Every tab the panel has, not just the one being read.

    Part of the snapshot rather than a separate call because two conversations can be
    answering at the same moment: a tab strip that has to round-trip to find out that
    its own turn finished is a strip that shows stale status.
    """

    streaming: StreamingTurn | None = None
    """The answer currently arriving, or ``None`` when nothing is being streamed.

    Kept out of ``history`` on purpose: a half-written answer that lands in the replay
    list would be fed back to the model as a finished statement the next time it is
    asked. The panel draws it as a temporary bubble and drops it when the turn ends.
    """

    speaks_typed: bool = True
    """Whether typed answers get read aloud.

    In the snapshot for the same reason as :attr:`thinking_loader`: the switch lives in
    the settings dialog and the control now also lives beside the voice core, and two
    copies of the truth would disagree for as long as a save takes.
    """

    thinking_loader: str = "dots"
    """Which animation the 「思考中」 surfaces show.

    In the snapshot rather than fetched by each page because the chat bubble and the
    desktop figure are two drawings of one wait: if the panel asked for the setting and
    the pet read it from disk, they could disagree for as long as a save takes, and the
    disagreement is the thing the operator would notice.
    """

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
            "history": [
                {
                    "role": turn.role,
                    "text": turn.text,
                    "reasoning": turn.reasoning,
                    "model": turn.model,
                    "record": turn.record,
                }
                for turn in self.history
            ],
            "last_event": self.last_event,
            "interrupted": self.interrupted,
            "conversations": [card.to_dict() for card in self.conversations],
            "streaming": self.streaming.to_dict() if self.streaming is not None else None,
            "thinking_loader": self.thinking_loader,
            "speaks_typed": self.speaks_typed,
        }


STREAM_FLUSH_SECONDS: float = 0.08
"""How often a growing answer is allowed to publish a snapshot.

A streamed reply produces a chunk every few tens of milliseconds, and each snapshot costs
a copy of the history plus a JSON trip into the page. Storing the text always and
notifying at most this often loses nothing -- the next flush carries the newest whole
partial -- while a per-token notify would spend the desktop's time redrawing instead of
answering.
"""


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
        self._conversations: tuple[ConversationCard, ...] = ()
        self._thinking_loader = "dots"
        self._speaks_typed = True
        self._streaming: StreamingTurn | None = None
        """The answer still arriving, if one is.

        Declared here rather than inferred, because "no streaming turn" is a state this
        object spends most of its life in and mypy cannot see it in an assignment alone.
        """
        self._stream_flushed = 0.0
        """When the last streaming snapshot was published. See :data:`STREAM_FLUSH_SECONDS`."""
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

    def add_turn(
        self, role: str, text: str, reasoning: str = "", model: str = "", record: str = ""
    ) -> None:
        """Append one message that did not come from the microphone.

        The typed question and its answer have to land in the same transcript as
        spoken ones, or the panel shows two histories and the reader has to work
        out which one is the conversation.

        ``model`` names who answered, which only matters once a panel can hold a
        round table: three models writing into one log need their rows labelled.
        """
        with self._lock:
            self._add_turn(role, text, reasoning, model, record)
            snapshot = self._build()
        self._notify(snapshot)

    def replace_history(self, rows: Sequence[Mapping[str, object]]) -> None:
        """Show another conversation: the store's own rows, oldest first.

        Takes the store's shape rather than a tuple of fields because the caller has
        nothing else to hand over, and a translation layer here would be a second place
        for 「role/content/model」 to drift away from what the table actually holds.

        A switch has to be one atomic replacement. Appending and then trimming would let
        the pump publish a snapshot holding *both* conversations, which is the exact thing
        the tab strip exists to prevent.
        """
        with self._lock:
            self._history = [
                ChatTurn(
                    role=str(row.get("role") or ""),
                    text=str(row.get("content") or ""),
                    model=str(row.get("model") or ""),
                )
                for row in rows
                if row.get("role") in ("user", "assistant") and str(row.get("content") or "")
            ][-self._max_history :]
            snapshot = self._build()
        self._notify(snapshot)

    def set_speaks_typed(self, enabled: bool) -> None:
        """Say whether typed answers get read aloud, so the panel's switch can match."""
        with self._lock:
            if bool(enabled) is self._speaks_typed:
                return
            self._speaks_typed = bool(enabled)
            snapshot = self._build()
        self._notify(snapshot)

    def set_thinking_loader(self, kind: str) -> None:
        """Say which loader the two 「思考中」 surfaces should draw."""
        with self._lock:
            if kind == self._thinking_loader:
                return
            self._thinking_loader = kind
            snapshot = self._build()
        self._notify(snapshot)

    def set_turn(self, state: UiVoiceState) -> None:
        """Say what she is doing right now, from outside the microphone's event stream.

        A typed question emits no pipeline events at all -- nothing sends ``speech_end``
        for a keyboard -- so without this call there is no way for the figure to know
        she is being asked something, and 「思考中」 stays a label only the voice path
        can reach. This moves the *turn* axis and nothing else: whether the microphone
        is open remains the service's to report, and the two are separate on purpose.
        """
        with self._lock:
            if state is self._voice_state:
                return
            self._voice_state = state
            snapshot = self._build()
        self._notify(snapshot)

    def clear_history(self) -> None:
        """Empty the transcript, leaving the voice indicator alone.

        「清空」 is a request about the *log*. Waking-state with it would be the
        classic bug where clearing a chat panel also releases the microphone.
        """
        with self._lock:
            self._history.clear()
            snapshot = self._build()
        self._notify(snapshot)

    def set_conversations(self, rows: Sequence[Mapping[str, object]]) -> None:
        """Publish the tab strip. Called whenever a turn starts, ends, or switches.

        No equality guard here: the rows are rebuilt from the service each time and a
        status that changed without anybody pressing anything (a task finishing on its
        own thread) is exactly the case a guard would swallow.
        """
        with self._lock:
            self._conversations = tuple(ConversationCard.from_row(row) for row in rows)
            snapshot = self._build()
        self._notify(snapshot)

    def set_stream(
        self,
        conversation_id: str,
        task_id: str,
        text: str,
        *,
        phase: str = "",
        model: str = "",
    ) -> None:
        """Replace the in-progress answer with the whole partial text so far.

        The text is always stored; the snapshot is only published on the flush interval.
        A caller that reads the newest snapshot when it does arrive cannot tell the
        difference, because each one carries the complete partial.
        """
        now = time.monotonic()
        with self._lock:
            self._streaming = StreamingTurn(
                conversation_id=conversation_id,
                task_id=task_id,
                text=text,
                phase=phase,
                model=model,
            )
            if now - self._stream_flushed < STREAM_FLUSH_SECONDS:
                return
            self._stream_flushed = now
            snapshot = self._build()
        self._notify(snapshot)

    def clear_stream(self, task_id: str = "") -> None:
        """Drop the in-progress bubble.

        ``task_id`` is matched when given, so a tab that finished cannot erase the
        bubble of the turn another tab has already started -- which is what happens the
        moment two conversations are answering at once.
        """
        with self._lock:
            if self._streaming is None:
                return
            if task_id and self._streaming.task_id != task_id:
                return
            self._streaming = None
            # Reset the throttle so the next answer's first chunk is published at once
            # instead of waiting out an interval that belongs to the turn that just ended.
            self._stream_flushed = 0.0
            snapshot = self._build()
        self._notify(snapshot)

    # -- internals -----------------------------------------------------

    def _apply(self, event: PipelineEvent) -> None:
        kind = event.kind
        if kind == _KIND_WAKE:
            self._voice_state = UiVoiceState.LISTENING
            self._interrupted = False
        elif kind == _KIND_SPEECH_START:
            self._voice_state = UiVoiceState.LISTENING
        elif kind == _KIND_SPEECH_END:
            self._voice_state = UiVoiceState.PROCESSING
            self._interrupted = False
        elif kind == _KIND_USER_TEXT:
            self._add_turn("user", event.text)
        elif kind == _KIND_REPLY:
            self._add_turn("assistant", event.text)
        elif kind == _KIND_BARGE_IN:
            self._interrupted = True
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
        # Unknown kinds are ignored but still recorded in ``last_event``.

    def _add_turn(
        self, role: str, text: str, reasoning: str = "", model: str = "", record: str = ""
    ) -> None:
        if not text:
            return
        self._history.append(
            ChatTurn(role=role, text=text, reasoning=reasoning, model=model, record=record)
        )
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
            conversations=self._conversations,
            streaming=self._streaming,
            thinking_loader=self._thinking_loader,
            speaks_typed=self._speaks_typed,
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
