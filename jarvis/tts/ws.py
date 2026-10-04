"""A minimal WebSocket client, written here because the project does not add
dependencies for one endpoint.

Scope is exactly what the realtime speech API needs: a text client, no
extensions, no compression, no proxy negotiation. RFC 6455 §4--§7, which is
about 150 lines of framing, against a dependency that would arrive with its own
event loop and its own opinions about who owns the socket.

Two details that are easy to get wrong and silent when wrong:

* **Client frames must be masked** (RFC 6455 §5.3). A server is required to fail
  the connection on an unmasked frame, so this is not a nicety -- and because the
  failure arrives as a bare close, an unmasked implementation looks like "the
  service dropped me".
* **A message may be split across frames.** ``response.audio.delta`` payloads are
  routinely larger than one frame, so a reader that returns one frame per call
  hands the caller broken JSON. :meth:`WebSocket.recv` reassembles continuation
  frames and only then returns the whole message.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import logging
import os
import socket
import ssl
import struct
from typing import TYPE_CHECKING, Final
from urllib.parse import urlsplit

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Iterable

logger = logging.getLogger("jarvis.tts.ws")

WS_GUID: Final[bytes] = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
"""The constant from RFC 6455 §1.3. Servers echo a hash of it to prove they read
the handshake rather than replying 101 to anything."""

_OPCODE_CONTINUATION: Final[int] = 0x0
_OPCODE_TEXT: Final[int] = 0x1
_OPCODE_BINARY: Final[int] = 0x2
_OPCODE_CLOSE: Final[int] = 0x8
_OPCODE_PING: Final[int] = 0x9
_OPCODE_PONG: Final[int] = 0xA

MAX_MESSAGE_BYTES: Final[int] = 16 * 1024 * 1024
"""Ceiling on a reassembled message. A hostile or broken peer that never sends a
final frame would otherwise grow this buffer until the process dies."""


class WebSocketError(Exception):
    """A socket-level failure with a message worth reading."""


def mask_key() -> bytes:
    """Four random bytes. Cryptographically random, not ``random``: a predictable
    mask is a cache-poisoning footgun in the original sense of the RFC."""
    return os.urandom(4)


def build_frame(payload: bytes, opcode: int = _OPCODE_TEXT, *, fin: bool = True) -> bytes:
    """One client-to-server frame, always masked.

    Also used for pong replies, which is why ``opcode`` is a parameter rather
    than fixed: a client that answers a ping with a text frame is disconnected.
    """
    header = bytearray()
    header.append((0x80 if fin else 0x00) | opcode)
    length = len(payload)
    if length < 126:
        header.append(0x80 | length)
    elif length < 0x10000:
        header.append(0x80 | 126)
        header.extend(struct.pack("!H", length))
    else:
        header.append(0x80 | 127)
        header.extend(struct.pack("!Q", length))
    key = mask_key()
    header.extend(key)
    masked = bytes(byte ^ key[index % 4] for index, byte in enumerate(payload))
    return bytes(header) + masked


def accept_key(nonce: str) -> str:
    """The value a compliant server must send back for ``nonce``."""
    digest = hashlib.sha1(nonce.encode("ascii") + WS_GUID).digest()
    return base64.b64encode(digest).decode("ascii")


def read_exactly(sock: socket.socket, count: int) -> bytes:
    """Read ``count`` bytes or fail. ``recv`` is allowed to return short reads."""
    chunks: list[bytes] = []
    remaining = count
    while remaining > 0:
        chunk = sock.recv(remaining)
        if not chunk:
            raise WebSocketError("连接被对方关闭了")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def read_frame(sock: socket.socket) -> tuple[int, bool, bytes]:
    """One frame: ``(opcode, fin, payload)``, unmasked.

    Server frames are never masked (RFC 6455 §5.1), so the mask bit is not even
    read -- but it is *checked*, because a peer that sets it would otherwise
    have its payload interpreted as plain data and produce plausible-looking
    garbage.
    """
    first, second = read_exactly(sock, 2)
    fin = bool(first & 0x80)
    opcode = first & 0x0F
    masked = bool(second & 0x80)
    length = second & 0x7F
    if length == 126:
        (length,) = struct.unpack("!H", read_exactly(sock, 2))
    elif length == 127:
        (length,) = struct.unpack("!Q", read_exactly(sock, 8))
    if length > MAX_MESSAGE_BYTES:
        raise WebSocketError(f"对方发来了一个 {length} 字节的帧，超过上限")
    key = read_exactly(sock, 4) if masked else b""
    payload = read_exactly(sock, length) if length else b""
    if masked:
        payload = bytes(byte ^ key[index % 4] for index, byte in enumerate(payload))
    return opcode, fin, payload


class WebSocket:
    """A connected text WebSocket. Construct with :meth:`connect`."""

    def __init__(self, sock: socket.socket) -> None:
        self._sock = sock

    # -- lifecycle ---------------------------------------------------------

    @classmethod
    def connect(
        cls,
        url: str,
        *,
        headers: Iterable[tuple[str, str]] = (),
        timeout: float = 30.0,
        context: ssl.SSLContext | None = None,
    ) -> WebSocket:
        """Open a ``ws://`` or ``wss://`` connection and complete the handshake.

        Raises:
            WebSocketError: On a refused handshake, with the status line, because
                "401" and "404" mean very different things to whoever has to fix
                it and the caller cannot tell them apart otherwise.
        """
        parts = urlsplit(url)
        if parts.scheme not in {"ws", "wss"}:
            raise WebSocketError(f"不支持这个协议：{parts.scheme}")
        secure = parts.scheme == "wss"
        host = parts.hostname or ""
        port = parts.port or (443 if secure else 80)
        path = parts.path or "/"
        if parts.query:
            path = f"{path}?{parts.query}"

        raw = socket.create_connection((host, port), timeout=timeout)
        if secure:
            raw = (context or ssl.create_default_context()).wrap_socket(raw, server_hostname=host)
        raw.settimeout(timeout)

        nonce = base64.b64encode(os.urandom(16)).decode("ascii")
        lines = [
            f"GET {path} HTTP/1.1",
            f"Host: {host}:{port}",
            "Upgrade: websocket",
            "Connection: Upgrade",
            f"Sec-WebSocket-Key: {nonce}",
            "Sec-WebSocket-Version: 13",
        ]
        lines.extend(f"{name}: {value}" for name, value in headers)
        raw.sendall(("\r\n".join(lines) + "\r\n\r\n").encode("ascii"))

        try:
            status, response_headers = _read_handshake(raw)
        except WebSocketError:
            raw.close()
            raise
        if status != 101:
            raw.close()
            raise WebSocketError(f"WebSocket 握手被拒绝（HTTP {status}）")
        # The accept hash proves the peer understood the handshake. Skipping it
        # would accept a plain HTTP 101 from a proxy that knows nothing about
        # WebSockets, and every later frame would be nonsense.
        if response_headers.get("sec-websocket-accept", "") != accept_key(nonce):
            raw.close()
            raise WebSocketError("WebSocket 握手回执不对（对方不是真的 WebSocket 服务）")
        return cls(raw)

    # -- messaging ---------------------------------------------------------

    def set_timeout(self, seconds: float | None) -> None:
        """Change the socket timeout after the handshake.

        Needed because the two phases want opposite things: the handshake must
        fail fast (a black-holed connect should not hold a phone call for the
        default OS timeout), while the stream that follows must be allowed to
        wait for a while -- the service sends nothing between an utterance's
        last audio delta and ``response.done``, and a timeout there would
        truncate a perfectly healthy reply. ``None`` means block.
        """
        self._sock.settimeout(seconds)

    def send(self, message: str) -> None:
        self._sock.sendall(build_frame(message.encode("utf-8"), _OPCODE_TEXT))

    def recv(self) -> str | None:
        """The next complete text message, or ``None`` when the peer closed.

        Reassembles continuation frames and answers pings, both of which happen
        in practice: a fragmented audio delta read as one frame is invalid JSON,
        and a ping left unanswered is a disconnection after the peer's timeout.
        """
        parts: list[bytes] = []
        total = 0
        while True:
            opcode, fin, payload = read_frame(self._sock)
            if opcode == _OPCODE_CLOSE:
                return None
            if opcode == _OPCODE_PING:
                self._sock.sendall(build_frame(payload, _OPCODE_PONG))
                continue
            if opcode == _OPCODE_PONG:
                continue
            if opcode in (_OPCODE_TEXT, _OPCODE_BINARY):
                parts = [payload]
                total = len(payload)
            elif opcode == _OPCODE_CONTINUATION:
                parts.append(payload)
                total += len(payload)
                if total > MAX_MESSAGE_BYTES:
                    raise WebSocketError("对方发来的消息超过上限")
            else:  # pragma: no cover - reserved opcodes, not used by this service
                logger.debug("ignoring frame with opcode %#x", opcode)
                continue
            if fin:
                return b"".join(parts).decode("utf-8", errors="replace")

    def close(self) -> None:
        """Send a close frame and drop the socket. Idempotent."""
        # Both steps tolerate the socket already being gone, which is the state
        # a double close arrives in: a failed send tells us exactly what the
        # explicit close below would raise, and there is nothing to do about
        # either of them.
        with contextlib.suppress(OSError):
            self._sock.sendall(build_frame(b"\x03\xe8", _OPCODE_CLOSE))
        with contextlib.suppress(OSError):
            self._sock.close()

    @property
    def closed(self) -> bool:
        return self._sock.fileno() < 0


def _read_handshake(sock: socket.socket) -> tuple[int, dict[str, str]]:
    """Read the response head. Byte-wise, so no body is consumed by accident."""
    buffer = bytearray()
    while b"\r\n\r\n" not in buffer:
        chunk = sock.recv(1)
        if not chunk:
            raise WebSocketError("握手过程中连接就断了")
        buffer.extend(chunk)
        if len(buffer) > 64 * 1024:  # pragma: no cover - a hostile peer
            raise WebSocketError("握手回执大得不像话")
    head = buffer.decode("latin-1")
    lines = head.split("\r\n")
    status_parts = lines[0].split(" ", 2)
    try:
        status = int(status_parts[1])
    except (IndexError, ValueError) as exc:
        raise WebSocketError(f"看不懂的握手回执：{lines[0]!r}") from exc
    headers: dict[str, str] = {}
    for line in lines[1:]:
        name, _, value = line.partition(":")
        if name:
            headers[name.strip().lower()] = value.strip()
    return status, headers


__all__ = [
    "MAX_MESSAGE_BYTES",
    "WebSocket",
    "WebSocketError",
    "accept_key",
    "build_frame",
    "read_frame",
]
