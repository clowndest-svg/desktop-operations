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

    Empty for everything except a turn the operator attached pictures to. The OpenAI-style
    client turns a non-empty tuple into a ``content`` array of text + ``image_url`` parts,
    which is the one shape every compatible endpoint agrees on -- and the one a model
    without eyes will answer with an error about. What the attachment note promises is
    decided by whether a picture is really going out, so the list here and the text above
    it must never disagree.
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

    thinking: bool | None = None
    """Ask the model to return its reasoning alongside the answer.

    ``None`` sends nothing, which is what every caller did before this field existed
    and what keeps a provider that has no such knob from being told to invent one.
    Measured on the qwenai token-plan endpoint on 2026-10-02: with the flag on, the
    reply carries ``reasoning_content`` and ``usage.completion_tokens_details
    .reasoning_tokens``; without it, neither field appears at all — so the reason the
    HUD showed no thinking chain was the request, not the rendering.
    """

    thinking_budget: int | None = None
    """How many tokens of that reasoning are allowed.

    Not a "high/medium/low" label: the same endpoint truncates ``reasoning_content``
    at the budget it is given while still answering in full, so this is a real spend
    limit and the number in the settings panel is the number that shows up in
    ``reasoning_tokens``.
    """

    task_id: str = ""
    """Which turn or round-table task this request belongs to, for the ledger.

    Not a wire field -- :meth:`OpenAiCompatClient._payload` picks what it sends
    explicitly, so this never reaches a provider. It exists because a task answered by
    three models over four rounds is twelve requests, and "what did that cost" can only
    be answered afterwards if every one of them knew which task it was for. Time-window
    totals cannot say that: they are the only grouping the ledger had until a task
    needed one.
    """


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

    reasoning_tokens: int | None = None
    """Tokens the model spent on reasoning, when it says so at all.

    Same rule as ``cached_tokens`` and for the same reason: this is what makes the
    thinking budget in the settings panel checkable rather than decorative.
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
    reasoning: str = ""
    """What the model thought before it answered, when it was asked to say (``thinking``).

    Kept out of ``content`` because the two have different lives: the answer is what
    gets read aloud, stored in the transcript and replayed into the next turn, while
    the reasoning is shown to the operator on request and belongs in none of those.
    """


@dataclass(frozen=True, slots=True)
class StreamToolCall:
    """One fragment of a streamed tool call.

    Split out from :class:`ToolCall` because the wire does not send a whole call:
    ``id`` and ``name`` arrive once, ``arguments`` in pieces, and several calls can be
    interleaved by ``index``. Assembling them is the client's job -- an ``arguments``
    fragment is not valid JSON on its own, so nothing upstream of here could parse it
    even if it tried.
    """

    index: int
    call_id: str = ""
    name: str = ""
    arguments: str = ""


@dataclass(frozen=True, slots=True)
class StreamChunk:
    """One increment of a streaming reply.

    ``text`` may be empty on the final chunk that only carries
    ``finish_reason``/``usage``.
    """

    text: str = ""
    reasoning: str = ""
    """The reasoning increment, when the endpoint streams one.

    Only worth a field because the answer and the reasoning arrive on different keys
    (``delta.content`` and ``delta.reasoning_content``) and a thinking bubble that
    silently stayed empty while the answer streamed would look like the setting was off.
    """

    tool_calls: tuple[StreamToolCall, ...] = ()
    """Tool-call fragments in this chunk. Empty for a plain text increment."""

    model: str = ""
    """The model this chunk claims answered, when the endpoint repeats it per chunk.

    Worth carrying because the attribution written into the transcript should be the
    model that *answered*, not the one that was asked -- several providers answer with
    a different id than the one in the request.
    """

    finish_reason: str | None = None
    usage: Usage | None = None
