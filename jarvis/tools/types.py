"""Tool vocabulary: what a tool declares, and what calling one returns.

A tool is *data plus a callable*: a JSON-Schema description the model can read,
a risk level the policy can act on, and a handler. Keeping the description next
to the handler is what makes automatic discovery possible — the registry can
hand the model a tool list without anybody maintaining a second document that
drifts.

The risk level is not decoration. It is the only thing standing between "the
model summarised a file" and "the model overwrote a file", so it is declared by
the tool author and enforced by :mod:`jarvis.tools.policy`, never by the model.
"""

from __future__ import annotations

import enum
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field


class RiskLevel(enum.StrEnum):
    """How much damage a tool can do if it is called at the wrong moment."""

    SAFE = "safe"
    """Read-only, no side effects. Always allowed."""

    CAUTION = "caution"
    """Has a side effect (network, clipboard, a new file) but nothing
    irreversible. Allowed, and worth logging."""

    DANGEROUS = "dangerous"
    """Can destroy data or run code: delete, overwrite, shell. Refused unless
    the caller passes an explicit confirmation *and* the config allows it."""


SOURCE_BUILTIN: str = "builtin"
"""``ToolSpec.source`` for tools shipped with JARVIS."""

SOURCE_PLUGIN_PREFIX: str = "plugin:"
"""``ToolSpec.source`` prefix for plugin-provided tools."""

SOURCE_MCP_PREFIX: str = "mcp:"
"""``ToolSpec.source`` prefix for tools bridged from an MCP server."""

PERMISSION_WRITE: str = "write"
"""The tool creates, modifies or deletes something on disk."""

PERMISSION_SHELL: str = "shell"
"""The tool runs a process."""

PERMISSION_NETWORK: str = "network"
"""The tool makes an outbound request."""

KNOWN_PERMISSIONS: frozenset[str] = frozenset(
    {PERMISSION_WRITE, PERMISSION_SHELL, PERMISSION_NETWORK}
)
"""Every permission the policy understands.

Declared separately from :class:`RiskLevel` because the two answer different
questions. ``RiskLevel`` says how bad a mistake would be; a permission says
*which config switch* governs it. "Write a file" and "run a shell command" are
both dangerous, and an operator wants to allow one without the other.
"""


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """Everything the registry and the model need to know about one tool."""

    name: str
    """Stable id. Must match ``^[A-Za-z0-9_-]{1,64}$`` so it survives JSON
    Schema and every provider's function-calling format."""

    description: str
    """What the tool does, written for the model to read. Chinese, because the
    model is instructed in Chinese and the tool list is part of its prompt."""

    parameters: Mapping[str, object] = field(default_factory=dict)
    """JSON Schema object describing the arguments (``{"type": "object", ...}``)."""

    risk: RiskLevel = RiskLevel.SAFE
    """Declared by the author, enforced by the policy."""

    source: str = SOURCE_BUILTIN
    """Where the tool came from, so it can be unregistered as a group."""

    permissions: frozenset[str] = frozenset()
    """Which config switches govern this tool; see :data:`KNOWN_PERMISSIONS`."""

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "description": self.description,
            "parameters": dict(self.parameters),
            "risk": self.risk.value,
            "source": self.source,
            "permissions": sorted(self.permissions),
        }

    def to_openai_schema(self) -> dict[str, object]:
        """Render in the OpenAI ``tools`` array shape.

        Kept separate from :meth:`to_dict` because the wire format carries the
        risk level nowhere: the model must not be told which tools are dangerous,
        or it will start reasoning about the policy instead of the task.
        """
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": dict(self.parameters),
            },
        }


@dataclass(frozen=True, slots=True)
class ToolResult:
    """The outcome of one invocation.

    A refusal (unknown tool, policy block, bad arguments) is reported here
    rather than raised: callers on the voice path must be able to turn any
    outcome into a sentence, and an exception would arrive as a silent turn.
    """

    ok: bool
    output: str = ""
    error: str = ""
    tool: str = ""
    """Which tool produced this, so a failure is attributable after the fact."""

    def to_dict(self) -> dict[str, object]:
        return {"ok": self.ok, "output": self.output, "error": self.error, "tool": self.tool}

    def as_text(self) -> str:
        """The one-line form handed back to the model or shown to the user."""
        if self.ok:
            return self.output
        return f"工具 {self.tool} 执行失败：{self.error}" if self.tool else self.error


type ToolHandler = Callable[[Mapping[str, object]], str]
"""What a tool's implementation looks like: validated arguments in, text out.

Text out (not a structured object) because the result is going into a prompt.
A tool that needs to return structure should serialise it — the model reads
JSON perfectly well, and one return type keeps the registry trivial.
"""


@dataclass(frozen=True, slots=True)
class ToolCall:
    """One finished invocation, kept for the operator to look back at.

    Deliberately thin: the name, whether it worked, how long it took, and one line
    of what came back. The registry holds forty of these and nothing more, so
    "what did the assistant just do to my machine" has an answer that does not
    require a log file — and does not grow without bound either.
    """

    at: float
    """Wall-clock seconds; the page formats them, Python does not bake in a locale."""

    tool: str
    ok: bool
    milliseconds: int
    detail: str

    def to_dict(self) -> dict[str, object]:
        return {
            "at": self.at,
            "tool": self.tool,
            "ok": self.ok,
            "milliseconds": self.milliseconds,
            "detail": self.detail,
        }


@dataclass(frozen=True, slots=True)
class RegisteredTool:
    """A spec paired with the callable that implements it."""

    spec: ToolSpec
    handler: ToolHandler
    """Takes validated arguments, returns text. May raise; the registry wraps."""


def object_schema(
    properties: Mapping[str, object],
    *,
    required: Sequence[str] = (),
) -> dict[str, object]:
    """Build a JSON Schema object, so no tool hand-writes the boilerplate.

    ``additionalProperties`` is left at its default (true): providers differ on
    whether they honour it, and a model that sends an extra key is better
    served by the argument validator saying so than by a schema violation.
    """
    schema: dict[str, object] = {"type": "object", "properties": dict(properties)}
    if required:
        schema["required"] = list(required)
    return schema


def string_property(description: str, *, default: str | None = None) -> dict[str, object]:
    """A JSON Schema string property."""
    schema: dict[str, object] = {"type": "string", "description": description}
    if default is not None:
        schema["default"] = default
    return schema


def integer_property(
    description: str, *, default: int | None = None, minimum: int | None = None
) -> dict[str, object]:
    """A JSON Schema integer property."""
    schema: dict[str, object] = {"type": "integer", "description": description}
    if default is not None:
        schema["default"] = default
    if minimum is not None:
        schema["minimum"] = minimum
    return schema


def boolean_property(description: str, *, default: bool | None = None) -> dict[str, object]:
    """A JSON Schema boolean property."""
    schema: dict[str, object] = {"type": "boolean", "description": description}
    if default is not None:
        schema["default"] = default
    return schema


_JSON_TYPES: Mapping[str, tuple[type, ...]] = {
    "string": (str,),
    "integer": (int,),
    "number": (int, float),
    "boolean": (bool,),
    "array": (list, tuple),
    "object": (dict,),
}


def validate_arguments(spec: ToolSpec, arguments: Mapping[str, object]) -> list[str]:
    """Check ``arguments`` against ``spec.parameters``; returns problems.

    An empty list means "call it". The check is deliberately shallow — required
    keys and scalar types, no ``$ref`` resolution — because its job is to turn a
    model's malformed call into a sentence the model can correct, not to be a
    second JSON Schema implementation.
    """
    problems: list[str] = []
    schema = spec.parameters
    properties = schema.get("properties")
    known = set(properties) if isinstance(properties, Mapping) else set()

    raw_required = schema.get("required")
    required = list(raw_required) if isinstance(raw_required, list) else []
    for key in required:
        if key not in arguments:
            problems.append(f"缺少必填参数 {key}")
    for key, value in arguments.items():
        if known and key not in known:
            problems.append(f"未知参数 {key}")
            continue
        if not isinstance(properties, Mapping):
            continue
        declared = properties.get(key)
        if not isinstance(declared, Mapping):
            continue
        expected = declared.get("type")
        if not isinstance(expected, str):
            continue
        allowed = _JSON_TYPES.get(expected)
        if allowed is None:
            continue
        # bool is an int subclass; a JSON "integer" must not accept true/false.
        if expected in {"integer", "number"} and isinstance(value, bool):
            problems.append(f"参数 {key} 应为 {expected}")
            continue
        if not isinstance(value, allowed):
            problems.append(f"参数 {key} 应为 {expected}")
    return problems


def normalise_tool_name(raw: str) -> str:
    """Coerce an arbitrary identifier into a valid tool name.

    MCP servers name their tools freely (``read_file``, ``fs.read``, ``读文件``),
    and a name with a dot or a Chinese character breaks every provider's
    function-calling schema. Anything outside ``[A-Za-z0-9_-]`` becomes ``_``;
    an empty result falls back to ``tool``.
    """
    cleaned = "".join(
        character if (character.isascii() and (character.isalnum() or character in "_-")) else "_"
        for character in raw.strip()
    )
    return cleaned.strip("_") or "tool"


__all__ = [
    "KNOWN_PERMISSIONS",
    "PERMISSION_NETWORK",
    "PERMISSION_SHELL",
    "PERMISSION_WRITE",
    "SOURCE_BUILTIN",
    "SOURCE_MCP_PREFIX",
    "SOURCE_PLUGIN_PREFIX",
    "RegisteredTool",
    "RiskLevel",
    "ToolHandler",
    "ToolResult",
    "ToolSpec",
    "boolean_property",
    "integer_property",
    "normalise_tool_name",
    "object_schema",
    "string_property",
    "validate_arguments",
]
