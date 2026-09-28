"""Agent contracts for the multi-agent layer.

An :class:`Agent` turns a user turn plus conversation history into a reply.
Agents are the *workers* in the LangGraph orchestration graph; the supervisor
routes a turn to one of them. Workers never call each other directly — all
control flow lives in :mod:`jarvis.orchestration.graph`.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from jarvis.llm.types import ChatMessage


@dataclass(frozen=True, slots=True)
class AgentResult:
    """A worker agent's reply."""

    text: str
    """Reply text; empty string means "I can't handle this" (router falls back)."""

    used_tools: tuple[str, ...] = ()
    """Names of tools the agent invoked (empty for pure-chat agents)."""


@dataclass(frozen=True, slots=True)
class AgentContext:
    """Inputs handed to a worker agent for one turn."""

    user_text: str
    """The transcribed user utterance."""

    history: Sequence[ChatMessage] = ()
    """Prior turns (oldest first), excluding the current user turn."""

    default_agent: str = "chat"
    """Fallback agent name (the router uses it when intent is ambiguous)."""


@runtime_checkable
class Agent(Protocol):
    """A unit of conversational capability (a worker in the graph)."""

    @property
    def name(self) -> str:
        """Stable agent id used by the supervisor router."""
        ...

    def run(self, context: AgentContext) -> AgentResult:
        """Produce a reply for ``context``."""
        ...
