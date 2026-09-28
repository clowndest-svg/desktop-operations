"""The ``LlmClient`` protocol: what "an LLM" means to the rest of JARVIS.

Consumers (agents, planner, memory summariser...) depend on this protocol
only — never on a concrete implementation. That keeps providers swappable
per config change and makes every consumer trivially testable with a stub.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from typing import Protocol, runtime_checkable

from jarvis.llm.types import ChatMessage, ChatResponse, GenerationOptions, StreamChunk

__all__ = ["LlmClient"]


@runtime_checkable
class LlmClient(Protocol):
    """A synchronous chat-completion client (streaming and non-streaming)."""

    @property
    def provider_name(self) -> str:
        """Configured provider key, e.g. ``deepseek``."""
        ...

    @property
    def model(self) -> str:
        """Model identifier requests are sent to."""
        ...

    def complete(
        self,
        messages: Sequence[ChatMessage],
        *,
        options: GenerationOptions | None = None,
    ) -> ChatResponse:
        """Send a conversation, block until the full reply is available.

        Raises:
            LlmError: any subclass from :mod:`jarvis.llm.errors`.
        """
        ...

    def stream(
        self,
        messages: Sequence[ChatMessage],
        *,
        options: GenerationOptions | None = None,
    ) -> Iterator[StreamChunk]:
        """Send a conversation, yield reply increments as they arrive.

        Raises:
            LlmError: any subclass from :mod:`jarvis.llm.errors`; errors
                before the first chunk are retried per policy, errors
                mid-stream are not (the partial text is already out).
        """
        ...
