"""Orchestration-layer types: the LangGraph state and pipeline events."""

from __future__ import annotations

from typing import TypedDict

from jarvis.core.events import PipelineEvent
from jarvis.llm.types import ChatMessage

__all__ = ["GraphState", "PipelineEvent"]


class GraphState(TypedDict, total=False):
    """Mutable state threaded through the LangGraph supervisor graph.

    ``user_text`` / ``history`` are the inputs for the current turn;
    ``next`` is the router's decision; ``trace`` records which agents ran;
    ``final_answer`` is the produced reply (None until a worker fills it).
    """

    user_text: str
    history: list[ChatMessage]
    messages: list[ChatMessage]
    next: str
    trace: list[str]
    final_answer: str | None
