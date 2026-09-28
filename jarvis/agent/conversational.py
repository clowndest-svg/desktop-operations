"""Conversational agent: general chat / QA backed by an LLM client."""

from __future__ import annotations

from jarvis.agent.types import AgentContext, AgentResult
from jarvis.llm.client import LlmClient
from jarvis.llm.types import ChatMessage, Role

_DEFAULT_SYSTEM = "你是 JARVIS，一个中文语音助手。请用简洁、自然、有帮助的中文回答用户。"


class ConversationalAgent:
    """Fallback / general-purpose chat agent backed by an LLM client."""

    name = "chat"

    def __init__(self, llm: LlmClient, *, system_prompt: str | None = None) -> None:
        self._llm = llm
        self._system = system_prompt or _DEFAULT_SYSTEM

    def run(self, context: AgentContext) -> AgentResult:
        messages: list[ChatMessage] = []
        if not context.history or context.history[0].role != Role.SYSTEM:
            messages.append(ChatMessage.system(self._system))
        messages.extend(context.history)
        messages.append(ChatMessage.user(context.user_text))
        response = self._llm.complete(messages)
        return AgentResult(text=response.content)
