"""Chat service: the text door the desktop HUD types through.

Deliberately narrow in one direction and wide in another. It still does no
speech recognition and no synthesis — the voice stack needs roughly 3 GB and
tens of seconds to load, and WebView2 refuses to play a local audio file from a
``file://`` page, so speech stays on the voice path where
:class:`~jarvis.orchestration.voice_pipeline.VoicePipeline` plays it straight out
of the engine.

What it does now, in order, is the whole assistant:

1. **Recall** what is known about this user, and put the preferences and top
   facts in front of the model.
2. **Retrieve** evidence from the knowledge base and attach it to the question.
3. **Let the model call tools**, feeding results back until it answers.
4. **Observe** the turn, so the next question is answered by something that
   remembers this one.

Every one of those is optional and injected as a provider. With none of them
supplied, ``ask`` is exactly the plain chat call it was in phase 17 — which is
what keeps the phase-17 tests, and a text-only install, working unchanged.

:meth:`ask` never raises. It runs on pywebview's JS-API thread, and an exception
there surfaces to the page as an opaque bridge failure -- the one place a question
goes missing with no trace of why.

Several conversations, several turns at once
--------------------------------------------
A conversation is a :class:`~jarvis.app.conversation.Conversation` with its own replay
list, its own citation list and -- when the operator points it somewhere specific -- its
own provider and model. Two questions in flight therefore cannot see each other's state,
which they did when all three of those lived in fields on this service.

Two more callables exist for the same reason: ``client_for`` answers "(provider, model) →
client" so a turn can be sent to a model that is not the one the window last picked, and
``tuning_for`` answers "(provider, model) → how hard that model should think". Both are
optional; without them every turn behaves exactly as it did when there was one model and
one conversation.
"""

from __future__ import annotations

import json
import logging
import threading
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from dataclasses import replace as _dataclass_replace
from typing import TYPE_CHECKING

from jarvis.app.collaboration import MODE_TABLE, Collaboration
from jarvis.app.conversation import (
    STATUS_DONE,
    STATUS_FAILED,
    STATUS_IDLE,
    Conversation,
)
from jarvis.app.round_table import Seat, Speech, refuse_pictures
from jarvis.core.exceptions import JarvisError
from jarvis.llm.client import StreamingClient
from jarvis.llm.types import ChatMessage, ChatResponse, GenerationOptions, Role
from jarvis.prompt import render_prompt
from jarvis.prompt.templates import HUD_ASSISTANT

if TYPE_CHECKING:  # pragma: no cover - typing only
    from jarvis.app.transcript_service import TranscriptService
    from jarvis.knowledge import KnowledgeService
    from jarvis.llm.client import LlmClient
    from jarvis.memory import MemoryService
    from jarvis.tools import ToolService

logger = logging.getLogger("jarvis.app.chat_service")

MAX_ATTACHMENTS: int = 4
"""How many files one turn may carry. More than a handful stops being context and
starts being a file transfer with a chat shaped hole in it."""

MAX_IMAGE_DATA_CHARS: int = 400_000
"""A downscaled thumbnail is tens of kilobytes of base64; anything near this cap is
somebody handing over an original, which belongs in a file, not a conversation."""

UNSAVED_SESSION: str = "hud"
"""The session id a chat without a transcript store runs under, and the one a chat
*with* a store uses until its first turn opens a real conversation."""

HUD_SYSTEM_PROMPT: str = HUD_ASSISTANT.body
"""The *shipped* persona text, exported for tests and for callers that want the
default regardless of configuration.

The live persona comes from ``render_prompt("hud_assistant")``, which honours a
``prompt.overrides`` entry. The two differ only when an operator has replaced
the wording, and keeping this constant around is what lets
``test_the_persona_does_not_claim_unperformed_actions`` assert against the
wording JARVIS ships rather than against whatever a local config happens to say.

Written for reading, not for speaking: the voice prompt in :mod:`jarvis.app.demo`
caps replies at ~40 characters because every Markdown mark turns into an audible
pause, while a typed answer has no such limit. So the constraint here is the
opposite one -- don't be lazy, and don't invent capabilities, because the
operator is looking at a panel that really can delete files.
"""

MAX_TOOL_ROUNDS_DEFAULT: int = 4
"""How many times the model may call tools before it must answer.

Bounded on purpose: a model that keeps calling the same tool would otherwise
hold the JS bridge thread for as long as it likes, and the page would simply
stop responding with no explanation.
"""


@dataclass(frozen=True, slots=True)
class ThinkingRequest:
    """What the operator asked for on the thinking axis, read fresh every turn.

    A provider rather than a value because the settings panel can change it while the
    window is open, and "save, then restart to see the effect" is what this round
    exists to delete.
    """

    enabled: bool
    """Ask the model to return its reasoning alongside the answer."""

    budget: int
    """How many tokens of reasoning that costs. Only meaningful when ``enabled``.

    A real budget, not a label: the endpoint behind the qwenai key truncates
    ``reasoning_content`` at this number while still answering in full, so the value
    in the panel is checkable against ``reasoning_tokens`` in the reply.
    """


@dataclass(frozen=True, slots=True)
class ConversationTuning:
    """How one provider/model pair is configured to answer.

    Both knobs travel together because they are read from the same stored row
    (``provider\\x00model``) and because a turn that got the thinking budget from the
    right row but the context size from the global setting would replay the wrong
    number of exchanges to a model it just asked to think harder.

    Either half may be ``None``, meaning "nothing recorded for this pair, use the
    global value" -- which is what a model nobody has touched in the panel looks like.
    """

    thinking: ThinkingRequest | None = None
    history_turns: int | None = None


@dataclass(frozen=True, slots=True)
class _Pair:
    """Which provider/model a turn will ask, and whether somebody *chose* that.

    The distinction is not pedantic: a conversation that named ``deepseek`` must be told
    when that client cannot be built, because answering from another model would
    contradict the label on the tab. A conversation that inherited the window's choice
    has no such promise attached to it, and must keep answering the way it did before
    per-model clients existed at all.
    """

    provider: str
    model: str
    chosen: bool

    @property
    def label(self) -> str:
        return f"{self.provider}/{self.model}" if self.provider else self.model


@dataclass(frozen=True, slots=True)
class _Turn:
    """What one model drive produced: the answer, the tools it needed, and the trace."""

    text: str
    tools_used: tuple[str, ...] = ()
    reasoning: str = ""
    reasoning_tokens: int | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    cancelled: bool = False
    """The answer stopped of its own accord because the operator asked it to.

    Kept separate from ``error`` because a cancellation is not a fault: the partial text
    is real, belongs in the history, and must not be shown as 「失败」.
    """

    @property
    def answered(self) -> bool:
        """Whether this turn should be treated as a finished answer."""
        return bool(self.text.strip())


def _reasoning_tokens(response: ChatResponse) -> int | None:
    return response.usage.reasoning_tokens if response.usage is not None else None


def _prompt_tokens(response: ChatResponse) -> int | None:
    return response.usage.prompt_tokens if response.usage is not None else None


def _completion_tokens(response: ChatResponse) -> int | None:
    return response.usage.completion_tokens if response.usage is not None else None


def _never() -> bool:
    """The stop predicate a caller gets when nobody can stop this turn.

    A default ``None`` checked at every boundary would be thirteen ``if x is not None``
    branches; one shared no-op is the same cost with one place to read.
    """
    return False


def _sum_or_none(values: Sequence[int | None]) -> int | None:
    """Total tokens thought, or ``None`` when not one round reported a number.

    Summing a list of unknowns into ``0`` would turn "the provider stayed quiet" into
    "she did not think", and those two look identical on screen while meaning opposite
    things about whether the budget setting is doing anything.
    """
    known = [value for value in values if value is not None]
    return sum(known) if known else None


@dataclass(frozen=True, slots=True)
class ChatReply:
    """One answered question, in the shape the page renders."""

    question: str
    answer: str
    error: str = ""
    """Non-empty when the turn failed; shown inline instead of swallowed."""

    tools_used: tuple[str, ...] = ()
    """Tools the model actually invoked while answering."""

    sources: tuple[str, ...] = ()
    """Knowledge-base citations, e.g. ``手册.pdf 第 3 段``."""

    grounded: bool = False
    """Whether the answer cites retrieved evidence rather than the model's own
    knowledge. The CLI prints this; the HUD does not yet."""

    reasoning: str = ""
    """What the model said it thought about, when thinking is switched on.

    Empty is the honest default: a provider that was never asked returns nothing, and
    the page must not draw a 「思考过程」 box around a blank.
    """

    reasoning_tokens: int | None = None
    """Tokens the reasoning cost, or ``None`` when the provider did not say.

    ``None`` is not zero. The panel reads this to show that the budget the operator
    set is actually being applied; showing 0 for a silent provider would look like
    the setting working when nothing was measured at all.
    """

    provider: str = ""
    model: str = ""
    """Which model answered. Recorded rather than inferred: with several conversations
    and a round table, "she said" is not enough to know who said it."""

    conversation: str = ""
    """The conversation this turn belongs to, echoed so the page can file the answer
    under the right tab even if it switched tabs while waiting."""

    task_id: str = ""
    """The registry entry that ran this turn, when there was one."""

    cancelled: bool = False
    """The operator stopped this turn. ``answer`` holds whatever got out before that."""

    record: str = ""
    """Everything said around a round table, in order. Empty for a normal turn.

    Separate from ``answer`` because the two have different readers: the answer is what
    gets spoken, stored and replayed into the next turn, while the record exists so the
    operator can see who argued what. Replaying it would double the context of every
    follow-up question for a benefit nobody asked for twice.
    """

    def to_dict(self) -> dict[str, object]:
        """What ``chat_ask`` hands to the page.

        Deliberately *not* extended with ``sources``/``grounded``/``reasoning_tokens``:
        the transcript is pushed through the state bridge, and a richer payload here
        means the bundle has to be rebuilt in lockstep with Python. ``reasoning`` is
        the one addition, because the thinking chain has to reach the page that draws
        it -- and it arrives as a plain string so an older bundle simply ignores it
        rather than rendering blank rows.

        ``model``/``conversation``/``cancelled`` are the new ones, and they are here
        because the page cannot reconstruct them: three tabs and a round table all
        answer into the same panel.
        """
        return {
            "question": self.question,
            "answer": self.answer,
            "error": self.error,
            "reasoning": self.reasoning,
            "record": self.record,
            "model": self.model,
            "provider": self.provider,
            "conversation": self.conversation,
            "task_id": self.task_id,
            "cancelled": self.cancelled,
        }


def _as_count(value: object) -> int:
    """A row count from the store, or 0. Anything that is not a number is not a count."""
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return value if value >= 0 else 0


def _as_size(value: object) -> int:
    """A byte count from the page, or 0. bool is an int in Python and is not a size."""
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return value if 0 <= value <= 10 * 1024**3 else 0


def _clean_attachments(raw: Sequence[Mapping[str, object]]) -> tuple[dict[str, object], ...]:
    """What the page sent, reduced to what is safe to store and to show.

    The page is a boundary: names and sizes arrive as whatever JavaScript produced,
    and image data arrives as a data URL that will be stored and later re-rendered.
    So each field is typed and capped here, and anything unrecognisable is dropped
    rather than guessed at.
    """
    kept: list[dict[str, object]] = []
    for entry in raw:
        if not isinstance(entry, Mapping) or len(kept) >= MAX_ATTACHMENTS:
            continue
        name = str(entry.get("name") or "未命名")[:120]
        kind = entry.get("kind")
        kind = kind if kind in ("image", "video", "file") else "file"
        mime = str(entry.get("mime") or "application/octet-stream")[:80]
        size = entry.get("size")
        size = _as_size(size)
        clean: dict[str, object] = {"name": name, "kind": kind, "mime": mime, "size": size}
        data = entry.get("data")
        if (
            kind == "image"
            and isinstance(data, str)
            and data.startswith("data:image/")
            and len(data) <= MAX_IMAGE_DATA_CHARS
        ):
            clean["data"] = data
        kept.append(clean)
    return tuple(kept)


def _images_of(attachments: Sequence[Mapping[str, object]]) -> tuple[str, ...]:
    """The pictures that survived :func:`_clean_attachments`, in the order given.

    A separate function because two things have to agree on this list and cannot see each
    other's mind: the wording the model reads, and the ``images`` field on the message. An
    image dropped for size must be described as dropped in *both*, and the second one is
    computed from the first.
    """
    return tuple(
        str(entry["data"])
        for entry in attachments
        if entry.get("kind") == "image" and entry.get("data")
    )


def _with_attachment_note(
    question: str, attachments: Sequence[Mapping[str, object]], *, sees_images: bool = False
) -> str:
    """Name what was handed over, in the text the model reads.

    Nothing about a picture is claimed here that the request does not carry. A model told
    「缩略图已随消息提供」 will describe pixels it was never given, and before the content
    array existed that sentence was true of *every* picture this app sent -- the
    ``images`` field was filled in and then dropped on the floor by the serializer. The
    rule did not change; what changed is which branch it takes: a picture that is really
    on the wire is announced as one, and an oversize one is still only a filename.

    ``sees_images`` is the whole question, so it is a parameter rather than something
    worked out in here: whether the same file reaches the model depends on the model, and
    this function's job is to say whichever way it went.
    """
    if not attachments:
        return question
    lines = [question, ""]
    for entry in attachments:
        note = (
            f"[附件] {entry.get('name')} · {entry.get('mime')} · {_as_size(entry.get('size'))} 字节"
        )
        if entry.get("kind") != "image":
            note += " · 本工具不解析这类文件的内容"
        elif not entry.get("data"):
            note += " · 这张图超过了随请求发送的上限，只在界面上，你看不到画面"
        elif not sees_images:
            note += " · 图只在界面上，没有随请求发出，你看不到画面"
        else:
            note += " · 图片已随本条消息附上，请按画面内容回答"
        lines.append(note)
    return "\n".join(lines)


class ChatService:
    """Answers typed turns, with memory, retrieval and tools when available."""

    name = "chat"

    def __init__(
        self,
        client_provider: Callable[[], LlmClient],
        *,
        max_history_turns: int = 10,
        system_prompt: str | None = None,
        memory_provider: Callable[[], MemoryService] | None = None,
        knowledge_provider: Callable[[], KnowledgeService] | None = None,
        tool_provider: Callable[[], ToolService] | None = None,
        transcript: TranscriptService | None = None,
        session_id: str = "hud",
        max_tool_rounds: int = MAX_TOOL_ROUNDS_DEFAULT,
        thinking_provider: Callable[[], ThinkingRequest] | None = None,
        history_turns_provider: Callable[[], int] | None = None,
        client_for: Callable[[str, str], LlmClient] | None = None,
        tuning_for: Callable[[str, str], ConversationTuning | None] | None = None,
        pair_provider: Callable[[], tuple[str, str]] | None = None,
        collaboration: Collaboration | None = None,
        vision_for: Callable[[str, str], bool | None] | None = None,
    ) -> None:
        """Create the service.

        Args:
            client_provider: Yields the live LLM client. A provider, not a client:
                components are registered before ``LlmService`` has built one, and
                reading it early raises.
            max_history_turns: How many past exchanges are replayed to the model.
                Long enough for follow-up questions, short enough that a page left
                open all day does not grow the request without bound. Superseded per
                turn by ``history_turns_provider`` when one is supplied.
            system_prompt: The persona the answers come from.
            memory_provider: Long-term memory, or ``None`` for a stateless chat.
            knowledge_provider: The RAG knowledge base, or ``None``.
            tool_provider: The tool registry, or ``None`` for chat-only.
            transcript: Where conversations are kept across restarts. ``None``
                keeps the old behaviour exactly: one in-memory conversation that
                清空 or a quit ends for good.
            session_id: Which conversation to record turns under.
            max_tool_rounds: Cap on tool-calling iterations per turn.
            thinking_provider: The thinking knobs for the next request, or ``None``
                to leave every request exactly as it was before this field existed --
                which is what a provider that has no reasoning to show wants.
            history_turns_provider: How many exchanges to replay, read every turn so
                the settings panel's 上下文轮数 takes effect on the next question
                rather than after a restart.
            client_for: Builds (or fetches) the client for one provider and model.
                ``None`` means every conversation answers from ``client_provider``,
                which is the pre-multi-model behaviour.
            tuning_for: How hard one provider/model is supposed to think. Preferred
                over ``thinking_provider`` when both a provider and a model are known,
                because one global pair cannot describe three models answering at once.
            pair_provider: The provider/model the window last picked. A conversation
                that names neither inherits it, which is what keeps 设置 as the place a
                single-tab user configures and the tab strip from having to restate it.
        """
        self._client_provider = client_provider
        self._max_history = max(0, max_history_turns)
        # ``None`` means "whatever the registry currently says", which is what
        # lets a config override take effect without rebuilding the service.
        self._system_prompt = system_prompt
        self._memory_provider = memory_provider
        self._knowledge_provider = knowledge_provider
        self._tool_provider = tool_provider
        self._transcript = transcript
        self._max_tool_rounds = max(0, max_tool_rounds)
        self._thinking_provider = thinking_provider
        self._history_turns_provider = history_turns_provider
        self._client_for = client_for
        self._tuning_for = tuning_for
        self._pair_provider = pair_provider
        self._vision_for = vision_for
        """Whether this pair has ever been shown a picture it could describe.

        ``None`` from the callable means "nobody has looked", which attaches anyway: a
        provider that cannot see answers with an error the operator can read, and that is
        a truer outcome than a rule invented here silently throwing the screenshot away.
        """
        self._collaboration = collaboration
        """The three collaboration shapes, or ``None`` when this process is single-model.

        Injected rather than built in here: it needs a client factory for arbitrary
        pairs, which is the same thing ``client_for`` was handed, and a chat service that
        constructed one would be a service that always carries a second way to reach the
        network.
        """
        self._conversations: dict[str, Conversation] = {}
        self._guard = threading.Lock()
        """Guards the conversation *table*, never a conversation's contents.

        Split that way on purpose: the replay list has its own lock inside
        :class:`Conversation`, so a turn that holds a model open for twenty seconds does
        not stop somebody else listing or opening tabs.
        """
        self._active = session_id or UNSAVED_SESSION
        self._started = False
        self._touch(self._active)

    # ------------------------------------------------------------------
    # Context window
    # ------------------------------------------------------------------

    @property
    def max_history_turns(self) -> int:
        """How many past exchanges the next answer will be built from.

        Read per turn because the panel can change it: a value cached at construction
        is a setting that appears to save and does nothing.
        """
        return self._history_limit(self._global_pair())

    def _history_limit(self, pair: _Pair) -> int:
        """The context size for one provider/model pair, falling back to the global one.

        Per pair rather than per process because the two knobs in the chat header sit
        next to a model picker: three tabs on three models are meant to be configured
        independently, and a single global number silently overwrites two of them.
        """
        if pair.provider and pair.model and self._tuning_for is not None:
            try:
                tuning = self._tuning_for(pair.provider, pair.model)
            except Exception:
                logger.exception("could not read the tuning for %s", pair.label)
            else:
                if tuning is not None and tuning.history_turns is not None:
                    value = int(tuning.history_turns)
                    if value >= 0:
                        return value
        if self._history_turns_provider is None:
            return self._max_history
        try:
            value = int(self._history_turns_provider())
        except (TypeError, ValueError):
            return self._max_history
        return value if value >= 0 else self._max_history

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Mark ready. The client is fetched per turn, so nothing loads here."""
        self._started = True
        logger.info(
            "chat service ready (memory=%s, knowledge=%s, tools=%s)",
            self._memory_provider is not None,
            self._knowledge_provider is not None,
            self._tool_provider is not None,
        )

    def stop(self) -> None:
        """Drop every in-memory conversation. Stored ones are untouched: shutting the
        app down is not an instruction about anybody's history."""
        self._started = False
        with self._guard:
            self._conversations.clear()
            self._active = UNSAVED_SESSION

    @property
    def running(self) -> bool:
        return self._started

    # ------------------------------------------------------------------
    # The conversation table
    # ------------------------------------------------------------------

    def _touch(self, conversation_id: str, *, title: str = "") -> Conversation:
        """The conversation with this id, created empty if it does not exist yet."""
        key = conversation_id or UNSAVED_SESSION
        with self._guard:
            found = self._conversations.get(key)
            if found is None:
                found = Conversation(key, title=title)
                self._conversations[key] = found
            return found

    def conversation(self, conversation_id: str = "") -> Conversation:
        """The conversation a call is about: the named one, or the one on screen."""
        target = str(conversation_id or "")
        if not target:
            with self._guard:
                target = self._active
        return self._touch(target)

    def conversations(self) -> list[dict[str, object]]:
        """Every conversation this process knows, in the order the tabs were opened.

        Turn counts come from the store when the store has more than memory does, so a
        conversation opened before the app restarted does not claim to be empty.
        """
        with self._guard:
            keys = list(self._conversations)
            active = self._active
        stored: dict[str, Mapping[str, object]] = {}
        transcript = self._transcript
        if transcript is not None:
            stored = {str(row["id"]): row for row in transcript.list_sessions(limit=200)}
        rows: list[dict[str, object]] = []
        for key in keys:
            card = self._touch(key).snapshot()
            record = stored.get(key) or {}
            card["active"] = key == active
            card["title"] = card["title"] or str(record.get("title") or "")
            card["stored_turns"] = _as_count(record.get("turns"))
            card["updated_at"] = str(record.get("updated_at") or "")
            rows.append(card)
        return rows

    def focus(self, conversation_id: str) -> dict[str, object]:
        """Show one conversation in the panel. Its stored turns are loaded on demand."""
        conversation = self._touch(str(conversation_id or ""))
        if self._transcript is not None:
            with conversation.lock:
                empty = not conversation.history
            if empty:
                self._load_from_transcript(conversation)
        with self._guard:
            self._active = conversation.id
        return {"ok": True, "conversation": conversation.id, "conversations": self.conversations()}

    def new_conversation(
        self, *, provider: str = "", model: str = "", title: str = ""
    ) -> dict[str, object]:
        """Open a tab. Nothing is written to the store until its first question, so a
        click that goes nowhere leaves no empty conversation to clean up afterwards."""
        key = uuid.uuid4().hex
        conversation = self._touch(key, title=title)
        conversation.provider = provider
        conversation.model = model
        with self._guard:
            self._active = key
        return {"ok": True, "conversation": key, "conversations": self.conversations()}

    def set_conversation_model(
        self, conversation_id: str, provider: str, model: str
    ) -> dict[str, object]:
        """Point one tab at one model. Other tabs keep theirs -- that is the point."""
        conversation = self._touch(str(conversation_id or ""))
        named = str(provider or "").strip()
        if named and self._client_for is None:
            return {
                "ok": False,
                "error": "这个进程没有按模型建客户端的能力，只能跟随全局选择",
                "conversation": conversation.id,
            }
        with conversation.lock:
            conversation.provider = named
            conversation.model = str(model or "").strip()
        answer: dict[str, object] = {
            "ok": True,
            "error": "",
            "conversation": conversation.id,
        }
        answer.update(self._pair(conversation))
        return answer

    def _pair(self, conversation: Conversation) -> dict[str, str]:
        """The (provider, model) this conversation will actually use.

        Empty means "follow the window's choice", which is what a single-tab user set
        in 设置 and expects a new tab to inherit rather than lose.
        """
        with conversation.lock:
            return {"provider": conversation.provider, "model": conversation.model}

    def _resolve_pair(
        self, conversation: Conversation, provider: str = "", model: str = ""
    ) -> _Pair:
        """Turn a call's override and the tab's own choice into a concrete pair.

        ``chosen`` is true only when the tab or the call named it, which is what decides
        between "ask this exact model" and "ask whatever the window picked" -- and, on
        failure, between reporting a real problem and carrying on unchanged.
        """
        named = str(provider or "").strip()
        chosen_model = str(model or "").strip()
        stored = self._pair(conversation)
        explicit = bool(named or chosen_model) or bool(stored["provider"] or stored["model"])
        if not named:
            named = stored["provider"]
        if not chosen_model:
            chosen_model = stored["model"]
        if named and chosen_model:
            return _Pair(named, chosen_model, explicit)
        fallback = self._global_pair()
        return _Pair(named or fallback.provider, chosen_model or fallback.model, explicit)

    def _global_pair(self) -> _Pair:
        """The provider/model the window last picked, if anyone can say."""
        if self._pair_provider is None:
            return _Pair("", "", False)
        try:
            value = self._pair_provider()
        except Exception:
            logger.exception("could not read the selected model; following the conversation")
            return _Pair("", "", False)
        return _Pair(str(value[0] or ""), str(value[1] or ""), False)

    # ------------------------------------------------------------------
    # Turns
    # ------------------------------------------------------------------

    def ask(
        self,
        text: str,
        *,
        remembered: bool = True,
        attachments: Sequence[Mapping[str, object]] = (),
        conversation: str = "",
        provider: str = "",
        model: str = "",
        task_id: str = "",
        on_delta: Callable[[str], None] | None = None,
        on_phase: Callable[[str], None] | None = None,
        should_stop: Callable[[], bool] | None = None,
    ) -> ChatReply:
        """Answer one question. Never raises: see the module docstring.

        Args:
            text: What to ask.
            remembered: Whether the exchange goes into the running conversation
                and the long-term memory. ``False`` is for turns nobody *said* --
                a machine-written digest handed over by a button -- which would
                otherwise sit in the history and steer every later answer.
            conversation: Which conversation this belongs to. Empty means the one the
                panel is showing. Every piece of per-turn state -- the replay list, the
                citations, the model -- is keyed off this, which is what lets a second
                tab ask its own question while the first is still waiting.
            provider: Optional per-turn model override, for a round-table seat or a tab
                pointed somewhere specific. Paired with ``model``.
            model: The model id to use with ``provider``.
            task_id: The registry entry running this turn, so the answer can be traced
                back to the button press that started it.
            on_delta: Called with the whole partial answer as it grows. Supplying it is
                what switches the request to streaming, and streaming is what makes
                停止 mean now rather than "when this response happens to finish".
            should_stop: Checked at every boundary the turn can be interrupted at.
            on_phase: Told what the turn is doing right now -- thinking, calling a tool, a
                named seat speaking. The conversation records the phase either way; this is
                what moves the strip *now*, instead of whenever a snapshot happens to get
                pushed, which is the difference between a status line and a fossil.
        """
        question = text.strip()
        if not question:
            return ChatReply("", "", "问题是空的")
        if not self._started:
            return ChatReply(question, "", "对话服务未启动")
        chat = self.conversation(conversation)
        kept = _clean_attachments(attachments)
        pair = self._resolve_pair(chat, provider, model)
        with chat.lock:
            chat.begin_turn(task_id)
        if remembered:
            # Recorded before the model is asked, not after it answers: what the operator
            # typed is a fact about the conversation regardless of whether anything came
            # back, and a transcript that loses the question on a failed turn makes their
            # own asking deniable.
            self._record_question(chat, question, kept)
        try:
            client = self._client_for_pair(pair)
        except JarvisError as exc:
            logger.error("chat has no model client: %s", exc)
            chat.end_turn(status=STATUS_FAILED, error=str(exc))
            return ChatReply(question, "", str(exc), conversation=chat.id, task_id=task_id)
        except Exception as exc:  # a provider bug must still reach the page
            logger.exception("chat could not read the model client")
            detail = f"{type(exc).__name__}: {exc}"
            chat.end_turn(status=STATUS_FAILED, error=detail)
            return ChatReply(
                question,
                "",
                detail,
                conversation=chat.id,
                provider=pair.provider,
                model=pair.model,
                task_id=task_id,
            )

        try:
            system = self._build_system_prompt()
            images = self._attachable(_images_of(kept), pair)
            user_content, sources = self._build_user_message(
                _with_attachment_note(question, kept, sees_images=bool(images))
            )
            messages = self._initial_messages(system, user_content, chat=chat, history=remembered)
            if messages and images:
                messages[-1] = _dataclass_replace(messages[-1], images=images)
            tools = self._tool_schemas()
            turn = self._run(
                client,
                messages,
                tools,
                chat=chat,
                pair=pair,
                task_id=task_id,
                on_delta=on_delta,
                on_phase=on_phase,
                should_stop=should_stop,
            )
        except JarvisError as exc:
            logger.error("chat turn failed: %s", exc)
            chat.end_turn(status=STATUS_FAILED, error=str(exc))
            return ChatReply(
                question,
                "",
                str(exc),
                conversation=chat.id,
                provider=pair.provider,
                model=pair.model,
                task_id=task_id,
            )
        except Exception as exc:
            logger.exception("chat turn failed unexpectedly")
            detail = f"{type(exc).__name__}: {exc}"
            chat.end_turn(status=STATUS_FAILED, error=detail)
            return ChatReply(
                question,
                "",
                detail,
                conversation=chat.id,
                provider=pair.provider,
                model=pair.model,
                task_id=task_id,
            )

        answer = turn.text.strip()
        if not answer and not turn.cancelled:
            chat.end_turn(status=STATUS_FAILED, error="模型没有返回内容")
            return ChatReply(
                question,
                "",
                "模型没有返回内容",
                conversation=chat.id,
                provider=pair.provider,
                model=pair.model,
                task_id=task_id,
            )
        if remembered and answer:
            self._remember(chat, question, answer)
            self._record_answer(chat, answer, model=pair.model, task_id=task_id)
            self._observe(chat.id, question, answer)
        chat.end_turn(
            status=STATUS_DONE if answer else STATUS_IDLE,
            error="已中断" if turn.cancelled and not answer else "",
            sources=sources,
        )
        return ChatReply(
            question,
            answer,
            tools_used=turn.tools_used,
            sources=sources,
            grounded=bool(sources),
            reasoning=turn.reasoning,
            reasoning_tokens=turn.reasoning_tokens,
            provider=pair.provider,
            model=pair.model,
            conversation=chat.id,
            task_id=task_id,
            cancelled=turn.cancelled,
        )

    def ask_together(
        self,
        text: str,
        seats: Sequence[object],
        *,
        mode: str = MODE_TABLE,
        rounds: int = 2,
        conversation: str = "",
        attachments: Sequence[Mapping[str, object]] = (),
        task_id: str = "",
        remembered: bool = True,
        on_speech: Callable[[Speech], None] | None = None,
        on_floor: Callable[[Seat, int], None] | None = None,
        on_delta: Callable[[str], None] | None = None,
        on_phase: Callable[[str], None] | None = None,
        should_stop: Callable[[], bool] | None = None,
    ) -> ChatReply:
        """Ask one question with several models. Never raises, same as :meth:`ask`.

        ``mode`` picks the shape (``table`` / ``boss`` / ``vote``); everything after the
        engines hand back -- transcript, memory, citations, the answer's signature -- is
        this method's job and shared by all three, because a second copy of "what gets
        stored" is where a vote and a round table would start behaving differently in the
        history and nobody could say why.


        This is the same conversation, the same transcript and the same memory as a normal
        turn -- only the asking is shared. What gets stored is the merged answer: the
        record of who said what reaches the page (``ChatReply.record``) but is not replayed
        into the next question, because three models' opinions in the history would be the
        largest thing in every request after it and the operator already has the conclusion.

        Retrieval happens once, here, rather than per seat: the evidence is part of the
        question, and a table where two chairs read the manual and one did not is arguing
        about a different problem than the one that was asked.
        """
        question = text.strip()
        if not question:
            return ChatReply("", "", "问题是空的")
        if self._collaboration is None:
            return ChatReply(question, "", "协作模式没有接上模型服务")
        chat = self.conversation(conversation)
        kept = _clean_attachments(attachments)
        chosen = [seat for seat in (Seat.of(item) for item in seats) if seat is not None]
        if not chosen:
            return ChatReply(question, "", "至少选一个模型才开得了这一桌", conversation=chat.id)
        with chat.lock:
            chat.begin_turn(task_id, phase=chat.speaking_phase(chosen[0].label))
        note = refuse_pictures(chosen[0], _images_of(kept))
        if note:
            logger.info("round table refused %d picture(s): %s", len(kept), note)
            chat.end_turn(status=STATUS_FAILED, error=note)
            return ChatReply(question, "", note, conversation=chat.id, task_id=task_id)
        if remembered:
            self._record_question(chat, question, kept)

        asked, sources = self._build_user_message(question)
        host = _Pair(chosen[0].provider, chosen[0].model, True)
        history = chat.replay(limit=self._history_limit(host)) if remembered else ()
        result = self._collaboration.run(
            mode,
            asked,
            chosen,
            rounds=rounds,
            system=self._build_system_prompt(),
            history=tuple(history),
            task_id=task_id,
            on_speech=self._seat_listener(chat, on_speech, on_phase),
            on_floor=self._floor_listener(chat, on_floor, on_phase),
            on_delta=on_delta,
            should_stop=should_stop,
        )
        answer = result.answer.strip()
        if remembered and answer:
            self._remember(chat, question, answer)
            self._record_answer(chat, answer, model=chosen[0].model, task_id=task_id)
            self._observe(chat.id, question, answer)
        chat.end_turn(
            status=STATUS_DONE if answer else STATUS_FAILED if result.error else STATUS_IDLE,
            error=result.error or ("已中断" if result.stopped else ""),
            sources=sources,
        )
        return ChatReply(
            question,
            answer,
            result.error,
            sources=sources,
            grounded=bool(sources),
            provider=chosen[0].provider,
            model=chosen[0].model,
            conversation=chat.id,
            task_id=task_id,
            cancelled=result.stopped,
            record=result.record,
        )

    def _seat_listener(
        self,
        chat: Conversation,
        on_speech: Callable[[Speech], None] | None,
        on_phase: Callable[[str], None] | None,
    ) -> Callable[[Speech], None]:
        """Advance the strip's line when a speech lands, then hand it on to the page.

        The phase is written here rather than inside the table because the table does not
        know which conversation it is running: several can be open, and a service that
        updated "the" conversation would update whichever one the panel happens to show.
        """

        def spoke(speech: Speech) -> None:
            self._phase(chat, chat.answering_phase(), on_phase)
            if on_speech is not None:
                on_speech(speech)

        return spoke

    def _floor_listener(
        self,
        chat: Conversation,
        on_floor: Callable[[Seat, int], None] | None,
        on_phase: Callable[[str], None] | None,
    ) -> Callable[[Seat, int], None]:
        """Say who has the floor, on the strip and to the page."""

        def took_floor(seat: Seat, round_index: int) -> None:
            self._phase(chat, chat.speaking_phase(str(seat.label)), on_phase)
            if on_floor is not None:
                on_floor(seat, round_index)

        return took_floor

    def _attachable(self, images: tuple[str, ...], pair: _Pair) -> tuple[str, ...]:
        """The pictures this pair should actually be sent.

        A model that was measured not to take images still gets the question, and the
        attachment note then says the picture stayed on the page -- which is what makes
        the difference between a wrong answer and a lie. The measurement is what
        :class:`~jarvis.app.model_probe.ModelCaps` records when 设置 tests a model; before
        anyone has looked, the picture goes out and the provider's own reply decides.
        """
        if not images or self._vision_for is None:
            return images
        try:
            sees = self._vision_for(pair.provider, pair.model)
        except Exception:
            logger.exception("could not read this model's vision record; sending the picture")
            return images
        if sees is False:
            logger.info(
                "not sending %d picture(s): %s was measured as unable to see",
                len(images),
                pair.label,
            )
            return ()
        return images

    @staticmethod
    def _phase(chat: Conversation, phase: str, on_phase: Callable[[str], None] | None) -> None:
        """Record what the turn is doing, and tell whoever is watching.

        Two destinations because the readers differ: the conversation's own phase is what
        a later snapshot of the strip shows, while the callback is the only thing that can
        change the page *during* a turn. A phase that becomes visible only when the turn
        ends is a phase nobody sees.
        """
        chat.set_phase(phase)
        if on_phase is None:
            return
        try:
            on_phase(phase)
        except Exception:  # a status line must not cost the operator their answer
            logger.exception("could not report the turn's phase; ignoring")

    def _client_for_pair(self, pair: _Pair) -> LlmClient:
        """The client for one pair, or the one the window picked.

        A pair the tab or the call *named* is never silently downgraded: answering from
        another model while the tab says otherwise is worse than an error the operator
        can read. A pair that only came from the global choice carries no such promise,
        so it keeps using the shared client exactly as it did before per-model tabs
        existed -- which is what lets a console run, or any caller that never wired
        client_for, carry on working unchanged.
        """
        if pair.chosen and pair.provider:
            if self._client_for is None:
                raise JarvisError(f"这个进程不能按模型建客户端：{pair.label}")
            return self._client_for(pair.provider, pair.model)
        return self._client_provider()

    @property
    def history_length(self) -> int:
        """How many exchanges the active conversation is still replaying."""
        conversation = self.conversation()
        with conversation.lock:
            return len(conversation.history) // 2

    @property
    def session_id(self) -> str:
        """The conversation new turns are being written into."""
        with self._guard:
            return self._active

    # ------------------------------------------------------------------
    # Conversations, kept across restarts
    # ------------------------------------------------------------------

    def sessions(self) -> dict[str, object]:
        """The list the HUD's history popup draws, newest first."""
        transcript = self._transcript
        if transcript is None:
            return {"error": "对话历史未启用", "current": self.session_id, "sessions": []}
        return {
            "error": "",
            "current": self.session_id,
            "sessions": transcript.list_sessions(),
            "conversations": self.conversations(),
        }

    def session_messages(self, session_id: str) -> dict[str, object]:
        """Every turn of one conversation, oldest first, for the page to draw."""
        transcript = self._transcript
        if transcript is None:
            return {"error": "对话历史未启用", "current": self.session_id, "messages": []}
        return {
            "error": "",
            "current": self.session_id,
            "messages": transcript.whole_session(str(session_id)),
        }

    def new_session(self) -> dict[str, object]:
        """Open a blank conversation. The old one stays stored on disk."""
        self.new_conversation()
        return self.sessions()

    def switch_session(self, session_id: str) -> dict[str, object]:
        """Show a stored conversation, replaying its recent turns into the model."""
        self.focus(str(session_id))
        return self.sessions()

    def rename_session(self, session_id: str, title: str) -> dict[str, object]:
        transcript = self._transcript
        if transcript is None:
            return self.sessions()
        if not transcript.rename(str(session_id), str(title)):
            return {**self.sessions(), "error": "没有这个对话，或改名失败"}
        chat = self._touch(str(session_id))
        with chat.lock:
            chat.title = str(title)
        return self.sessions()

    def delete_session(self, session_id: str) -> dict[str, object]:
        """Delete one stored conversation. Deleting the open one opens a new one.

        This is the only path that discards anything, and it stays behind a button the
        operator presses -- including for a conversation that is mid-turn, whose thread
        will simply find its rows gone when it tries to record the answer.
        """
        transcript = self._transcript
        if transcript is None:
            return self.sessions()
        target = str(session_id)
        if not transcript.delete(target):
            return {**self.sessions(), "error": "没有这个对话"}
        with self._guard:
            self._conversations.pop(target, None)
            was_active = target == self._active
        if was_active:
            return self.new_session()
        return self.sessions()

    def append_turn(self, role: str, content: str, conversation: str = "") -> None:
        """One *spoken* turn, from the voice pipeline, into an open conversation.

        Spoken and typed turns land in the same session list because they are the
        same conversation from the operator's side of the glass; keeping them in
        two histories would make "what did we talk about" unanswerable.
        """
        transcript = self._transcript
        if transcript is None or role not in ("user", "assistant") or not content.strip():
            return
        chat = self.conversation(conversation)
        self._ensure_session(chat, content)
        transcript.append(chat.id, role, content)

    def _ensure_session(self, chat: Conversation, first_text: str) -> None:
        """Give a freshly opened tab a row, keeping the id it was opened with.

        The id never changes after this: the tab, its running task and its stored rows all
        carry it, and re-issuing one would leave the turn in flight writing into a row the
        tab is not showing.
        """
        transcript = self._transcript
        if transcript is None or not chat.id:
            return
        with chat.lock:
            if chat.title:
                return
        created = transcript.new_session(first_text, session_id=chat.id)
        if created:
            with chat.lock:
                chat.title = first_text

    def _record_question(
        self, chat: Conversation, question: str, attachments: Sequence[Mapping[str, object]]
    ) -> None:
        transcript = self._transcript
        if transcript is None:
            return
        self._ensure_session(chat, question)
        blob = json.dumps([dict(entry) for entry in attachments], ensure_ascii=False)
        transcript.append(chat.id, "user", question, blob)

    def _record_answer(
        self, chat: Conversation, answer: str, *, model: str = "", task_id: str = ""
    ) -> None:
        transcript = self._transcript
        if transcript is None:
            return
        transcript.append(chat.id, "assistant", answer, model=model, task_id=task_id)

    def clear_history(self, conversation: str = "") -> None:
        """Blank the screen. With a transcript store that means *opening a new
        conversation*, not erasing the old one: 清空 is about what is in front of
        you, and last week's answers are not in front of you."""
        if self._transcript is not None:
            self.new_session()
            return
        chat = self.conversation(conversation)
        chat.clear()

    def _load_from_transcript(self, chat: Conversation) -> None:
        """Replay a stored conversation's recent turns into it, once, when it is opened."""
        transcript = self._transcript
        if transcript is None or not chat.id:
            return
        limit = self._history_limit(self._resolve_pair(chat)) * 2
        rows = transcript.messages(chat.id, limit=limit)
        chat.replace_history(
            [
                (
                    ChatMessage.user(str(row["content"]))
                    if row["role"] == "user"
                    else ChatMessage.assistant(str(row["content"]))
                )
                for row in rows
                if row["role"] in ("user", "assistant")
            ]
        )

    # ------------------------------------------------------------------
    # Prompt assembly
    # ------------------------------------------------------------------

    def _build_system_prompt(self) -> str:
        """Persona plus whatever is known about this user.

        The memory block is appended, not prepended: the persona is what keeps
        the assistant in character, and burying it under a list of facts is how
        a model starts replying like a database.
        """
        base = self._system_prompt or render_prompt("hud_assistant")
        memory = self._memory_provider() if self._memory_provider else None
        if memory is None:
            return base
        try:
            block = memory.profile_block()
        except JarvisError as exc:
            logger.warning("memory profile unavailable: %s", exc)
            return base
        return f"{base}\n\n{block}" if block else base

    def _build_user_message(self, question: str) -> tuple[str, tuple[str, ...]]:
        """Attach retrieved evidence to the question itself.

        The context goes into the user turn rather than the system prompt on
        purpose: evidence is *about this question*, and putting it in the system
        message makes it look like standing policy that applies to everything the
        user says afterwards.

        When the automatic search misses, the model is told it missed and pointed at
        ``knowledge_search``. That is the fix for the failure this used to have: the
        retrieval term is the user's own sentence, so "接着上次那个说" matched nothing
        and the model, seeing no evidence, concluded there was none. Now it can re-query
        with a keyword it picks itself.

        The citations come back as the second half of the tuple instead of being filed
        on the service: two turns in flight used to read each other's sources, and the
        panel then printed 「来源：手册.pdf」 under an answer that had never opened it.
        """
        knowledge = self._knowledge_provider() if self._knowledge_provider else None
        if knowledge is None:
            return question, ()
        try:
            hits = knowledge.retrieve(question)
        except JarvisError as exc:
            logger.warning("knowledge retrieval failed: %s", exc)
            return question, ()
        if hits:
            sources = tuple(hit.citation() for hit in hits)
            evidence = "\n\n".join(
                f"[{index}] 来源：{hit.citation()}\n{hit.text}"
                for index, hit in enumerate(hits, start=1)
            )
            return (
                f"以下是从用户资料里检索到的片段（编号供引用，若与问题无关就忽略）：\n{evidence}"
                f"\n\n问题：{question}",
                sources,
            )
        if self._has_corpus(knowledge):
            return (
                f"{question}\n\n"
                "（按这句原话在资料库里没检索到片段。如果答案可能在导入的资料里，"
                "用 knowledge_search 换个更具体的关键词再查一次，别直接下结论说资料里没有。）"
            ), ()
        return question, ()

    @staticmethod
    def _has_corpus(knowledge: KnowledgeService) -> bool:
        """Whether there is anything worth searching, so the hint is not a lie.

        Pointing a model at an empty corpus produces a confident "你的资料里没有",
        which is exactly the wrong answer to give about a base nobody has imported
        into yet.
        """
        try:
            stats = knowledge.stats()
        except Exception:  # pragma: no cover - a stats failure must not lose the turn
            logger.warning("knowledge stats unavailable", exc_info=True)
            return False
        chunks = stats.get("chunks")
        return isinstance(chunks, int) and not isinstance(chunks, bool) and chunks > 0

    def _initial_messages(
        self,
        system: str,
        user_content: str,
        *,
        chat: Conversation | None = None,
        history: bool = True,
    ) -> list[ChatMessage]:
        messages: list[ChatMessage] = [ChatMessage.system(system)]
        if history and chat is not None:
            limit = self._history_limit(self._resolve_pair(chat))
            messages.extend(chat.replay(limit=limit))
        messages.append(ChatMessage.user(user_content))
        return messages

    def _tool_schemas(self) -> list[dict[str, object]]:
        tools = self._tool_provider() if self._tool_provider else None
        if tools is None or not tools.enabled:
            return []
        return tools.to_openai_tools()

    # ------------------------------------------------------------------
    # The tool-calling loop
    # ------------------------------------------------------------------

    def _run(
        self,
        client: LlmClient,
        messages: list[ChatMessage],
        tools: Sequence[Mapping[str, object]],
        *,
        chat: Conversation,
        pair: _Pair,
        task_id: str = "",
        on_delta: Callable[[str], None] | None = None,
        on_phase: Callable[[str], None] | None = None,
        should_stop: Callable[[], bool] | None = None,
    ) -> _Turn:
        """Drive the model until it answers without asking for a tool.

        When the round cap is hit the model is called once more *without* tools,
        so the user gets a real answer built from whatever was already gathered
        rather than "I ran out of steps".

        Reasoning comes back from every round, not just the last one: a turn that
        called two tools thought before each of them, and a trace that only shows
        the final thought is the part the operator can least use.

        Every round is streamed when somebody is watching or a stop button exists,
        because a stream is the only shape that can be abandoned mid-response:
        ``complete`` hands back one block after twenty seconds and leaves no handle to
        pull away. Tool-call fragments are reassembled inside the client, so this loop
        sees one ``ChatResponse`` per round whether or not anything streamed.
        """
        used: list[str] = []
        thoughts: list[str] = []
        spent: list[int | None] = []
        prompts: list[int | None] = []
        completions: list[int | None] = []
        stop = should_stop if should_stop is not None else _never

        def call(options: GenerationOptions | None) -> ChatResponse:
            """One request, streamed only when there is something to stream through.

            Tallying happens here rather than at each call site because a round whose
            trace was never folded in is invisible in the panel -- and a tool round is
            the round the operator most wants to see, since that is where the model
            decided what to ask the machine for.
            """
            self._phase(chat, chat.thinking_phase(), on_phase)
            built = self._thinking_options(options, pair, task_id)
            if on_delta is None and should_stop is None:
                response = client.complete(messages, options=built)
            elif not isinstance(client, StreamingClient):
                # Answered in one block, and a stop request can only be honoured at the
                # next boundary. Logged because "点了停止没反应" is otherwise
                # indistinguishable from a hang -- and the reason is the client, not
                # the network.
                logger.info("this model client cannot stream; answering in one block")
                if stop():
                    return ChatResponse(content="", model=client.model, finish_reason="cancelled")
                response = client.complete(messages, options=built)
            else:
                response = client.complete_stream(
                    messages,
                    options=built,
                    on_text=on_delta,
                    should_stop=stop,
                )
            thoughts.append(response.reasoning)
            spent.append(_reasoning_tokens(response))
            prompts.append(_prompt_tokens(response))
            completions.append(_completion_tokens(response))
            return response

        def build(response: ChatResponse, *, cancelled: bool) -> _Turn:
            """What the turn amounts to, across every round it has run so far.

            Token totals are summed across rounds rather than taken from the last one:
            a turn that called two tools paid for three requests, and reporting only the
            final usage would understate the turn in exactly the panel whose whole
            purpose is telling the operator what the turn cost.
            """
            return _Turn(
                text=response.content,
                tools_used=tuple(used),
                reasoning="\n\n".join(part for part in thoughts if part),
                reasoning_tokens=_sum_or_none(spent),
                prompt_tokens=_sum_or_none(prompts),
                completion_tokens=_sum_or_none(completions),
                cancelled=cancelled,
            )

        if not tools or self._max_tool_rounds == 0:
            response = call(None)
            return build(response, cancelled=response.finish_reason == "cancelled")

        options = GenerationOptions(tools=tuple(tools), tool_choice="auto")
        for round_index in range(self._max_tool_rounds):
            if stop():
                return _Turn(text="", tools_used=tuple(used), cancelled=True)
            response = call(options)
            if response.finish_reason == "cancelled":
                # The partial answer is kept rather than dropped: the operator watched it
                # arrive, and a "stopped" that lost the text looks identical to "nothing
                # came back".
                return build(response, cancelled=True)
            if not response.tool_calls:
                return build(response, cancelled=False)
            messages.append(
                ChatMessage(
                    role=Role.ASSISTANT,
                    content=response.content,
                    tool_calls=response.tool_calls,
                )
            )
            for position, requested in enumerate(response.tool_calls):
                if stop():
                    # Stopping between two tool calls of one round is deliberately not
                    # offered: the model would never be told what ran and what did not,
                    # and a half-answered tool round is how a resumed conversation starts
                    # with a false premise. The next round boundary is honest instead.
                    logger.info(
                        "turn stopped with %d/%d tool calls of round %d unrun",
                        len(response.tool_calls) - position,
                        len(response.tool_calls),
                        round_index + 1,
                    )
                    break
                used.append(requested.name)
                self._phase(chat, chat.tool_phase(), on_phase)
                result_text = self._invoke(requested.name, requested.arguments)
                messages.append(ChatMessage.tool_result(requested.id, result_text))
            logger.debug(
                "tool round %d finished, %d call(s)", round_index + 1, len(response.tool_calls)
            )

        logger.warning("tool round cap reached (%d); forcing a final answer", self._max_tool_rounds)
        response = call(None)
        return build(response, cancelled=response.finish_reason == "cancelled")

    def _thinking_options(
        self,
        options: GenerationOptions | None = None,
        pair: _Pair | None = None,
        task_id: str = "",
    ) -> GenerationOptions:
        """Attach the thinking knobs for one pair to a request, if there are any.

        Per pair rather than per process: three conversations on three models are
        configured independently in the panel, and reading one global pair would let
        the last model picked in 设置 decide how *every* tab thinks.

        With nothing set the request is left exactly as it was: a model that has
        nothing to show should not be asked for it, and ``enable_thinking`` is not a
        field every compatible endpoint understands.
        """
        request = self._thinking_request(pair or _Pair("", "", False))
        # The task id goes on regardless of the thinking answer: a request that is not
        # asked to reason still costs tokens, and a task whose calls only got counted
        # when thinking happened would report the cheaper half of the work as the whole
        # bill.
        base = _dataclass_replace(options or GenerationOptions(), task_id=task_id)
        if request is None or not request.enabled:
            # Off means "send nothing at all", which is exactly what every request
            # looked like before this setting existed -- and it is the measured state:
            # the qwen endpoint returns no ``reasoning_content`` when the flag is absent.
            # Explicitly sending ``false`` would be a new field on the default path for
            # every provider, and a strict OpenAI-compatible one can 400 on a parameter
            # it does not know. Not worth the risk for a distinction nothing observed.
            return base
        return _dataclass_replace(
            base,
            thinking=True,
            thinking_budget=max(1, int(request.budget)) if request.budget > 0 else None,
            task_id=task_id,
        )

    def _thinking_request(self, pair: _Pair) -> ThinkingRequest | None:
        """How hard this pair should think: its own row first, the global switch after."""
        if pair.provider and pair.model and self._tuning_for is not None:
            try:
                tuning = self._tuning_for(pair.provider, pair.model)
            except Exception:
                logger.exception("could not read the tuning for %s", pair.label)
            else:
                if tuning is not None:
                    return tuning.thinking
        return self._current_thinking_request()

    def _current_thinking_request(self) -> ThinkingRequest | None:
        if self._thinking_provider is None:
            return None
        try:
            return self._thinking_provider()
        except Exception:  # pragma: no cover - a settings read must not lose the turn
            logger.exception("could not read the thinking settings; answering without it")
            return None

    def _invoke(self, name: str, raw_arguments: str) -> str:
        """Run one tool call, turning every failure into text for the model.

        The model needs to *see* the refusal to recover from it ("that needs
        confirmation, ask the user instead"), so a policy block comes back as a
        tool result rather than an exception.
        """
        tools = self._tool_provider() if self._tool_provider else None
        if tools is None:
            return f"工具 {name} 不可用：工具服务未启用"
        try:
            arguments = json.loads(raw_arguments) if raw_arguments.strip() else {}
        except json.JSONDecodeError:
            logger.warning("tool %s got unparsable arguments: %r", name, raw_arguments[:120])
            return f"工具 {name} 的参数不是合法 JSON：{raw_arguments[:120]}"
        if not isinstance(arguments, dict):
            return f"工具 {name} 的参数必须是 JSON 对象"
        try:
            result = tools.invoke(name, arguments)
        except JarvisError as exc:  # pragma: no cover - invoke does not raise
            return f"工具 {name} 执行失败：{exc}"
        return result.as_text()

    # ------------------------------------------------------------------
    # Memory
    # ------------------------------------------------------------------

    def _observe(self, conversation_id: str, question: str, answer: str) -> None:
        """Record the turn and mine it for durable memories.

        Called after the reply exists, never before: memory extraction must not
        sit between the user and their answer.
        """
        memory = self._memory_provider() if self._memory_provider else None
        if memory is None:
            return
        try:
            memory.observe_turn(conversation_id, question, answer)
        except JarvisError as exc:
            logger.warning("could not record the turn: %s", exc)
        except Exception:  # pragma: no cover - memory must never break chat
            logger.exception("memory observation failed unexpectedly")

    # ------------------------------------------------------------------
    # History
    # ------------------------------------------------------------------

    def _remember(self, chat: Conversation, question: str, answer: str) -> None:
        """Append this exchange to the conversation it belongs to, and trim its window.

        The trim is per conversation, and the limit is read per turn: a pair configured
        with 3 轮上下文 next to another with 30 is the case this whole change exists for.
        """
        chat.remember(question, answer, keep_pairs=self._history_limit(self._resolve_pair(chat)))
