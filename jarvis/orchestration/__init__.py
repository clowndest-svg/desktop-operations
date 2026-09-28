"""Orchestration: LangGraph multi-agent brain + the end-to-end voice pipeline.

Responsibility (phase 10):
    * :class:`AgentGraph` — a LangGraph ``StateGraph`` that routes a turn
      through worker agents (chat / tools) via an LLM supervisor.
    * :class:`VoicePipeline` — a single-microphone state machine wiring
      wake word -> VAD -> ASR -> agent graph -> TTS (with Barge-In).
    * :class:`OrchestrationService` — the lifecycle component that owns both.

``AgentGraph`` is re-exported lazily (PEP 562) so importing this package never
forces ``langgraph`` / ``torch`` to load unless the graph is actually used.
"""

from jarvis.orchestration.service import OrchestrationService, OrchestrationSettings
from jarvis.orchestration.types import PipelineEvent
from jarvis.orchestration.voice_pipeline import VoicePipeline

__all__ = [
    "AgentGraph",
    "OrchestrationService",
    "OrchestrationSettings",
    "PipelineEvent",
    "VoicePipeline",
]


def __getattr__(name: str) -> object:
    if name == "AgentGraph":
        from jarvis.orchestration.graph import AgentGraph

        return AgentGraph
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
