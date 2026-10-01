"""MCP vocabulary: what a discovered tool looks like and how a channel behaves.

These types are the seam between the wire protocol (JSON-RPC over stdio or
HTTP) and the rest of JARVIS. Keeping them as plain frozen dataclasses means a
server's tool list can be serialised straight to the UI without a translation
step, and tests can build one without touching a subprocess.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

MCP_PROTOCOL_VERSION: str = "2024-11-05"
"""Protocol revision JARVIS speaks.

The MCP handshake is versioned on purpose: a server that only understands an
older revision is allowed to answer with its own version, and the client is
expected to keep going rather than abort. Pinning a constant here documents the
one we advertise in ``initialize``.
"""


@dataclass(frozen=True, slots=True)
class McpToolSpec:
    """One tool advertised by one MCP server.

    ``input_schema`` is the server's own JSON Schema, kept verbatim: JARVIS does
    not rewrite a third-party tool's contract, it only prefixes the *name* so a
    collision with a built-in cannot happen.
    """

    server: str
    """Configured server name the tool belongs to."""

    name: str
    """Tool name as the server calls it (may contain characters JARVIS has to
    sanitise before it reaches a provider's function-calling schema)."""

    description: str
    """Human/model-readable description, forwarded unchanged."""

    input_schema: Mapping[str, object]
    """JSON Schema object for the arguments (``{"type": "object", ...}``)."""

    def to_dict(self) -> dict[str, object]:
        return {
            "server": self.server,
            "name": self.name,
            "description": self.description,
            "input_schema": dict(self.input_schema),
        }


@dataclass(frozen=True, slots=True)
class McpServerStatus:
    """The connection state of one configured server, for the UI.

    A failure is data, not an exception: one unreachable server must leave the
    others working and must be *visible* as an error rather than silently
    missing from the list.
    """

    name: str
    transport: str
    connected: bool
    tool_count: int
    error: str = ""
    """Empty when ``connected``; the reason otherwise."""

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "transport": self.transport,
            "connected": self.connected,
            "tool_count": self.tool_count,
            "error": self.error,
        }


@runtime_checkable
class McpTransport(Protocol):
    """One request/response JSON-RPC channel to a server.

    Deliberately minimal: the client only ever sends a request and waits for
    its answer, so the transport does not need streaming, batching or session
    management. Tests satisfy this protocol with a fake and never spawn a
    process or open a socket.
    """

    def request(self, method: str, params: Mapping[str, object]) -> Mapping[str, object]:
        """Send a request and return its ``result`` object.

        Raises:
            McpError: on timeout, transport failure, or a JSON-RPC error reply.
        """
        ...

    def notify(self, method: str, params: Mapping[str, object]) -> None:
        """Send a one-way notification (no reply expected)."""
        ...

    def close(self) -> None:
        """Release the underlying process/socket. Idempotent."""
        ...


__all__ = [
    "MCP_PROTOCOL_VERSION",
    "McpServerStatus",
    "McpToolSpec",
    "McpTransport",
]
