"""``McpService`` — the lifecycle component that owns every MCP connection.

One job: turn the configured servers into registered tools, and keep a bad
server from taking the good ones down with it. A server that fails to start is
recorded as an *error status*, not raised: the voice assistant must still come
up when a user's filesystem MCP server is misconfigured, and the UI needs to be
able to say which one is broken.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Callable, Mapping

from jarvis.config.schema import McpSection, McpServerSection
from jarvis.core.exceptions import McpError
from jarvis.mcp.client import McpClient
from jarvis.mcp.types import McpServerStatus, McpToolSpec
from jarvis.tools import SOURCE_MCP_PREFIX, RiskLevel, ToolRegistry, ToolSpec

logger = logging.getLogger("jarvis.mcp.service")

_MAX_TOOL_NAME_LENGTH = 64
"""Provider function-calling schemas cap tool names at 64 characters."""

_NAME_SAFE = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-")


def sanitise_tool_name(raw: str) -> str:
    """Coerce an arbitrary identifier into ``[A-Za-z0-9_-]``.

    MCP servers name tools freely (``fs.read``, ``读文件``, ``a b``), and a name
    with a dot, a space or a non-ASCII character is rejected by every provider's
    function-calling schema. Each offending character becomes ``_``; leading and
    trailing underscores are trimmed; an empty result falls back to ``tool`` so
    the name is never blank.
    """
    cleaned = "".join(character if character in _NAME_SAFE else "_" for character in raw.strip())
    return cleaned.strip("_") or "tool"


def qualified_tool_name(server: str, tool: str) -> str:
    """Build the registry name for an MCP tool, bounded to 64 characters.

    The ``mcp__<server>__<tool>`` shape guarantees a server tool can never
    shadow a built-in (which never carries the ``mcp__`` prefix). When the
    result would exceed the schema limit it is truncated and a short hash of the
    full name is appended, so two long names still get distinct ids.
    """
    base = f"mcp__{sanitise_tool_name(server)}__{sanitise_tool_name(tool)}"
    if len(base) <= _MAX_TOOL_NAME_LENGTH:
        return base
    digest = hashlib.sha1(base.encode("utf-8")).hexdigest()[:8]
    keep = _MAX_TOOL_NAME_LENGTH - len(digest) - 1
    return f"{base[:keep]}_{digest}"


class McpService:
    """Connect to configured MCP servers and bridge their tools into the registry."""

    name = "mcp"

    def __init__(
        self,
        settings_provider: Callable[[], McpSection],
        registry_provider: Callable[[], ToolRegistry],
        *,
        client_factory: Callable[[McpServerSection], McpClient] | None = None,
    ) -> None:
        """Create the service.

        Args:
            settings_provider: Returns the validated ``mcp`` config section. A
                provider rather than the section because configuration does not
                exist yet at registration time.
            registry_provider: Returns the shared tool registry. Injected so
                tests hand in a fake and no real registry is built.
            client_factory: Optional override that builds a client per server;
                tests use it to inject fake transports.
        """
        self._settings_provider = settings_provider
        self._registry_provider = registry_provider
        self._client_factory = client_factory
        self._clients: dict[str, McpClient] = {}
        self._status: dict[str, McpServerStatus] = {}
        self._tools: dict[str, tuple[McpToolSpec, ...]] = {}
        self._started = False

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        """Connect every enabled server (idempotent).

        Failures are isolated per server: one broken connection is recorded and
        skipped so the rest — and the application — keep going.
        """
        if self._started:
            return
        self._started = True
        settings = self._settings_provider()
        if not settings.enabled:
            logger.info("MCP 未启用，跳过服务器连接")
            return
        for server in settings.servers.values():
            if not server.enabled:
                self._status[server.name] = McpServerStatus(
                    name=server.name,
                    transport=server.transport,
                    connected=False,
                    tool_count=0,
                )
                continue
            self._connect(server, settings)
        logger.info(
            "MCP 服务就绪（%d/%d 个服务器已连接）",
            sum(1 for status in self._status.values() if status.connected),
            len(self._status),
        )

    def stop(self) -> None:
        """Unregister every bridged tool and close every connection (idempotent)."""
        for server_name, client in list(self._clients.items()):
            self._unregister(server_name)
            try:
                client.close()
            except Exception:  # pragma: no cover - closing must not break shutdown
                logger.warning("关闭 MCP 客户端失败：%s", server_name, exc_info=True)
        self._clients.clear()
        self._tools.clear()
        self._status.clear()
        self._started = False

    @property
    def running(self) -> bool:
        """Whether :meth:`start` has run."""
        return self._started

    # -- queries -----------------------------------------------------------

    def servers(self) -> list[McpServerStatus]:
        """Connection status of every configured server, sorted by name."""
        return [self._status[name] for name in sorted(self._status)]

    def tools(self) -> list[McpToolSpec]:
        """Every tool discovered across all connected servers."""
        result: list[McpToolSpec] = []
        for server_name in sorted(self._tools):
            result.extend(self._tools[server_name])
        return result

    def stats(self) -> dict[str, object]:
        """A JSON-ready snapshot for the UI. Never raises."""
        try:
            enabled = bool(self._settings_provider().enabled)
        except Exception:  # pragma: no cover - config provider is injected
            enabled = False
        connected = sum(1 for status in self._status.values() if status.connected)
        return {
            "enabled": enabled,
            "running": self._started,
            "servers": len(self._status),
            "connected": connected,
            "tools": sum(len(tools) for tools in self._tools.values()),
            "errors": {
                name: status.error for name, status in sorted(self._status.items()) if status.error
            },
        }

    # -- operations --------------------------------------------------------

    def call(self, server: str, tool: str, arguments: Mapping[str, object]) -> str:
        """Call a tool on a named server.

        Raises:
            McpError: if the server is unknown or not connected, or the call
                itself fails or times out.
        """
        client = self._clients.get(server)
        if client is None:
            raise McpError(
                f"未知的 MCP 服务器：{server}",
                details={"server": server, "available": sorted(self._clients)},
            )
        return client.call_tool(tool, arguments)

    # -- internals ---------------------------------------------------------

    def _connect(self, server: McpServerSection, settings: McpSection) -> None:
        client: McpClient | None = None
        try:
            client = (
                self._client_factory(server)
                if self._client_factory is not None
                else McpClient(
                    server,
                    connect_timeout_seconds=settings.connect_timeout_seconds,
                    call_timeout_seconds=settings.call_timeout_seconds,
                )
            )
            client.connect()
            tools = client.list_tools()
        except McpError as exc:
            logger.warning("MCP 服务器 %s 连接失败：%s", server.name, exc)
            self._record_failure(server, str(exc), client)
            return
        except Exception as exc:  # a third-party transport may raise anything
            logger.exception("MCP 服务器 %s 连接时发生未预期错误", server.name)
            self._record_failure(server, f"{type(exc).__name__}: {exc}", client)
            return
        self._clients[server.name] = client
        self._tools[server.name] = tuple(tools)
        self._status[server.name] = McpServerStatus(
            name=server.name,
            transport=server.transport,
            connected=True,
            tool_count=len(tools),
        )
        self._register(server.name, client, tools)

    def _record_failure(
        self, server: McpServerSection, message: str, client: McpClient | None
    ) -> None:
        if client is not None:
            try:
                client.close()
            except Exception:  # pragma: no cover - best effort cleanup
                logger.debug("清理失败的 MCP 客户端时出错：%s", server.name, exc_info=True)
        self._status[server.name] = McpServerStatus(
            name=server.name,
            transport=server.transport,
            connected=False,
            tool_count=0,
            error=message,
        )

    def _register(self, server_name: str, client: McpClient, tools: list[McpToolSpec]) -> None:
        try:
            registry = self._registry_provider()
        except Exception:  # pragma: no cover - registry provider is injected
            logger.exception("无法获取工具注册表，MCP 工具 %s 未注册", server_name)
            return
        source = f"{SOURCE_MCP_PREFIX}{server_name}"
        for tool in tools:
            spec = ToolSpec(
                name=qualified_tool_name(server_name, tool.name),
                description=_describe(server_name, tool),
                parameters=dict(tool.input_schema),
                risk=RiskLevel.CAUTION,
                source=source,
            )
            try:
                registry.register(spec, _make_handler(client, tool.name))
            except Exception:  # pragma: no cover - registry is ours
                logger.exception("注册 MCP 工具失败：%s", spec.name)

    def _unregister(self, server_name: str) -> None:
        try:
            registry = self._registry_provider()
        except Exception:  # pragma: no cover - registry provider is injected
            logger.warning("无法获取工具注册表，跳过 MCP 工具注销：%s", server_name)
            return
        try:
            registry.unregister_source(f"{SOURCE_MCP_PREFIX}{server_name}")
        except Exception:  # pragma: no cover - registry is ours
            logger.warning("注销 MCP 工具失败：%s", server_name, exc_info=True)


def _describe(server_name: str, tool: McpToolSpec) -> str:
    """Prefix the description so the model can tell which server a tool came from."""
    detail = tool.description.strip() or tool.name
    return f"[MCP:{server_name}] {detail}"


def _make_handler(client: McpClient, tool_name: str) -> Callable[[Mapping[str, object]], str]:
    """Bind a handler that forwards to the server's tool of the same name."""

    def handler(arguments: Mapping[str, object]) -> str:
        return client.call_tool(tool_name, arguments)

    return handler


__all__ = ["McpService", "qualified_tool_name", "sanitise_tool_name"]
