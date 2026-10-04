"""Conversation: one chat's own state, kept apart from every other chat's.

Why this exists
---------------
``ChatService`` used to hold exactly one history, one citation list and one model choice
for the whole process. That was only ever safe because there was exactly one conversation
on screen and one question at a time. Both stopped being true: pywebview runs **every JS
bridge call on its own thread**, so a typed question, a spoken one, a phone question and
an announcement can all be inside ``ask()`` at once.

Sharing those three fields across concurrent turns is not a subtle race. The citation list
is written when retrieval runs and read after the model answers, so two turns in flight
hand each other's sources -- the panel shows 「来源：手册.pdf」 under an answer that never
read that file. The history is appended in *completion* order, so a fast answer that
finished second lands first and the next replay feeds the model a conversation in an order
nobody experienced. And the model choice was global, so "this tab asks DeepSeek, that one
asks Qwen" was impossible to express at all.

So each conversation owns its state, including its own lock, and nothing is shared between
them. The one exception is deliberate: :attr:`Conversation.provider` and
:attr:`Conversation.model` may be empty, which means "follow whatever the window picked",
so a single-tab user keeps the behaviour they had before this class existed.
"""

from __future__ import annotations

import threading
from collections.abc import Sequence
from typing import Any

from jarvis.llm.types import ChatMessage

STATUS_IDLE = "idle"
"""Nothing is running for this conversation and it has nothing to show."""

STATUS_RUNNING = "running"
"""A turn is in flight. :attr:`Conversation.phase` says which part of it."""

STATUS_DONE = "done"
"""The last turn answered. Kept as a status because the tab strip marks it once, then
falls back to :data:`STATUS_IDLE` -- a permanently amber tab would be noise."""

STATUS_FAILED = "failed"
"""The last turn ended in an error or a cancellation, and :attr:`Conversation.error`
says which. Shown until the operator asks again."""

_PHASE_TOOL = "调用工具"
_PHASE_THINKING = "思考中"
_PHASE_ANSWERING = "输出中"


class Conversation:
    """The state one chat carries: its replay list, its model, what it is doing now."""

    def __init__(
        self,
        conversation_id: str,
        *,
        title: str = "",
        provider: str = "",
        model: str = "",
    ) -> None:
        self.id = conversation_id
        self.title = title
        self.provider = provider
        self.model = model
        self.history: list[ChatMessage] = []
        self.sources: tuple[str, ...] = ()
        self.status = STATUS_IDLE
        self.phase = ""
        self.error = ""
        self.task_id = ""
        self.turn_count = 0
        self.stored = False
        """Whether this conversation has a row on disk yet.

        A tab opened but never asked is not a conversation anybody wants in 历史, so the
        row appears with the first turn -- and from then on the id must not change, or the
        tab, its running task and its stored rows stop agreeing with each other.
        """
        self.lock = threading.RLock()
        """Held for every read or write of the fields above.

        Per conversation rather than one global lock: a long model call must not be able
        to hold up somebody typing into a different tab, which is precisely what a single
        service-wide lock would do.
        """

    # -- replay list -------------------------------------------------------

    def remember(self, question: str, answer: str, *, keep_pairs: int) -> None:
        """Append one exchange and trim the replay window to ``keep_pairs`` exchanges.

        The trim lives here because the answer and the question must be cut as a *pair*:
        trimming the raw list length can leave a dangling user message, which some
        providers reject outright and others answer twice.
        """
        with self.lock:
            self.history.append(ChatMessage.user(question))
            self.history.append(ChatMessage.assistant(answer))
            limit = max(0, int(keep_pairs)) * 2
            if limit and len(self.history) > limit:
                self.history = self.history[-limit:]
            self.turn_count += 1

    def replay(self, *, limit: int) -> list[ChatMessage]:
        """The past exchanges to feed into the next request, oldest first."""
        with self.lock:
            window = [message for message in self.history if message.role != "system"]
            pairs = max(0, int(limit)) * 2
            return window[-pairs:] if pairs else []

    def replace_history(self, messages: Sequence[ChatMessage]) -> None:
        """Load a stored conversation into the replay list (opening a tab)."""
        with self.lock:
            self.history = [message for message in messages if message.role != "system"]

    def clear(self) -> None:
        """Blank this conversation. Does not touch what is on disk."""
        with self.lock:
            self.history = []
            self.sources = ()
            self.error = ""
            self.turn_count = 0

    # -- what the page shows ----------------------------------------------

    def begin_turn(self, task_id: str, *, phase: str = _PHASE_THINKING) -> None:
        """Mark this conversation as busy. Called before the model is asked."""
        with self.lock:
            self.task_id = task_id
            self.status = STATUS_RUNNING
            self.phase = phase
            self.error = ""
            self.sources = ()

    def set_phase(self, phase: str) -> None:
        with self.lock:
            if self.status == STATUS_RUNNING:
                self.phase = phase

    def end_turn(self, *, status: str, error: str = "", sources: Sequence[str] = ()) -> None:
        """Close a turn out. ``status`` is running/nothing; anything else lands here."""
        with self.lock:
            self.status = status
            self.phase = ""
            self.error = error
            self.task_id = ""
            if sources:
                self.sources = tuple(sources)

    def thinking_phase(self) -> str:
        return _PHASE_THINKING

    def tool_phase(self) -> str:
        return _PHASE_TOOL

    def speaking_phase(self, who: str) -> str:
        """The strip's line while one seat of a round table has the floor.

        Named models rather than a generic 「思考中」 because that is the whole point of
        the table: the operator wants to see whose turn it is, and three bubbles that all
        say the same thing say nothing.
        """
        return f"{who} 正在发言"

    def answering_phase(self) -> str:
        return _PHASE_ANSWERING

    def snapshot(self) -> dict[str, Any]:
        """The row the conversation strip draws."""
        with self.lock:
            return {
                "id": self.id,
                "title": self.title,
                "provider": self.provider,
                "model": self.model,
                "status": self.status,
                "phase": self.phase,
                "error": self.error,
                "task_id": self.task_id,
                "turns": self.turn_count,
                "sources": list(self.sources),
            }


__all__ = [
    "STATUS_DONE",
    "STATUS_FAILED",
    "STATUS_IDLE",
    "STATUS_RUNNING",
    "Conversation",
]
