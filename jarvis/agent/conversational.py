"""Conversational agent: general chat / QA backed by an LLM client."""

from __future__ import annotations

from jarvis.agent.types import AgentContext, AgentResult
from jarvis.llm.client import LlmClient
from jarvis.llm.types import ChatMessage, Role
from jarvis.prompt import render_prompt


class ConversationalAgent:
    """Fallback / general-purpose chat agent backed by an LLM client."""

    name = "chat"

    def __init__(self, llm: LlmClient, *, system_prompt: str | None = None) -> None:
        self._llm = llm
        # Resolved by name so a `prompt.overrides` entry in the config file
        # takes effect: the wording is the behaviour for this worker.
        self._system = system_prompt or render_prompt("agent_conversational")

    def run(self, context: AgentContext) -> AgentResult:
        messages: list[ChatMessage] = []
        if not context.history or context.history[0].role != Role.SYSTEM:
            messages.append(ChatMessage.system(self._system))
        messages.extend(context.history)
        messages.append(ChatMessage.user(context.user_text))
        response = self._llm.complete(messages)
        return AgentResult(text=response.content)
