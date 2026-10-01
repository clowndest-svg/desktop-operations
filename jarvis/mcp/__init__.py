"""MCP integration: connect external Model Context Protocol servers.

Responsibility (delivered in phase 15):
    * MCP client, server registry driven purely by user configuration
      (filesystem, GitHub, SQLite, Postgres, Playwright, Notion, ...);
      discovered MCP tools are bridged into the ``tools`` registry so no
      code change is needed to add a new server.

Allowed dependencies: ``core``, ``config``, ``tools``.

The official ``mcp`` SDK is deliberately not a dependency: the protocol is
JSON-RPC over stdio or HTTP, both of which the standard library speaks, so the
transports here are built on ``subprocess``/``urllib`` (mirroring
:mod:`jarvis.llm.transport`).
"""

from jarvis.mcp.client import McpClient
from jarvis.mcp.service import McpService, qualified_tool_name, sanitise_tool_name
from jarvis.mcp.transport import HttpTransport, StdioTransport
from jarvis.mcp.types import (
    MCP_PROTOCOL_VERSION,
    McpServerStatus,
    McpToolSpec,
    McpTransport,
)

__all__ = [
    "MCP_PROTOCOL_VERSION",
    "HttpTransport",
    "McpClient",
    "McpServerStatus",
    "McpService",
    "McpToolSpec",
    "McpTransport",
    "StdioTransport",
    "qualified_tool_name",
    "sanitise_tool_name",
]
