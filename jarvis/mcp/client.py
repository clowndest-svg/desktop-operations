"""One connection to one MCP server: handshake, list tools, call a tool.

The client owns the request sequence the protocol prescribes —
``initialize`` → ``notifications/initialized`` → ``tools/list`` → ``tools/call``
— and nothing else. Server lifecycle, tool registration and failure isolation
belong to :class:`~jarvis.mcp.service.McpService`; keeping them out of here
means this class stays testable with a single fake transport.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping

from jarvis import __version__
from jarvis.config.schema import McpServerSection
from jarvis.core.constants import APP_NAME
from jarvis.core.exceptions import McpError
from jarvis.mcp.transport import HttpTransport, StdioTransport, TimeoutAware
from jarvis.mcp.types import MCP_PROTOCOL_VERSION, McpToolSpec, McpTransport

logger = logging.getLogger("jarvis.mcp.client")

_EMPTY_SCHEMA: Mapping[str, object] = {"type": "object", "properties": {}}
"""Fallback when a server omits ``inputSchema``; the model still needs an object."""


class McpClient:
    """A live conversation with one configured MCP server."""

    def __init__(
        self,
        config: McpServerSection,
        *,
        transport: McpTransport | None = None,
        connect_timeout_seconds: float,
        call_timeout_seconds: float,
    ) -> None:
        """Create the client.

        Args:
            config: The validated server entry (transport, command/url, env).
            transport: Injected channel; tests pass a fake so no process is
                spawned. ``None`` builds the real one from ``config``.
            connect_timeout_seconds: Budget for ``initialize`` (a server may
                spend seconds loading a model before it answers).
            call_timeout_seconds: Budget for each ``tools/call`` afterwards.
        """
        self._config = config
        self._transport = transport
        self._connect_timeout_seconds = float(connect_timeout_seconds)
        self._call_timeout_seconds = float(call_timeout_seconds)
        self._connected = False
        self._tools: list[McpToolSpec] = []

    # -- identity ----------------------------------------------------------

    @property
    def name(self) -> str:
        """The configured server name."""
        return self._config.name

    @property
    def connected(self) -> bool:
        """Whether the handshake has completed."""
        return self._connected

    @property
    def tools(self) -> list[McpToolSpec]:
        """Tools discovered by the last :meth:`list_tools` call."""
        return list(self._tools)

    # -- lifecycle ---------------------------------------------------------

    def connect(self) -> None:
        """Perform the MCP handshake (idempotent).

        Raises:
            McpError: if the transport cannot be built or the server does not
                complete ``initialize`` in time. The transport is closed on
                failure so a half-open process never leaks.
        """
        if self._connected:
            return
        transport = self._transport or self._build_transport()
        self._apply_timeout(transport, self._connect_timeout_seconds)
        try:
            transport.request(
                "initialize",
                {
                    "protocolVersion": MCP_PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": APP_NAME.lower(), "version": __version__},
                },
            )
            transport.notify("notifications/initialized", {})
        except McpError:
            transport.close()
            raise
        except Exception as exc:  # a third-party transport may raise anything
            transport.close()
            raise McpError(
                f"MCP 服务器 {self._config.name} 初始化失败：{exc}",
                details={"server": self._config.name, "reason": str(exc)},
            ) from exc
        self._transport = transport
        self._connected = True
        self._apply_timeout(transport, self._call_timeout_seconds)
        logger.info("MCP 服务器已连接：%s (%s)", self._config.name, self._config.transport)

    def list_tools(self) -> list[McpToolSpec]:
        """Fetch the server's tool list.

        Raises:
            McpError: if not connected or the reply is malformed.
        """
        result = self._require_transport().request("tools/list", {})
        raw = result.get("tools")
        if not isinstance(raw, list):
            raise McpError(
                f"MCP 服务器 {self._config.name} 的 tools/list 响应缺少 tools 数组",
                details={"server": self._config.name},
            )
        tools: list[McpToolSpec] = []
        for item in raw:
            parsed = self._parse_tool(item)
            if parsed is not None:
                tools.append(parsed)
        self._tools = tools
        return list(tools)

    def call_tool(self, name: str, arguments: Mapping[str, object]) -> str:
        """Invoke ``name`` and return its text output.

        Raises:
            McpError: if not connected, the call times out, or the server
                reports ``isError``.
        """
        result = self._require_transport().request(
            "tools/call", {"name": name, "arguments": dict(arguments)}
        )
        text = _extract_text(result)
        if result.get("isError") is True:
            raise McpError(
                f"MCP 工具 {name} 执行失败：{text or '服务器未提供错误详情'}",
                details={"server": self._config.name, "tool": name},
            )
        return text

    def close(self) -> None:
        """Close the transport; idempotent and never raises."""
        transport = self._transport
        self._transport = None
        self._connected = False
        self._tools = []
        if transport is None:
            return
        try:
            transport.close()
        except Exception:  # pragma: no cover - closing must not break shutdown
            logger.warning("关闭 MCP 传输失败：%s", self._config.name, exc_info=True)

    # -- internals ---------------------------------------------------------

    def _build_transport(self) -> McpTransport:
        config = self._config
        if config.transport == "stdio":
            return StdioTransport(
                command=config.command,
                args=config.args,
                env=config.env,
                timeout_seconds=self._connect_timeout_seconds,
            )
        if config.transport == "http":
            return HttpTransport(url=config.url, timeout_seconds=self._connect_timeout_seconds)
        raise McpError(
            f"不支持的 MCP 传输方式：{config.transport}",
            details={"server": config.name, "transport": config.transport},
        )

    @staticmethod
    def _apply_timeout(transport: McpTransport, seconds: float) -> None:
        """Retune a real transport's per-request deadline; fakes are skipped."""
        if isinstance(transport, TimeoutAware):
            transport.timeout_seconds = seconds

    def _require_transport(self) -> McpTransport:
        transport = self._transport
        if transport is None or not self._connected:
            raise McpError(
                f"MCP 服务器 {self._config.name} 未连接",
                details={"server": self._config.name},
            )
        return transport

    def _parse_tool(self, item: object) -> McpToolSpec | None:
        if not isinstance(item, Mapping):
            logger.warning("忽略 MCP 服务器 %s 的非法工具条目", self._config.name)
            return None
        name = item.get("name")
        if not isinstance(name, str) or not name.strip():
            logger.warning("忽略 MCP 服务器 %s 的无名工具", self._config.name)
            return None
        description = item.get("description")
        schema = item.get("inputSchema")
        return McpToolSpec(
            server=self._config.name,
            name=name,
            description=description if isinstance(description, str) else "",
            input_schema=dict(schema) if isinstance(schema, Mapping) else dict(_EMPTY_SCHEMA),
        )


def _extract_text(result: Mapping[str, object]) -> str:
    """Flatten an MCP ``CallToolResult`` into the text the model should see."""
    content = result.get("content")
    parts: list[str] = []
    if isinstance(content, list):
        for item in content:
            if isinstance(item, Mapping):
                text = item.get("text")
                if isinstance(text, str):
                    parts.append(text)
    if parts:
        return "\n".join(parts)
    structured = result.get("structuredContent")
    if structured is not None:
        return json.dumps(structured, ensure_ascii=False)
    return ""


__all__ = ["McpClient"]
