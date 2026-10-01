"""Tests for the MCP layer (jarvis.mcp).

Everything here runs against fakes: no child process is spawned and no socket
is opened. The transports are exercised through :class:`_LineChannel` (the
framing/id-correlation core) with two plain callables standing in for a
server's stdin/stdout, and :class:`McpClient` / :class:`McpService` are driven
by a scripted fake transport. That keeps the suite fast and deterministic while
still covering the protocol shapes a real server would send.
"""

from __future__ import annotations

import json
import queue
from collections.abc import Mapping

import pytest

from jarvis.config.schema import McpSection, McpServerSection, ToolsSection
from jarvis.core.exceptions import McpError
from jarvis.mcp import (
    MCP_PROTOCOL_VERSION,
    McpClient,
    McpServerStatus,
    McpService,
    McpToolSpec,
    qualified_tool_name,
    sanitise_tool_name,
)
from jarvis.mcp.transport import _decode, _encode, _LineChannel
from jarvis.tools import RiskLevel, ToolRegistry

# ---------------------------------------------------------------------------
# Fakes and helpers
# ---------------------------------------------------------------------------


class FakeTransport:
    """A scripted :class:`McpTransport`: records calls, returns canned results."""

    def __init__(
        self,
        responses: dict[str, object] | None = None,
        *,
        fail_on: str | None = None,
    ) -> None:
        self.requests: list[tuple[str, dict[str, object]]] = []
        self.notifications: list[tuple[str, dict[str, object]]] = []
        self.closed = False
        self._responses = responses or {}
        self._fail_on = fail_on

    def request(self, method: str, params: Mapping[str, object]) -> dict[str, object]:
        self.requests.append((method, dict(params)))
        if self._fail_on == method:
            raise McpError(f"MCP 请求 {method} 超时", details={"method": method})
        result = self._responses.get(method, {})
        if isinstance(result, Exception):
            raise result
        assert isinstance(result, dict)
        return result

    def notify(self, method: str, params: Mapping[str, object]) -> None:
        self.notifications.append((method, dict(params)))

    def close(self) -> None:
        self.closed = True


def _server(
    name: str = "fs", *, enabled: bool = True, transport: str = "stdio"
) -> McpServerSection:
    return McpServerSection(
        name=name,
        transport=transport,
        command="mcp-server" if transport == "stdio" else "",
        args=(),
        url="http://localhost:1234/mcp" if transport == "http" else "",
        env={},
        enabled=enabled,
    )


def _settings(servers: dict[str, McpServerSection], *, enabled: bool = True) -> McpSection:
    return McpSection(
        enabled=enabled,
        connect_timeout_seconds=1.0,
        call_timeout_seconds=1.0,
        servers=servers,
    )


def _tools_section() -> ToolsSection:
    return ToolsSection(
        enabled=True,
        confirm_dangerous=True,
        allow_write=False,
        allow_shell=False,
        file_roots=(),
        max_result_chars=10_000,
    )


def _registry() -> ToolRegistry:
    return ToolRegistry(_tools_section)


_TOOL_LIST_RESPONSE: dict[str, object] = {
    "tools": [
        {
            "name": "read_file",
            "description": "读取文件内容",
            "inputSchema": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        },
        {
            "name": "fs.stat",
            "description": "",
        },
    ]
}


def _client(transport: FakeTransport) -> McpClient:
    return McpClient(
        _server(),
        transport=transport,
        connect_timeout_seconds=1.0,
        call_timeout_seconds=1.0,
    )


# ---------------------------------------------------------------------------
# Client: handshake and protocol shapes
# ---------------------------------------------------------------------------


class TestMcpClient:
    def test_initialize_advertises_protocol_version_and_client_info(self) -> None:
        """The handshake is versioned and named; a server decides compatibility
        from these exact fields, so a wrong shape means a refused connection."""
        transport = FakeTransport()
        _client(transport).connect()

        method, params = transport.requests[0]
        assert method == "initialize"
        assert params["protocolVersion"] == MCP_PROTOCOL_VERSION
        assert params["capabilities"] == {}
        client_info = params["clientInfo"]
        assert isinstance(client_info, dict)
        assert client_info["name"] == "jarvis"
        assert isinstance(client_info["version"], str)

    def test_initialized_notification_follows_the_handshake(self) -> None:
        """MCP requires ``notifications/initialized`` before any other request;
        servers that never receive it reject ``tools/list``."""
        transport = FakeTransport()
        _client(transport).connect()
        assert transport.notifications == [("notifications/initialized", {})]

    def test_connect_is_idempotent(self) -> None:
        transport = FakeTransport()
        client = _client(transport)
        client.connect()
        client.connect()
        assert [method for method, _ in transport.requests] == ["initialize"]

    def test_initialize_failure_closes_the_transport(self) -> None:
        """A half-open connection must not leak a child process when the
        handshake fails."""
        transport = FakeTransport(fail_on="initialize")
        with pytest.raises(McpError):
            _client(transport).connect()
        assert transport.closed

    def test_list_tools_parses_schema_and_defaults_missing_one(self) -> None:
        transport = FakeTransport({"tools/list": _TOOL_LIST_RESPONSE})
        client = _client(transport)
        client.connect()

        tools = client.list_tools()

        assert [tool.name for tool in tools] == ["read_file", "fs.stat"]
        first = tools[0]
        assert first.server == "fs"
        assert first.description == "读取文件内容"
        assert first.input_schema["required"] == ["path"]
        # A server may omit inputSchema; the model still needs an object schema.
        assert tools[1].input_schema == {"type": "object", "properties": {}}

    def test_list_tools_rejects_a_malformed_reply(self) -> None:
        transport = FakeTransport({"tools/list": {"nope": True}})
        client = _client(transport)
        client.connect()
        with pytest.raises(McpError, match="tools"):
            client.list_tools()

    def test_call_tool_joins_text_content(self) -> None:
        transport = FakeTransport(
            {
                "tools/call": {
                    "content": [
                        {"type": "text", "text": "第一行"},
                        {"type": "text", "text": "第二行"},
                    ]
                }
            }
        )
        client = _client(transport)
        client.connect()
        assert client.call_tool("read_file", {"path": "a.txt"}) == "第一行\n第二行"
        assert transport.requests[-1] == (
            "tools/call",
            {"name": "read_file", "arguments": {"path": "a.txt"}},
        )

    def test_call_tool_raises_on_server_reported_error(self) -> None:
        """``isError`` is a failed call, not a successful one with odd text."""
        transport = FakeTransport(
            {"tools/call": {"isError": True, "content": [{"type": "text", "text": "权限不足"}]}}
        )
        client = _client(transport)
        client.connect()
        with pytest.raises(McpError, match="权限不足"):
            client.call_tool("read_file", {"path": "a.txt"})

    def test_calls_before_connect_are_refused(self) -> None:
        client = _client(FakeTransport())
        with pytest.raises(McpError, match="未连接"):
            client.list_tools()

    def test_timeout_from_the_transport_propagates_as_mcp_error(self) -> None:
        transport = FakeTransport(fail_on="tools/call")
        client = _client(transport)
        client.connect()
        with pytest.raises(McpError, match="超时"):
            client.call_tool("read_file", {"path": "a.txt"})


# ---------------------------------------------------------------------------
# Stdio framing: the JSON-RPC codec, driven without a process
# ---------------------------------------------------------------------------


class TestStdioFraming:
    def test_request_encodes_jsonrpc_and_correlates_by_id(self) -> None:
        """The wire shape and the id round-trip are the whole stdio protocol;
        getting either wrong makes every reply look like someone else's."""
        written: list[str] = []
        incoming: queue.Queue[str | None] = queue.Queue()

        def write(line: str) -> None:
            written.append(line)
            request = json.loads(line)
            reply = {"jsonrpc": "2.0", "id": request["id"], "result": {"ok": True}}
            incoming.put(json.dumps(reply))

        channel = _LineChannel(write, incoming)
        assert channel.request("tools/list", {"cursor": "x"}, 1.0) == {"ok": True}

        payload = json.loads(written[0])
        assert payload["jsonrpc"] == "2.0"
        assert payload["id"] == 1
        assert payload["method"] == "tools/list"
        assert payload["params"] == {"cursor": "x"}

    def test_notification_carries_no_id(self) -> None:
        """A notification with an id would make the server answer it, and the
        unsolicited reply would be mistaken for a response to something else."""
        written: list[str] = []
        channel = _LineChannel(written.append, queue.Queue())
        channel.notify("notifications/initialized", {})
        payload = json.loads(written[0])
        assert "id" not in payload
        assert payload["method"] == "notifications/initialized"

    def test_skips_notifications_and_unrelated_replies(self) -> None:
        """Servers push log notifications and may interleave replies; the
        channel must ignore everything that is not its own answer."""
        incoming: queue.Queue[str | None] = queue.Queue()
        incoming.put(json.dumps({"jsonrpc": "2.0", "method": "notifications/progress"}))
        incoming.put(json.dumps({"jsonrpc": "2.0", "id": 99, "result": {"other": True}}))
        incoming.put(json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"mine": True}}))

        channel = _LineChannel(lambda _line: None, incoming)
        assert channel.request("tools/list", {}, 1.0) == {"mine": True}

    def test_timeout_raises_mcp_error_with_the_budget(self) -> None:
        channel = _LineChannel(lambda _line: None, queue.Queue())
        with pytest.raises(McpError) as info:
            channel.request("tools/list", {}, 0.01)
        assert info.value.details["timeout_seconds"] == 0.01

    def test_closed_channel_raises_instead_of_hanging(self) -> None:
        """EOF is put on the queue as ``None``; without it a dead server would
        leave every call blocked until its timeout."""
        incoming: queue.Queue[str | None] = queue.Queue()
        incoming.put(None)
        channel = _LineChannel(lambda _line: None, incoming)
        with pytest.raises(McpError, match="已关闭"):
            channel.request("tools/list", {}, 1.0)

    def test_jsonrpc_error_reply_becomes_mcp_error(self) -> None:
        incoming: queue.Queue[str | None] = queue.Queue()
        incoming.put(
            json.dumps(
                {"jsonrpc": "2.0", "id": 1, "error": {"code": -32601, "message": "未知方法"}}
            )
        )
        channel = _LineChannel(lambda _line: None, incoming)
        with pytest.raises(McpError, match="未知方法"):
            channel.request("tools/list", {}, 1.0)

    def test_decode_ignores_noise_and_blank_lines(self) -> None:
        assert _decode("   ") is None
        assert _decode("not json") is None
        assert _decode("[1, 2]") is None
        assert _decode('{"a": 1}') == {"a": 1}

    def test_encode_keeps_non_ascii_readable(self) -> None:
        assert "读取" in _encode({"description": "读取"})


# ---------------------------------------------------------------------------
# Tool-name sanitisation
# ---------------------------------------------------------------------------


class TestToolNames:
    def test_dots_and_spaces_become_underscores(self) -> None:
        """Provider function-calling schemas accept ``[A-Za-z0-9_-]`` only; a
        dot would make the whole tool list invalid."""
        assert sanitise_tool_name("fs.read") == "fs_read"
        assert sanitise_tool_name("a b") == "a_b"

    def test_non_ascii_names_fall_back(self) -> None:
        assert sanitise_tool_name("读文件") == "tool"
        assert sanitise_tool_name("") == "tool"

    def test_qualified_name_prefixes_server_and_tool(self) -> None:
        assert qualified_tool_name("my server", "fs.read") == "mcp__my_server__fs_read"

    def test_qualified_name_is_bounded_for_provider_schemas(self) -> None:
        name = qualified_tool_name("s" * 80, "t" * 80)
        assert len(name) == 64
        assert name.startswith("mcp__")

    def test_long_names_stay_distinct(self) -> None:
        first = qualified_tool_name("s" * 80, "t" * 80)
        second = qualified_tool_name("s" * 80, "t" * 79 + "u")
        assert first != second


# ---------------------------------------------------------------------------
# Service: lifecycle, isolation, registration
# ---------------------------------------------------------------------------


def _service(
    registry: ToolRegistry,
    settings: McpSection,
    transports: dict[str, FakeTransport],
) -> McpService:
    def factory(server: McpServerSection) -> McpClient:
        return McpClient(
            server,
            transport=transports[server.name],
            connect_timeout_seconds=1.0,
            call_timeout_seconds=1.0,
        )

    return McpService(lambda: settings, lambda: registry, client_factory=factory)


class TestMcpService:
    def test_start_registers_tools_as_caution_sourced_from_the_server(self) -> None:
        registry = _registry()
        transport = FakeTransport({"tools/list": _TOOL_LIST_RESPONSE})
        service = _service(registry, _settings({"fs": _server()}), {"fs": transport})

        service.start()

        assert service.running
        assert registry.names() == ["mcp__fs__fs_stat", "mcp__fs__read_file"]
        registered = registry.get("mcp__fs__read_file")
        assert registered is not None
        assert registered.spec.source == "mcp:fs"
        assert registered.spec.risk is RiskLevel.CAUTION
        assert registered.spec.parameters["required"] == ["path"]

    def test_registered_tool_invokes_the_server(self) -> None:
        """The bridge must be transparent: calling the registry tool has to
        reach ``tools/call`` on the server with the arguments unchanged."""
        registry = _registry()
        transport = FakeTransport(
            {
                "tools/list": _TOOL_LIST_RESPONSE,
                "tools/call": {"content": [{"type": "text", "text": "文件内容"}]},
            }
        )
        service = _service(registry, _settings({"fs": _server()}), {"fs": transport})
        service.start()

        result = registry.invoke("mcp__fs__read_file", {"path": "a.txt"})

        assert result.ok
        assert result.output == "文件内容"
        assert (
            "tools/call",
            {"name": "read_file", "arguments": {"path": "a.txt"}},
        ) in transport.requests

    def test_one_failed_server_does_not_take_down_the_others(self) -> None:
        """A misconfigured server must degrade to an error status, never stop
        the application or the healthy servers next to it."""
        registry = _registry()
        good = FakeTransport({"tools/list": _TOOL_LIST_RESPONSE})
        bad = FakeTransport(fail_on="initialize")
        settings = _settings({"good": _server("good"), "bad": _server("bad")})
        service = _service(registry, settings, {"good": good, "bad": bad})

        service.start()

        statuses = {status.name: status for status in service.servers()}
        assert statuses["good"].connected
        assert statuses["good"].tool_count == 2
        assert not statuses["bad"].connected
        assert statuses["bad"].error
        assert registry.names() == ["mcp__good__fs_stat", "mcp__good__read_file"]
        assert bad.closed

    def test_disabled_servers_are_listed_but_not_connected(self) -> None:
        registry = _registry()
        settings = _settings({"off": _server("off", enabled=False)})
        service = _service(registry, settings, {})

        service.start()

        status = service.servers()[0]
        assert status.name == "off"
        assert not status.connected
        assert registry.names() == []

    def test_stop_unregisters_every_bridged_tool_and_closes_transports(self) -> None:
        registry = _registry()
        transport = FakeTransport({"tools/list": _TOOL_LIST_RESPONSE})
        service = _service(registry, _settings({"fs": _server()}), {"fs": transport})
        service.start()
        assert registry.names()

        service.stop()

        assert registry.names() == []
        assert transport.closed
        assert not service.running
        assert service.servers() == []

    def test_call_reports_an_unknown_server(self) -> None:
        service = _service(_registry(), _settings({}), {})
        service.start()
        with pytest.raises(McpError, match="未知的 MCP 服务器"):
            service.call("nope", "read_file", {})

    def test_call_propagates_a_transport_timeout_as_mcp_error(self) -> None:
        registry = _registry()
        transport = FakeTransport({"tools/list": _TOOL_LIST_RESPONSE}, fail_on="tools/call")
        service = _service(registry, _settings({"fs": _server()}), {"fs": transport})
        service.start()
        with pytest.raises(McpError, match="超时"):
            service.call("fs", "read_file", {"path": "a.txt"})

    def test_tools_and_stats_are_always_available(self) -> None:
        """These are polled by the UI; a disabled service must answer, not raise."""
        service = _service(_registry(), _settings({}, enabled=False), {})
        service.start()

        assert service.tools() == []
        stats = service.stats()
        assert stats["enabled"] is False
        assert stats["running"] is True
        assert stats["tools"] == 0

    def test_stats_reports_tool_count_and_errors(self) -> None:
        registry = _registry()
        good = FakeTransport({"tools/list": _TOOL_LIST_RESPONSE})
        bad = FakeTransport(fail_on="initialize")
        settings = _settings({"good": _server("good"), "bad": _server("bad")})
        service = _service(registry, settings, {"good": good, "bad": bad})
        service.start()

        stats = service.stats()
        assert stats["connected"] == 1
        assert stats["tools"] == 2
        errors = stats["errors"]
        assert isinstance(errors, dict)
        assert "bad" in errors

    def test_start_is_idempotent(self) -> None:
        registry = _registry()
        transport = FakeTransport({"tools/list": _TOOL_LIST_RESPONSE})
        service = _service(registry, _settings({"fs": _server()}), {"fs": transport})
        service.start()
        service.start()
        assert [method for method, _ in transport.requests].count("initialize") == 1


class TestMcpTypes:
    def test_tool_spec_and_status_serialise_for_the_ui(self) -> None:
        spec = McpToolSpec(
            server="fs", name="read", description="读", input_schema={"type": "object"}
        )
        assert spec.to_dict() == {
            "server": "fs",
            "name": "read",
            "description": "读",
            "input_schema": {"type": "object"},
        }
        status = McpServerStatus(
            name="fs", transport="stdio", connected=False, tool_count=0, error="x"
        )
        assert status.to_dict()["error"] == "x"
