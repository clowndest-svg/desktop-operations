"""The ``LlmClient`` protocol: what "an LLM" means to the rest of JARVIS.

Consumers (agents, planner, memory summariser...) depend on this protocol
only — never on a concrete implementation. That keeps providers swappable
per config change and makes every consumer trivially testable with a stub.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Sequence
from typing import Protocol, runtime_checkable

from jarvis.llm.types import ChatMessage, ChatResponse, GenerationOptions, StreamChunk

__all__ = ["LlmClient", "StreamingClient"]


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


@runtime_checkable
class StreamingClient(LlmClient, Protocol):
    """An :class:`LlmClient` that can also answer incrementally.

    A separate protocol rather than a sixth member of :class:`LlmClient`, because
    "can this be streamed?" is a question the caller has to be able to ask and answer
    out loud: a client without this method still gets its answer, just without
    increments and without a stop that lands mid-response. Folding the member into the
    base protocol would have turned that into a type error instead of a behaviour, and
    the behaviour is the part worth reading.
    """

    def complete_stream(
        self,
        messages: Sequence[ChatMessage],
        *,
        options: GenerationOptions | None = None,
        on_text: Callable[[str], None] | None = None,
        should_stop: Callable[[], bool] | None = None,
    ) -> ChatResponse:
        """Answer, reporting the text as it arrives and honouring a stop request.

        The difference from :meth:`LlmClient.stream` is who owns the pieces: ``stream``
        hands out increments and leaves assembly to the caller, while this returns the
        same :class:`~jarvis.llm.types.ChatResponse` ``complete`` would -- tool calls
        folded back together -- after having called ``on_text`` with the whole partial
        answer each time it grew.

        ``should_stop`` is polled once per chunk, and abandoning the stream closes the
        connection now rather than whenever the collector gets round to it. A provider
        that refuses streamed requests outright is remembered and answered with
        :meth:`LlmClient.complete` instead; a half-received answer is never re-asked,
        because the operator is already watching it.

        Raises:
            LlmError: any subclass from :mod:`jarvis.llm.errors`.
        """
        ...
