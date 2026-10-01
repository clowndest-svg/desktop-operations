"""Provider-agnostic chat data types.

These are the *only* message/response shapes the rest of JARVIS is allowed
to use — no OpenAI SDK objects, no raw dicts. All types are frozen (safe to
share across threads/agents) and deliberately minimal: fields that no
consumer needs yet are not modelled.

Tool-call *passthrough* is included already (the wire format exists in
every OpenAI-compatible API); actually executing tools is phase 12.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum

__all__ = [
    "ChatMessage",
    "ChatResponse",
    "GenerationOptions",
    "Role",
    "StreamChunk",
    "ToolCall",
    "Usage",
]


class Role(StrEnum):
    """Chat participant role, wire-compatible with OpenAI ``role`` values."""

    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


@dataclass(frozen=True, slots=True)
class ToolCall:
    """A tool invocation requested by the model.

    ``arguments`` stays a raw JSON string on purpose: parsing/validating it
    belongs to the tool layer (phase 12), and echoing it back verbatim is
    required by the wire protocol.
    """

    id: str
    name: str
    arguments: str


@dataclass(frozen=True, slots=True)
class ChatMessage:
    """One turn in a conversation."""

    role: Role
    content: str
    name: str | None = None
    """Optional participant name (OpenAI ``name`` field)."""

    tool_call_id: str | None = None
    """Set on ``Role.TOOL`` messages: which call this result answers."""

    tool_calls: tuple[ToolCall, ...] = ()
    """Set on assistant messages that requested tool invocations."""

    images: tuple[str, ...] = ()
    """Data URLs or http URLs attached to a *user* message.

    Empty for everything except a turn the operator attached pictures to. Providers
    that cannot see images get the text alone; the attachment is still described in
    the text so the model knows it was handed something it cannot open.
    """

    @staticmethod
    def system(content: str) -> ChatMessage:
        return ChatMessage(role=Role.SYSTEM, content=content)

    @staticmethod
    def user(content: str) -> ChatMessage:
        return ChatMessage(role=Role.USER, content=content)

    @staticmethod
    def assistant(content: str) -> ChatMessage:
        return ChatMessage(role=Role.ASSISTANT, content=content)

    @staticmethod
    def tool_result(tool_call_id: str, content: str) -> ChatMessage:
        return ChatMessage(role=Role.TOOL, content=content, tool_call_id=tool_call_id)


@dataclass(frozen=True, slots=True)
class GenerationOptions:
    """Per-request sampling knobs. ``None`` means "provider default"."""

    temperature: float | None = None
    max_tokens: int | None = None
    top_p: float | None = None
    tools: tuple[Mapping[str, object], ...] = ()
    """Function-calling schemas, in the provider's ``tools`` array shape.

    ``jarvis.llm`` deliberately does not build these: the shape belongs to
    whoever owns the tool list (:class:`jarvis.tools.registry.ToolRegistry`
    renders it), and keeping the two apart is what stops this package from
    learning what a tool *is*.
    """

    tool_choice: str | None = None
    """``auto`` / ``none`` / ``required``. ``None`` lets the provider decide."""


@dataclass(frozen=True, slots=True)
class Usage:
    """Token accounting reported by the provider."""

    prompt_tokens: int
    completion_tokens: int
    cached_tokens: int | None = None
    """Prompt tokens the provider served out of its cache, when it says so at all.

    ``None`` means the provider did not report it and every screen must then read
    「无读数」. A real ``0`` means it answered and nothing was cached. Collapsing
    those two is how a cache-hit rate quietly turns into a made-up number.
    """

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    @property
    def cache_hit_percent(self) -> float | None:
        """Share of the prompt that was cached, or ``None`` when unknowable.

        Clamped to 100 because a provider that counts cached prompt tokens
        separately from the prompt total can report more cached than input.
        """
        if self.cached_tokens is None or self.prompt_tokens <= 0:
            return None
        return 100.0 * min(self.cached_tokens, self.prompt_tokens) / self.prompt_tokens


@dataclass(frozen=True, slots=True)
class ChatResponse:
    """A complete (non-streaming) model reply."""

    content: str
    model: str
    """The model that actually answered (may differ from the requested one)."""

    finish_reason: str | None = None
    usage: Usage | None = None
    tool_calls: tuple[ToolCall, ...] = ()


@dataclass(frozen=True, slots=True)
class StreamChunk:
    """One increment of a streaming reply.

    ``text`` may be empty on the final chunk that only carries
    ``finish_reason``/``usage``.
    """

    text: str = ""
    finish_reason: str | None = None
    usage: Usage | None = None
