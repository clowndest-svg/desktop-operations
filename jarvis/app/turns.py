"""Turns: the table of running and recently finished work, and how it gets stopped.

Why a registry instead of "just a thread per question"
------------------------------------------------------
Every bridge call already runs on its own thread, so starting a turn concurrently is the
easy half. The parts that were impossible before this file:

* **Naming it.** "Stop that one" needs an id. Without one the UI can only stop the last
  question it remembers sending, which is wrong the moment two tabs are busy.
* **Knowing what is running.** The 任务 strip needs status per conversation, and the
  ledger needs to attribute spend to a task -- neither exists if a turn is an anonymous
  thread.
* **Cancelling on purpose.** See below; this is the only place the cancel flag lives.
* **Waiting in line.** One conversation answers one question at a time: a second
  question typed while the first is still thinking is registered as ``queued`` and
  starts when its conversation frees up. Two conversations still run at once -- the
  queue is per conversation, because the thing being protected is one record of one
  dialogue, not the model's capacity.

Cancellation is cooperative, and the boundaries are real and worth stating to whoever
reports "停止没反应": the flag is checked between tool rounds, between streamed answer
chunks (about one token's worth of work, so effectively instant while streaming is on),
and between seats of a round table. A **non-streaming** HTTP request in flight cannot be
interrupted -- :mod:`jarvis.llm.transport` uses ``urlopen`` and keeps no socket handle --
so on that path the turn stops when the response arrives. That is why answering streams by
default: it is not only nicer, it is what makes 停止 mean now.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from typing import Any

from jarvis.database import format_timestamp, utc_now

logger = logging.getLogger("jarvis.app.turns")

KIND_CHAT = "chat"
"""One question to one model, with tools."""

KIND_ROUNDTABLE = "roundtable"
"""Several models taking turns over one shared record, then a merge."""

STATE_RUNNING = "running"
STATE_QUEUED = "queued"
STATE_DONE = "done"
STATE_FAILED = "failed"
STATE_CANCELLED = "cancelled"

QUEUED_PHASE = "排队中"
"""What a waiting turn shows while it has no thread yet.

The alternative was to keep the operator's second question invisible until the first one
answers, which is indistinguishable from "it did not get my message" -- the same failure
the 思考中 card exists to prevent, one turn earlier."""

MAX_KEPT = 60
"""How many finished turns to remember for the panel. Enough for a working session,
bounded so a long-uptime desktop does not grow a list forever."""


@dataclass(frozen=True, slots=True)
class TurnInfo:
    """What the panel and the ledger need to know about one turn."""

    task_id: str
    conversation_id: str
    kind: str
    question: str
    state: str
    phase: str = ""
    error: str = ""
    participants: tuple[str, ...] = ()
    started_at: str = ""
    finished_at: str = ""
    answer_chars: int = 0
    tools_used: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "conversation_id": self.conversation_id,
            "kind": self.kind,
            "question": self.question,
            "state": self.state,
            "phase": self.phase,
            "error": self.error,
            "participants": list(self.participants),
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "answer_chars": self.answer_chars,
            "tools_used": list(self.tools_used),
        }


@dataclass(frozen=True, slots=True)
class TurnContext:
    """What a running turn knows about itself.

    Handed to the worker instead of letting it reach back into the registry: the worker
    should not be able to touch another turn's state, and ``should_stop`` has to be
    cheap enough to call inside a token loop.
    """

    info: TurnInfo
    _registry: TurnRegistry

    @property
    def task_id(self) -> str:
        return self.info.task_id

    @property
    def conversation_id(self) -> str:
        return self.info.conversation_id

    def should_stop(self) -> bool:
        """Whether the operator asked for this turn to stop. Never raises."""
        return self._registry.is_cancelling(self.task_id)

    def phase(self, text: str) -> None:
        """Say which part of the turn this is, for the strip and the thinking bubble."""
        self._registry.set_phase(self.task_id, text)

    def delta(self, text: str) -> None:
        """One more piece of the answer, already accumulated to the full partial text.

        The caller passes the whole partial, not the diff, so a dropped delivery is
        harmless -- the UI pump keeps only the newest snapshot and would otherwise lose
        whichever tokens it skipped.
        """
        self._registry.publish_delta(self.task_id, text)


class TurnRegistry:
    """Starts turns, holds their status, and holds the only cancel flags."""

    def __init__(
        self,
        *,
        on_update: Callable[[TurnInfo], None] | None = None,
        on_delta: Callable[[TurnInfo, str], None] | None = None,
        name_prefix: str = "jarvis-turn",
    ) -> None:
        """Create the table.

        Args:
            on_update: Called on the worker's thread whenever a turn's status changes,
                so the shell can push a fresh snapshot. Must not raise.
            on_delta: Called with the accumulated partial answer while a turn streams.
            name_prefix: Thread name prefix -- ``jstack`` output is how this gets debugged.
        """
        self.on_update = on_update
        """Called on the worker's thread when a turn's status changes.

        Public and assignable because the bridge that renders it is built after this
        table exists: the composition root does not know the sink before the window does.
        """
        self.on_delta = on_delta
        """Called with the accumulated partial answer while a turn streams."""
        self._name_prefix = name_prefix
        self._lock = threading.RLock()
        self._turns: dict[str, TurnInfo] = {}
        self._order: list[str] = []
        self._cancel: dict[str, threading.Event] = {}
        self._threads: dict[str, threading.Thread] = {}
        self._staged: dict[str, tuple[Callable[[TurnContext], dict[str, Any]], TurnContext]] = {}
        """The work a queued turn runs when its conversation frees up.

        Held here rather than closed over by an already-started thread: a turn that has
        not begun must not touch the model, the ledger or the transcript. ``chat.ask`` is
        what writes the question's own row, so deferring the callable is what keeps two
        queued questions from landing in the record out of order."""
        self._pending: dict[str, list[str]] = {}
        """Per-conversation FIFO of task ids waiting. Per conversation, not global: two
        tabs asking at once is the whole point of the table, and a queue that serialised
        them would throw that away to protect a model call that can run twice."""
        self._begun: dict[str, threading.Event] = {}
        """Set the moment a turn gets its thread, which for a queued turn is later."""

    # -- starting ----------------------------------------------------------

    def start(
        self,
        *,
        conversation_id: str,
        kind: str,
        question: str,
        work: Callable[[TurnContext], dict[str, Any]],
        participants: Sequence[str] = (),
    ) -> TurnInfo:
        """Take one turn, and run it as soon as that conversation is free.

        Returns the record immediately whether the turn started now or queued behind
        another question in the same conversation -- ``state`` says which. ``work``
        returns the fields it knows at the end (``answer_chars``, ``tools_used``);
        returning a value is not how a failure is reported -- raising ends the turn as
        failed, and the exception is logged with the task id attached.
        """
        task_id = uuid.uuid4().hex[:12]
        info = TurnInfo(
            task_id=task_id,
            conversation_id=conversation_id,
            kind=kind,
            question=question[:200],
            state=STATE_RUNNING,
            phase="思考中",
            participants=tuple(participants),
            started_at=format_timestamp(utc_now()),
        )
        cancel = threading.Event()
        context = TurnContext(info=info, _registry=self)
        with self._lock:
            waiting = self._running_locked(conversation_id)
            if waiting:
                info = replace(info, state=STATE_QUEUED, phase=QUEUED_PHASE)
                self._pending.setdefault(conversation_id, []).append(task_id)
                self._staged[task_id] = (work, context)
            self._turns[task_id] = info
            self._order.append(task_id)
            self._cancel[task_id] = cancel
            self._begun[task_id] = threading.Event()
            self._trim_locked()
        if waiting:
            logger.info("turn %s queued behind conversation %s", task_id, conversation_id)
            self._announce(info)
            return info
        self._launch(task_id, info, work, context, cancel)
        return info

    def _launch(
        self,
        task_id: str,
        info: TurnInfo,
        work: Callable[[TurnContext], dict[str, Any]],
        context: TurnContext,
        cancel: threading.Event,
    ) -> None:
        """Give a turn its thread. Called with the lock released, from either door."""
        thread = threading.Thread(
            target=self._drive,
            args=(task_id, work, context, cancel),
            name=f"{self._name_prefix}-{task_id}",
            daemon=True,
        )
        with self._lock:
            self._threads[task_id] = thread
            begun = self._begun.get(task_id)
        if begun is not None:
            begun.set()
        # Announce the turn as it is accepted, not when the service first changes its
        # phase: "what is running" is true from this instant, and everything that lights
        # up from it -- the tab card and the pet's 「思考中」 -- should not have to wait
        # for a model call to begin to become visible.
        self._announce(info)
        thread.start()

    def _drain(self, conversation_id: str) -> None:
        """Start the next question for a conversation that just became free.

        Only ever fires when nothing is running there, which is what makes it safe to
        call from every exit: a cancelled turn, a finished one and a conversation that
        was never busy all reach this line, and none of them may start a second answer
        beside one already on its way.
        """
        with self._lock:
            if self._running_locked(conversation_id):
                return
            waiting = self._pending.get(conversation_id) or []
            if not waiting:
                return
            task_id = waiting[0]
            staged = self._staged.pop(task_id, None)
            info = self._turns.get(task_id)
            if staged is None or info is None:
                # Nothing to run under a name still in line: drop it rather than leave
                # the queue stuck on an entry that will never start.
                self._pending[conversation_id] = waiting[1:]
                return
            if len(waiting) == 1:
                self._pending.pop(conversation_id, None)
            else:
                self._pending[conversation_id] = waiting[1:]
            info = replace(info, state=STATE_RUNNING, phase="思考中")
            self._turns[task_id] = info
        work, context = staged
        logger.info("turn %s starts: conversation %s is free", task_id, conversation_id)
        self._launch(task_id, info, work, context, self._cancel_of(task_id))

    def _drive(
        self,
        task_id: str,
        work: Callable[[TurnContext], dict[str, Any]],
        context: TurnContext,
        cancel: threading.Event,
    ) -> None:
        """Run the worker and file its result. Runs on the worker thread."""
        try:
            outcome = work(context) or {}
        except Exception as exc:
            logger.exception("turn %s failed", task_id)
            stopped = cancel.is_set()
            self._finish(
                task_id,
                STATE_CANCELLED if stopped else STATE_FAILED,
                error="" if stopped else f"{type(exc).__name__}: {exc}",
            )
            return
        stopped = cancel.is_set()
        self._finish(
            task_id,
            STATE_CANCELLED if stopped else str(outcome.get("state") or STATE_DONE),
            error=str(outcome.get("error") or ""),
            answer_chars=int(outcome.get("answer_chars") or 0),
            tools_used=tuple(str(name) for name in (outcome.get("tools_used") or ())),
        )

    # -- stopping ----------------------------------------------------------

    def cancel(self, task_id: str) -> dict[str, Any]:
        """Ask one turn to stop. Returns what happened, because the button must say so."""
        target = str(task_id)
        with self._lock:
            info = self._turns.get(target)
            cancel = self._cancel.get(target)
            state = "" if info is None else info.state
        if info is None:
            return {"ok": False, "error": "没有这个任务（可能已经结束了）", "task_id": target}
        if state == STATE_QUEUED:
            # Never started, so there is nothing to interrupt: take it out of line and
            # the question is simply not asked. Leaving it queued would answer a question
            # the operator just withdrew, one turn later.
            self._unqueue(target)
            logger.info("turn %s cancelled while queued", target)
            return {"ok": True, "error": "", "task_id": target, "state": STATE_CANCELLED}
        if cancel is None or state != STATE_RUNNING:
            return {"ok": False, "error": "这一轮已经结束了", "task_id": target}
        cancel.set()
        logger.info("turn %s cancellation requested", target)
        return {"ok": True, "error": "", "task_id": target, "state": state}

    def _unqueue(self, task_id: str) -> None:
        """Drop a waiting turn. Its conversation is untouched: something may be running."""
        with self._lock:
            info = self._turns.get(task_id)
            conversation = "" if info is None else info.conversation_id
            line = list(self._pending.get(conversation) or [])
            if task_id in line:
                line.remove(task_id)
                if line:
                    self._pending[conversation] = line
                else:
                    self._pending.pop(conversation, None)
            self._staged.pop(task_id, None)
        self._finish(task_id, STATE_CANCELLED)

    def is_cancelling(self, task_id: str) -> bool:
        """Whether a turn was asked to stop. Workers poll this at their boundaries."""
        with self._lock:
            cancel = self._cancel.get(task_id)
        return bool(cancel is not None and cancel.is_set())

    def cancel_for_conversation(self, conversation_id: str) -> dict[str, Any]:
        """Stop whatever that conversation is running. The tab's 停止 button."""
        with self._lock:
            pending = [
                task_id
                for task_id, info in self._turns.items()
                if info.conversation_id == conversation_id and info.state == STATE_RUNNING
            ]
        stopped = [str(self.cancel(task_id)["task_id"]) for task_id in pending]
        return {
            "ok": bool(stopped),
            "stopped": stopped,
            "error": "" if stopped else "没有正在进行的任务",
        }

    def wait(self, task_id: str, timeout: float) -> bool:
        """Block until that turn finishes. Used by the callers that still want a
        synchronous answer (the phone, and anything announcing a result).

        The timeout covers the wait **in line** as well as the answer: a question that
        queues behind another one on the same conversation holds the caller open until
        it is its turn, which is what "等上一个执行完再做" means for a caller that is
        sitting in the call. Timing out says so instead of answering empty.
        """
        deadline = time.monotonic() + max(0.0, float(timeout))
        with self._lock:
            begun = self._begun.get(task_id)
        if begun is not None and not begun.wait(max(0.0, deadline - time.monotonic())):
            return False
        with self._lock:
            thread = self._threads.get(task_id)
        if thread is None:
            return True
        thread.join(max(0.0, deadline - time.monotonic()))
        return not thread.is_alive()

    # -- reporting ---------------------------------------------------------

    def set_phase(self, task_id: str, phase: str) -> None:
        with self._lock:
            info = self._turns.get(task_id)
            if info is None or info.state != STATE_RUNNING:
                return
            info = replace(info, phase=phase)
            self._turns[task_id] = info
        self._announce(info)

    def publish_delta(self, task_id: str, text: str) -> None:
        """Hand the accumulated partial answer to the UI sink. Never blocks a turn."""
        with self._lock:
            info = self._turns.get(task_id)
        sink = self.on_delta
        if info is None or sink is None:
            return
        try:
            sink(info, text)
        except Exception:  # a page that will not render must not lose the answer
            logger.exception("delta delivery failed for turn %s", task_id)

    def get(self, task_id: str) -> TurnInfo | None:
        with self._lock:
            return self._turns.get(str(task_id))

    def running(self, conversation_id: str = "") -> tuple[TurnInfo, ...]:
        with self._lock:
            found = tuple(
                info
                for info in self._turns.values()
                if info.state == STATE_RUNNING
                and (not conversation_id or info.conversation_id == conversation_id)
            )
        return found

    def queued(self, conversation_id: str = "") -> tuple[TurnInfo, ...]:
        """Waiting turns, oldest request first -- the stack the panel draws."""
        with self._lock:
            order = (
                list(self._pending.get(conversation_id) or [])
                if conversation_id
                else [task_id for line in self._pending.values() for task_id in line]
            )
            found = tuple(
                self._turns[task_id]
                for task_id in order
                if self._turns.get(task_id, None) is not None
                and self._turns[task_id].state == STATE_QUEUED
            )
        return found

    def waiting(self, conversation_id: str = "") -> tuple[TurnInfo, ...]:
        """Everything that still owes somebody an answer, started or not.

        This is what 「思考中」 should follow. Off it onto ``running()`` alone is the bug
        where a second question clears the card the instant it is typed, because the
        first answer is still on screen and the new turn has no thread yet.
        """
        with self._lock:
            found = tuple(
                info
                for info in self._turns.values()
                if info.state in (STATE_RUNNING, STATE_QUEUED)
                and (not conversation_id or info.conversation_id == conversation_id)
            )
        return found

    def _running_locked(self, conversation_id: str) -> bool:
        """Whether a thread is answering for that conversation right now. Caller holds the lock.

        One check for both doors -- ``start`` asks it to decide whether to queue, ``_drain``
        asks it to decide whether the line may move -- and it is enough because a line can
        only exist while something is running: the head is flipped to running under the
        same lock that removed it from the line, so there is no instant where a question is
        waiting and the conversation is free.
        """
        return any(
            info.conversation_id == conversation_id and info.state == STATE_RUNNING
            for info in self._turns.values()
        )

    def _cancel_of(self, task_id: str) -> threading.Event:
        """The turn's own cancel flag, created on demand so it is never ``None``."""
        with self._lock:
            found = self._cancel.get(task_id)
            if found is None:
                found = threading.Event()
                self._cancel[task_id] = found
            return found

    def recent(self, limit: int = 20) -> tuple[TurnInfo, ...]:
        """Newest first, running included -- what the 任务 strip draws."""
        cap = max(1, int(limit))
        with self._lock:
            picked = self._order[-cap:][::-1]
            return tuple(self._turns[task_id] for task_id in picked if task_id in self._turns)

    def _finish(
        self,
        task_id: str,
        state: str,
        *,
        error: str = "",
        answer_chars: int = 0,
        tools_used: tuple[str, ...] = (),
    ) -> None:
        with self._lock:
            info = self._turns.get(task_id)
            if info is None:
                return
            info = replace(
                info,
                state=state,
                phase="",
                error=error,
                answer_chars=answer_chars,
                tools_used=tools_used,
                finished_at=format_timestamp(utc_now()),
            )
            self._turns[task_id] = info
            self._staged.pop(task_id, None)
            begun = self._begun.pop(task_id, None)
        if begun is not None:
            # Anyone blocked in :meth:`wait` for their turn to begin has to learn it
            # never will, or a synchronous caller sits out its whole timeout.
            begun.set()
        self._announce(info)
        self._drain(info.conversation_id)
        # Last, not first: ``wait`` joins whatever thread the table still lists, and a
        # caller that was told "finished" before the next question in line had been
        # handed its thread would read a queue that had already drained.
        with self._lock:
            self._threads.pop(task_id, None)

    def _announce(self, info: TurnInfo) -> None:
        listener = self.on_update
        if listener is None:
            return
        try:
            listener(info)
        except Exception:
            logger.exception("turn update listener failed; the panel may lag one beat")

    def _trim_locked(self) -> None:
        """Drop the oldest finished turns.

        A turn that is still running -- or still waiting to -- is never dropped: its
        cancel flag and its answer have to stay addressable, and forgetting a queued one
        would silently swallow a question the operator can see stacked in the panel. So
        a table full of live work is allowed to sit over the cap rather than orphan it.
        """
        if len(self._order) <= MAX_KEPT:
            return
        for task_id in list(self._order):
            if len(self._order) <= MAX_KEPT:
                break
            info = self._turns.get(task_id)
            if info is None or info.state in (STATE_RUNNING, STATE_QUEUED):
                continue
            self._order.remove(task_id)
            self._turns.pop(task_id, None)
            self._cancel.pop(task_id, None)
            self._threads.pop(task_id, None)
            self._staged.pop(task_id, None)
            self._begun.pop(task_id, None)
            if info is not None:
                line = self._pending.get(info.conversation_id) or []
                if task_id in line:
                    line.remove(task_id)
                if not line:
                    self._pending.pop(info.conversation_id, None)


__all__ = [
    "KIND_CHAT",
    "KIND_ROUNDTABLE",
    "STATE_CANCELLED",
    "STATE_DONE",
    "STATE_FAILED",
    "STATE_QUEUED",
    "STATE_RUNNING",
    "TurnContext",
    "TurnInfo",
    "TurnRegistry",
]
