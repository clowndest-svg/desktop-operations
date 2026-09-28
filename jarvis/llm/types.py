"""Provider-agnostic chat data types.

These are the *only* message/response shapes the rest of JARVIS is allowed
to use — no OpenAI SDK objects, no raw dicts. All types are frozen (safe to
share across threads/agents) and deliberately minimal: fields that no
consumer needs yet are not modelled.

Tool-call *passthrough* is included already (the wire format exists in
every OpenAI-compatible API); actually executing tools is phase 12.
"""

from __future__ import annotations

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


@dataclass(frozen=True, slots=True)
class Usage:
    """Token accounting reported by the provider."""

    prompt_tokens: int
    completion_tokens: int

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


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
