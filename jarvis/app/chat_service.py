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
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from dataclasses import replace as _dataclass_replace
from typing import TYPE_CHECKING

from jarvis.core.exceptions import JarvisError
from jarvis.llm.types import ChatMessage, GenerationOptions, Role
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

    def to_dict(self) -> dict[str, object]:
        """The three keys the Vue bundle renders.

        Deliberately *not* extended with the new fields: this dict crosses the
        pywebview bridge into a page built from a fixed bundle, and changing the
        payload shape is how a working window starts rendering blank rows. The
        richer fields are read directly by the command-line entry points.
        """
        return {"question": self.question, "answer": self.answer, "error": self.error}


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


def _with_attachment_note(question: str, attachments: Sequence[Mapping[str, object]]) -> str:
    """Name what was handed over, in the text the model reads.

    Image pixels travel as message parts where the provider supports them, but a
    model that cannot see them still deserves to know a picture arrived -- and a
    video or a document has no pixels to send at all, so its name and size are the
    whole of what can be said.
    """
    if not attachments:
        return question
    lines = [question, ""]
    for entry in attachments:
        note = (
            f"[附件] {entry.get('name')} · {entry.get('mime')} · {_as_size(entry.get('size'))} 字节"
        )
        if entry.get("kind") == "image" and entry.get("data"):
            note += " · 已随消息提供缩略图"
        elif entry.get("kind") == "image":
            note += " · 图太大未随消息提供，只能看到名字"
        else:
            note += " · 本工具不解析这类文件的内容"
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
    ) -> None:
        """Create the service.

        Args:
            client_provider: Yields the live LLM client. A provider, not a client:
                components are registered before ``LlmService`` has built one, and
                reading it early raises.
            max_history_turns: How many past exchanges are replayed to the model.
                Long enough for follow-up questions, short enough that a page left
                open all day does not grow the request without bound.
            system_prompt: The persona the answers come from.
            memory_provider: Long-term memory, or ``None`` for a stateless chat.
            knowledge_provider: The RAG knowledge base, or ``None``.
            tool_provider: The tool registry, or ``None`` for chat-only.
            transcript: Where conversations are kept across restarts. ``None``
                keeps the old behaviour exactly: one in-memory conversation that
                清空 or a quit ends for good.
            session_id: Which conversation to record turns under.
            max_tool_rounds: Cap on tool-calling iterations per turn.
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
        self._session_id = session_id
        self._max_tool_rounds = max(0, max_tool_rounds)
        self._history: list[ChatMessage] = []
        self._last_sources: tuple[str, ...] = ()
        self._started = False

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
        """Drop the in-memory replay list. Stored conversations are untouched:
        shutting the app down is not an instruction about anybody's history."""
        self._started = False
        self._history = []
        self._last_sources = ()

    @property
    def running(self) -> bool:
        return self._started

    # ------------------------------------------------------------------
    # Turns
    # ------------------------------------------------------------------

    def ask(
        self,
        text: str,
        *,
        remembered: bool = True,
        attachments: Sequence[Mapping[str, object]] = (),
    ) -> ChatReply:
        """Answer one question. Never raises: see the module docstring.

        Args:
            text: What to ask.
            remembered: Whether the exchange goes into the running conversation
                and the long-term memory. ``False`` is for turns nobody *said* --
                a machine-written digest handed over by a button -- which would
                otherwise sit in the history and steer every later answer.
        """
        question = text.strip()
        if not question:
            return ChatReply("", "", "问题是空的")
        if not self._started:
            return ChatReply(question, "", "对话服务未启动")
        try:
            client = self._client_provider()
        except JarvisError as exc:
            logger.error("chat has no model client: %s", exc)
            return ChatReply(question, "", str(exc))
        except Exception as exc:  # a provider bug must still reach the page
            logger.exception("chat could not read the model client")
            return ChatReply(question, "", f"{type(exc).__name__}: {exc}")

        kept = _clean_attachments(attachments)
        try:
            system = self._build_system_prompt()
            user_content = self._build_user_message(_with_attachment_note(question, kept))
            messages = self._initial_messages(system, user_content, history=remembered)
            if messages and kept:
                images = tuple(
                    str(entry["data"])
                    for entry in kept
                    if entry.get("kind") == "image" and entry.get("data")
                )
                if images:
                    messages[-1] = _dataclass_replace(messages[-1], images=images)
            tools = self._tool_schemas()
            answer, used = self._run(client, messages, tools)
        except JarvisError as exc:
            logger.error("chat turn failed: %s", exc)
            return ChatReply(question, "", str(exc))
        except Exception as exc:
            logger.exception("chat turn failed unexpectedly")
            return ChatReply(question, "", f"{type(exc).__name__}: {exc}")

        answer = answer.strip()
        if not answer:
            return ChatReply(question, "", "模型没有返回内容")
        if remembered:
            self._remember(ChatMessage.user(question), ChatMessage.assistant(answer))
            self._observe(question, answer)
            self._persist(question, answer, kept)
        return ChatReply(
            question,
            answer,
            tools_used=tuple(used),
            sources=self._last_sources,
            grounded=bool(self._last_sources),
        )

    @property
    def history_length(self) -> int:
        """How many exchanges are still being replayed to the model."""
        return len(self._history) // 2

    @property
    def session_id(self) -> str:
        """The conversation new turns are being written into."""
        return self._session_id

    # ------------------------------------------------------------------
    # Conversations, kept across restarts
    # ------------------------------------------------------------------

    def sessions(self) -> dict[str, object]:
        """The list the HUD's history popup draws, newest first."""
        transcript = self._transcript
        if transcript is None:
            return {"error": "对话历史未启用", "current": self._session_id, "sessions": []}
        return {"error": "", "current": self._session_id, "sessions": transcript.list_sessions()}

    def session_messages(self, session_id: str) -> dict[str, object]:
        """Every turn of one conversation, oldest first, for the page to draw."""
        transcript = self._transcript
        if transcript is None:
            return {"error": "对话历史未启用", "current": self._session_id, "messages": []}
        return {
            "error": "",
            "current": self._session_id,
            "messages": transcript.whole_session(str(session_id)),
        }

    def new_session(self) -> dict[str, object]:
        """Open a blank conversation. The old one stays on disk."""
        self._history = []
        self._last_sources = ()
        transcript = self._transcript
        if transcript is not None:
            self._session_id = transcript.new_session() or UNSAVED_SESSION
        return self.sessions()

    def switch_session(self, session_id: str) -> dict[str, object]:
        """Move the live conversation to a stored one, replaying its recent turns."""
        transcript = self._transcript
        if transcript is None:
            return self.sessions()
        target = str(session_id)
        rows = transcript.messages(target, limit=self._max_history * 2)
        self._session_id = target
        self._history = [
            (
                ChatMessage.user(str(row["content"]))
                if row["role"] == "user"
                else ChatMessage.assistant(str(row["content"]))
            )
            for row in rows
        ]
        self._last_sources = ()
        return self.sessions()

    def rename_session(self, session_id: str, title: str) -> dict[str, object]:
        transcript = self._transcript
        if transcript is None:
            return self.sessions()
        if not transcript.rename(str(session_id), str(title)):
            return {**self.sessions(), "error": "没有这个对话，或改名失败"}
        return self.sessions()

    def delete_session(self, session_id: str) -> dict[str, object]:
        """Delete one stored conversation. Deleting the open one opens a new one."""
        transcript = self._transcript
        if transcript is None:
            return self.sessions()
        target = str(session_id)
        if not transcript.delete(target):
            return {**self.sessions(), "error": "没有这个对话"}
        if target == self._session_id:
            return self.new_session()
        return self.sessions()

    def append_turn(self, role: str, content: str) -> None:
        """One *spoken* turn, from the voice pipeline, into the open conversation.

        Spoken and typed turns land in the same session list because they are the
        same conversation from the operator's side of the glass; keeping them in
        two histories would make "what did we talk about" unanswerable.
        """
        transcript = self._transcript
        if transcript is None or role not in ("user", "assistant") or not content.strip():
            return
        self._ensure_session(content)
        transcript.append(self._session_id, role, content)

    def _ensure_session(self, first_text: str) -> None:
        if self._transcript is None:
            return
        if self._session_id and self._session_id != UNSAVED_SESSION:
            return
        created = self._transcript.new_session(first_text)
        if created:
            self._session_id = created

    def _persist(
        self, question: str, answer: str, attachments: Sequence[Mapping[str, object]] = ()
    ) -> None:
        transcript = self._transcript
        if transcript is None:
            return
        self._ensure_session(question)
        if not self._session_id or self._session_id == UNSAVED_SESSION:
            return
        blob = json.dumps(list(attachments), ensure_ascii=False)
        transcript.append(self._session_id, "user", question, blob)
        transcript.append(self._session_id, "assistant", answer)

    def clear_history(self) -> None:
        """Blank the screen. With a transcript store that means *opening a new
        conversation*, not erasing the old one: 清空 is about what is in front of
        you, and last week's answers are not in front of you."""
        if self._transcript is not None:
            self.new_session()
            return
        self._history = []
        self._last_sources = ()

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

    def _build_user_message(self, question: str) -> str:
        """Attach retrieved evidence to the question itself.

        The context goes into the user turn rather than the system prompt on
        purpose: evidence is *about this question*, and putting it in the system
        message makes it look like standing policy that applies to everything the
        user says afterwards.
        """
        self._last_sources = ()
        knowledge = self._knowledge_provider() if self._knowledge_provider else None
        if knowledge is None:
            return question
        try:
            hits = knowledge.retrieve(question)
        except JarvisError as exc:
            logger.warning("knowledge retrieval failed: %s", exc)
            return question
        if not hits:
            return question
        self._last_sources = tuple(hit.citation() for hit in hits)
        evidence = "\n\n".join(
            f"[{index}] 来源：{hit.citation()}\n{hit.text}"
            for index, hit in enumerate(hits, start=1)
        )
        return (
            f"以下是从用户资料里检索到的片段（编号供引用，若与问题无关就忽略）：\n{evidence}"
            f"\n\n问题：{question}"
        )

    def _initial_messages(
        self, system: str, user_content: str, *, history: bool = True
    ) -> list[ChatMessage]:
        messages: list[ChatMessage] = [ChatMessage.system(system)]
        if history:
            messages.extend(self._history)
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
    ) -> tuple[str, list[str]]:
        """Drive the model until it answers without asking for a tool.

        Returns:
            ``(answer_text, tool_names_used)``.

        When the round cap is hit the model is called once more *without* tools,
        so the user gets a real answer built from whatever was already gathered
        rather than "I ran out of steps".
        """
        used: list[str] = []
        if not tools or self._max_tool_rounds == 0:
            return client.complete(messages).content, used

        options = GenerationOptions(tools=tuple(tools), tool_choice="auto")
        for round_index in range(self._max_tool_rounds):
            response = client.complete(messages, options=options)
            if not response.tool_calls:
                return response.content, used
            messages.append(
                ChatMessage(
                    role=Role.ASSISTANT,
                    content=response.content,
                    tool_calls=response.tool_calls,
                )
            )
            for call in response.tool_calls:
                used.append(call.name)
                result_text = self._invoke(call.name, call.arguments)
                messages.append(ChatMessage.tool_result(call.id, result_text))
            logger.debug(
                "tool round %d finished, %d call(s)", round_index + 1, len(response.tool_calls)
            )

        logger.warning("tool round cap reached (%d); forcing a final answer", self._max_tool_rounds)
        return client.complete(messages).content, used

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

    def _observe(self, question: str, answer: str) -> None:
        """Record the turn and mine it for durable memories.

        Called after the reply exists, never before: memory extraction must not
        sit between the user and their answer.
        """
        memory = self._memory_provider() if self._memory_provider else None
        if memory is None:
            return
        try:
            memory.observe_turn(self._session_id, question, answer)
        except JarvisError as exc:
            logger.warning("could not record the turn: %s", exc)
        except Exception:  # pragma: no cover - memory must never break chat
            logger.exception("memory observation failed unexpectedly")

    # ------------------------------------------------------------------
    # History
    # ------------------------------------------------------------------

    def _remember(self, *messages: ChatMessage) -> None:
        self._history.extend(messages)
        limit = self._max_history * 2
        if limit and len(self._history) > limit:
            self._history = self._history[-limit:]
