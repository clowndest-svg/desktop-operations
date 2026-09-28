"""Chat service: the text door the desktop HUD types through.

Deliberately narrow. The window can already see telemetry and the disk panel; what
it could not do until now was ask a question without speaking one. This answers a
typed turn through the model and keeps a short rolling history -- no speech
recognition, no synthesis, no WAV files.

Two reasons it stops there:

* The voice stack needs roughly 3 GB and tens of seconds to load; a text box that
  dragged it in could not open before an API key alone.
* WebView2 refuses to play a local audio file from a ``file://`` page, so a spoken
  reply would have to be served some other way. Speech stays on the voice path,
  where :class:`~jarvis.orchestration.voice_pipeline.VoicePipeline` plays it
  straight out of the engine.

:meth:`ask` never raises. It runs on pywebview's JS-API thread, and an exception
there surfaces to the page as an opaque bridge failure -- the one place a question
goes missing with no trace of why.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from jarvis.agent.conversational import ConversationalAgent
from jarvis.agent.types import AgentContext
from jarvis.core.exceptions import JarvisError
from jarvis.llm.types import ChatMessage

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Callable

    from jarvis.llm.client import LlmClient

logger = logging.getLogger("jarvis.app.chat_service")

HUD_SYSTEM_PROMPT = (
    "你是小夜，运行在用户本机电脑上的桌面助手。用简洁的中文回答，能一句话说清就不要写两段。"
    "用户在屏幕上阅读，可以适当分点。不要声称你执行了没有执行的操作。"
)
"""Written for reading, not for speaking.

The voice prompt in :mod:`jarvis.app.demo` caps replies at ~40 characters because
every Markdown mark turns into an audible pause. A typed answer has no such limit,
so the constraint here is the opposite one: don't be lazy, and don't invent
capabilities -- the operator is looking at a panel that really can delete files.
"""


@dataclass(frozen=True, slots=True)
class ChatReply:
    """One answered question, in the shape the page renders."""

    question: str
    answer: str
    error: str = ""
    """Non-empty when the turn failed; shown inline instead of swallowed."""

    def to_dict(self) -> dict[str, object]:
        return {"question": self.question, "answer": self.answer, "error": self.error}


class ChatService:
    """Answers typed turns against an :class:`~jarvis.llm.client.LlmClient`."""

    name = "chat"

    def __init__(
        self,
        client_provider: Callable[[], LlmClient],
        *,
        max_history_turns: int = 10,
        system_prompt: str = HUD_SYSTEM_PROMPT,
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
        """
        self._client_provider = client_provider
        self._max_history = max(0, max_history_turns)
        self._system_prompt = system_prompt
        self._history: list[ChatMessage] = []
        self._started = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Mark ready. The client is fetched per turn, so nothing loads here."""
        self._started = True
        logger.info("chat service ready (text only; no speech models)")

    def stop(self) -> None:
        self._started = False
        self.clear_history()

    @property
    def running(self) -> bool:
        return self._started

    # ------------------------------------------------------------------
    # Turns
    # ------------------------------------------------------------------

    def ask(self, text: str) -> ChatReply:
        """Answer one question. Never raises: see the module docstring."""
        from jarvis.llm.types import ChatMessage

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

        messages: list[ChatMessage] = list(self._history)
        try:
            agent = ConversationalAgent(client, system_prompt=self._system_prompt)
            result = agent.run(AgentContext(user_text=question, history=messages))
        except JarvisError as exc:
            logger.error("chat turn failed: %s", exc)
            return ChatReply(question, "", str(exc))
        except Exception as exc:
            logger.exception("chat turn failed unexpectedly")
            return ChatReply(question, "", f"{type(exc).__name__}: {exc}")

        answer = result.text.strip()
        if not answer:
            return ChatReply(question, "", "模型没有返回内容")
        self._remember(ChatMessage.user(question), ChatMessage.assistant(answer))
        return ChatReply(question, answer)

    @property
    def history_length(self) -> int:
        """How many exchanges are still being replayed to the model."""
        return len(self._history) // 2

    def clear_history(self) -> None:
        """Forget the conversation. The page's 清空 button calls this."""
        self._history = []

    def _remember(self, *messages: ChatMessage) -> None:
        self._history.extend(messages)
        limit = self._max_history * 2
        if limit and len(self._history) > limit:
            self._history = self._history[-limit:]
