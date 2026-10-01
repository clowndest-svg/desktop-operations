"""JSON-RPC transports for MCP servers.

Two wire formats are supported, both implemented on the standard library so
JARVIS never has to ship the official ``mcp`` SDK (and its dependency tree)
just to talk to a server:

* :class:`StdioTransport` spawns a child process and exchanges newline-delimited
  JSON-RPC over its stdin/stdout — the transport every local MCP server uses.
* :class:`HttpTransport` POSTs JSON-RPC to a URL (the "streamable HTTP"
  transport), using ``urllib`` exactly like :mod:`jarvis.llm.transport` does,
  so ``httpx``/``requests`` stay out of the dependency list.

The framing and id-correlation logic lives in :class:`_LineChannel`, which is
deliberately independent of ``subprocess``: a test drives it with two plain
callables and never spawns anything.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import subprocess
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from typing import Final, Protocol, runtime_checkable

from jarvis.core.constants import DEFAULT_ENCODING
from jarvis.core.exceptions import McpError

logger = logging.getLogger("jarvis.mcp.transport")

_CLOSE_TIMEOUT_SECONDS: Final[float] = 5.0
"""How long ``close`` waits for a terminated child before killing it."""


@runtime_checkable
class TimeoutAware(Protocol):
    """A transport whose per-request timeout can be changed after construction.

    The frozen :class:`~jarvis.mcp.types.McpTransport` interface carries no
    timeout argument, yet the handshake and a tool call want different budgets
    (a server may take seconds to boot but a call should be snappier, or the
    reverse). :class:`~jarvis.mcp.client.McpClient` therefore adjusts this
    attribute around each phase; a fake transport in tests omits it and is
    simply skipped.
    """

    timeout_seconds: float


def _encode(message: Mapping[str, object]) -> str:
    """Serialise one JSON-RPC message (no trailing newline)."""
    return json.dumps(message, ensure_ascii=False)


def _decode(line: str) -> Mapping[str, object] | None:
    """Parse one JSON-RPC line; ``None`` when it is blank or not an object.

    A malformed line is *not* fatal: servers occasionally log to stdout, and
    dropping the noise is friendlier than killing the connection over it.
    """
    stripped = line.strip()
    if not stripped:
        return None
    try:
        parsed: object = json.loads(stripped)
    except json.JSONDecodeError:
        logger.debug("ignoring non-JSON line from MCP server: %s", stripped[:200])
        return None
    if not isinstance(parsed, Mapping):
        return None
    return parsed


def _unwrap(message: Mapping[str, object], method: str, request_id: int) -> Mapping[str, object]:
    """Turn a JSON-RPC reply into its ``result`` object or raise :class:`McpError`."""
    error = message.get("error")
    if isinstance(error, Mapping):
        detail = error.get("message", "未知错误")
        raise McpError(
            f"MCP 请求 {method} 失败：{detail}",
            details={"method": method, "code": error.get("code"), "id": request_id},
        )
    result = message.get("result")
    if isinstance(result, Mapping):
        return result
    raise McpError(
        f"MCP 请求 {method} 返回了非法结果",
        details={"method": method, "id": request_id, "result_type": type(result).__name__},
    )


class _LineChannel:
    """Framing plus request/response correlation over a line-oriented channel.

    Split out from :class:`StdioTransport` so the interesting logic — assigning
    ids, skipping notifications and unrelated replies, honouring a deadline —
    can be tested with a list and a queue instead of a child process.
    """

    def __init__(
        self,
        write_line: Callable[[str], None],
        incoming: queue.Queue[str | None],
    ) -> None:
        self._write_line = write_line
        self._incoming = incoming
        self._write_lock = threading.Lock()
        self._request_lock = threading.Lock()
        self._next_id = 1

    def notify(self, method: str, params: Mapping[str, object]) -> None:
        """Send a one-way notification. Not serialised with pending requests."""
        self._send({"jsonrpc": "2.0", "method": method, "params": dict(params)})

    def request(
        self, method: str, params: Mapping[str, object], timeout_seconds: float
    ) -> Mapping[str, object]:
        """Send a request and block until its matching reply arrives.

        Requests are serialised: a second caller would otherwise consume the
        first caller's reply off the shared queue. MCP over stdio is a strictly
        turn-based conversation, so this costs nothing in practice.

        Raises:
            McpError: on timeout, a closed channel, or a JSON-RPC error reply.
        """
        with self._request_lock:
            request_id = self._next_id
            self._next_id += 1
            self._send(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "method": method,
                    "params": dict(params),
                }
            )
            deadline = time.monotonic() + timeout_seconds
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise self._timeout(method, timeout_seconds)
                try:
                    line = self._incoming.get(timeout=remaining)
                except queue.Empty as exc:
                    raise self._timeout(method, timeout_seconds) from exc
                if line is None:
                    raise McpError(
                        "MCP 服务器连接已关闭",
                        details={"method": method},
                    )
                message = _decode(line)
                if message is None or message.get("id") != request_id:
                    # A notification or another request's reply — keep waiting.
                    continue
                return _unwrap(message, method, request_id)

    def _send(self, message: Mapping[str, object]) -> None:
        with self._write_lock:
            self._write_line(_encode(message))

    @staticmethod
    def _timeout(method: str, timeout_seconds: float) -> McpError:
        return McpError(
            f"MCP 请求 {method} 超时",
            details={"method": method, "timeout_seconds": timeout_seconds},
        )


class StdioTransport:
    """Spawn a server process and speak newline-delimited JSON-RPC to it.

    The process is started lazily on the first request so constructing the
    transport is free and a disabled server costs nothing. A reader thread owns
    stdout (a blocking ``read`` on a pipe has no timeout on Windows) and pushes
    lines into a queue the requester drains with a deadline; stderr is drained
    separately so a chatty server cannot fill its pipe and deadlock.
    """

    def __init__(
        self,
        *,
        command: str,
        args: Sequence[str],
        env: Mapping[str, str],
        timeout_seconds: float,
    ) -> None:
        self._command = command
        self._args = tuple(args)
        self._env = dict(env)
        self.timeout_seconds = float(timeout_seconds)
        self._process: subprocess.Popen[str] | None = None
        self._incoming: queue.Queue[str | None] = queue.Queue()
        self._channel: _LineChannel | None = None
        self._lock = threading.Lock()

    def request(self, method: str, params: Mapping[str, object]) -> Mapping[str, object]:
        return self._channel_or_start().request(method, params, self.timeout_seconds)

    def notify(self, method: str, params: Mapping[str, object]) -> None:
        self._channel_or_start().notify(method, params)

    def close(self) -> None:
        """Terminate the child process; idempotent."""
        with self._lock:
            process = self._process
            self._process = None
            self._channel = None
        if process is None:
            return
        try:
            if process.stdin is not None:
                process.stdin.close()
        except OSError:  # pragma: no cover - already gone
            pass
        try:
            process.terminate()
            process.wait(timeout=_CLOSE_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            logger.warning("MCP 服务器进程未响应 terminate，强制结束：%s", self._command)
            process.kill()
        except OSError:  # pragma: no cover - process already reaped
            pass

    # -- internals ---------------------------------------------------------

    def _channel_or_start(self) -> _LineChannel:
        self._ensure_started()
        channel = self._channel
        if channel is None:  # pragma: no cover - defensive
            raise McpError("MCP 服务器进程未启动", details={"command": self._command})
        return channel

    def _ensure_started(self) -> None:
        with self._lock:
            if self._process is not None:
                return
            environ = {**os.environ, **self._env}
            try:
                process = subprocess.Popen(
                    [self._command, *self._args],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    encoding=DEFAULT_ENCODING,
                    env=environ,
                    bufsize=1,
                )
            except OSError as exc:
                raise McpError(
                    f"无法启动 MCP 服务器进程：{self._command}",
                    details={"command": self._command, "reason": str(exc)},
                ) from exc
            self._process = process
            self._channel = _LineChannel(self._write_line, self._incoming)
            self._spawn(self._read_stdout, process, "mcp-stdio-stdout")
            self._spawn(self._drain_stderr, process, "mcp-stdio-stderr")

    @staticmethod
    def _spawn(
        target: Callable[[subprocess.Popen[str]], None],
        process: subprocess.Popen[str],
        name: str,
    ) -> None:
        thread = threading.Thread(target=target, args=(process,), name=name, daemon=True)
        thread.start()

    def _write_line(self, line: str) -> None:
        process = self._process
        if process is None or process.stdin is None:
            raise McpError("MCP 服务器进程未启动", details={"command": self._command})
        try:
            process.stdin.write(line + "\n")
            process.stdin.flush()
        except (OSError, ValueError) as exc:
            raise McpError(
                "向 MCP 服务器写入失败",
                details={"command": self._command, "reason": str(exc)},
            ) from exc

    def _read_stdout(self, process: subprocess.Popen[str]) -> None:
        stream = process.stdout
        if stream is None:  # pragma: no cover - PIPE guarantees a stream
            self._incoming.put(None)
            return
        for raw_line in stream:
            stripped = raw_line.strip()
            if stripped:
                self._incoming.put(stripped)
        # EOF: unblock any waiting request with a definitive "closed" signal.
        self._incoming.put(None)

    def _drain_stderr(self, process: subprocess.Popen[str]) -> None:
        stream = process.stderr
        if stream is None:  # pragma: no cover - PIPE guarantees a stream
            return
        for raw_line in stream:
            text = raw_line.rstrip()
            if text:
                logger.debug("MCP server stderr: %s", text)


class HttpTransport:
    """POST JSON-RPC to a URL and read the reply (streamable HTTP transport).

    Stateless by design: JARVIS does not track ``Mcp-Session-Id`` because the
    servers it targets are the single-shot kind, and a missing header is a
    server-side rejection we surface as :class:`McpError` rather than guess at.
    """

    def __init__(self, *, url: str, timeout_seconds: float) -> None:
        self._url = url
        self.timeout_seconds = float(timeout_seconds)
        self._next_id = 1
        self._lock = threading.Lock()

    def request(self, method: str, params: Mapping[str, object]) -> Mapping[str, object]:
        with self._lock:
            request_id = self._next_id
            self._next_id += 1
        message = self._post(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": method,
                "params": dict(params),
            }
        )
        return _unwrap(message, method, request_id)

    def notify(self, method: str, params: Mapping[str, object]) -> None:
        self._post({"jsonrpc": "2.0", "method": method, "params": dict(params)}, expect_body=False)

    def close(self) -> None:
        """Nothing to release: ``urllib`` opens a fresh connection per request."""

    # -- internals ---------------------------------------------------------

    def _post(
        self, payload: Mapping[str, object], *, expect_body: bool = True
    ) -> Mapping[str, object]:
        data = json.dumps(payload, ensure_ascii=False).encode(DEFAULT_ENCODING)
        request = urllib.request.Request(
            self._url,
            data=data,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                raw = response.read().decode(DEFAULT_ENCODING, errors="replace")
        except urllib.error.HTTPError as exc:
            raise McpError(
                f"MCP HTTP 服务器返回错误状态 {exc.code}",
                details={"url": self._url, "status": exc.code},
            ) from exc
        except TimeoutError as exc:
            raise McpError("MCP HTTP 请求超时", details={"url": self._url}) from exc
        except urllib.error.URLError as exc:
            raise McpError(
                "MCP HTTP 连接失败",
                details={"url": self._url, "reason": str(exc.reason)},
            ) from exc
        except OSError as exc:  # pragma: no cover - rare socket-level failure
            raise McpError(
                "MCP HTTP 请求失败",
                details={"url": self._url, "reason": str(exc)},
            ) from exc
        if not expect_body:
            return {}
        return _parse_http_body(raw, self._url)


def _parse_http_body(raw: str, url: str) -> Mapping[str, object]:
    """Parse a JSON body, or the first ``data:`` frame of an SSE reply."""
    text = raw.strip()
    if not text:
        raise McpError("MCP HTTP 服务器返回了空响应", details={"url": url})
    if text.startswith("data:"):
        for line in text.splitlines():
            if line.startswith("data:"):
                text = line[len("data:") :].strip()
                break
    try:
        parsed: object = json.loads(text)
    except json.JSONDecodeError as exc:
        raise McpError("MCP HTTP 服务器返回了非 JSON 响应", details={"url": url}) from exc
    if not isinstance(parsed, Mapping):
        raise McpError("MCP HTTP 服务器返回了非对象 JSON", details={"url": url})
    return parsed


__all__ = [
    "HttpTransport",
    "StdioTransport",
    "TimeoutAware",
]
