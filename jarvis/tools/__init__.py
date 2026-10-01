"""Tool calling framework and built-in tools.

Responsibility (delivered in phase 12):
    * Tool interface, JSON-schema parameter models, auto-registration and
      dynamic discovery, dangerous-tool confirmation policy, and the
      built-in tool set (files, shell, search, office docs, system info,
      clipboard, notifications, ...).

How a call actually flows: ``ToolService.invoke`` → ``ToolRegistry.invoke`` →
``ToolPolicy.check`` (risk + permission) → ``validate_arguments`` (schema) →
the handler. Everything that can add a tool — the built-ins, an MCP bridge, a
plugin — registers into the same registry, which is the only reason one policy
can govern all three.

Allowed dependencies: ``core``, ``config``.
"""

from jarvis.tools.policy import ToolPolicy
from jarvis.tools.registry import TRUNCATION_NOTE, ToolRegistry
from jarvis.tools.safe_eval import (
    MAX_POWER,
    ExpressionError,
    evaluate,
    evaluate_text,
    extract_expression,
)
from jarvis.tools.service import ToolService
from jarvis.tools.types import (
    KNOWN_PERMISSIONS,
    PERMISSION_NETWORK,
    PERMISSION_SHELL,
    PERMISSION_WRITE,
    SOURCE_BUILTIN,
    SOURCE_MCP_PREFIX,
    SOURCE_PLUGIN_PREFIX,
    RegisteredTool,
    RiskLevel,
    ToolHandler,
    ToolResult,
    ToolSpec,
    boolean_property,
    integer_property,
    normalise_tool_name,
    object_schema,
    string_property,
    validate_arguments,
)

__all__ = [
    "KNOWN_PERMISSIONS",
    "MAX_POWER",
    "PERMISSION_NETWORK",
    "PERMISSION_SHELL",
    "PERMISSION_WRITE",
    "SOURCE_BUILTIN",
    "SOURCE_MCP_PREFIX",
    "SOURCE_PLUGIN_PREFIX",
    "TRUNCATION_NOTE",
    "ExpressionError",
    "RegisteredTool",
    "RiskLevel",
    "ToolHandler",
    "ToolPolicy",
    "ToolRegistry",
    "ToolResult",
    "ToolService",
    "ToolSpec",
    "boolean_property",
    "evaluate",
    "evaluate_text",
    "extract_expression",
    "integer_property",
    "normalise_tool_name",
    "object_schema",
    "string_property",
    "validate_arguments",
]
