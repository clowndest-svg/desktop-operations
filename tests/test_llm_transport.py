"""Tests for :class:`UrllibTransport`, the one piece that owns a real connection.

The stop button's whole promise is that an interrupted answer stops being fetched, and
that promise is kept or broken here rather than in the client: the client abandons an
iterator, and this module is what turns that into a closed socket. So these tests drive
the actual stdlib code path with ``urlopen`` swapped out -- a fake response that records
whether anything closed it, and bytes on the wire exactly as a server sends them.
"""

from __future__ import annotations

import io
import json
import urllib.error
import urllib.request
from collections.abc import Generator, Iterator
from email.message import Message

import pytest

from jarvis.llm.transport import (
    TransportNetworkError,
    TransportStatusError,
    TransportTimeoutError,
    UrllibTransport,
)


class FakeResponse:
    """A streamed HTTP response: line-based bytes, and a close anyone can observe.

    Iterable for SSE and a context manager for :meth:`post_json`, because the real
    ``addinfourl`` is both, and a fake that is only one of them cannot stand in for it.
    """

    def __init__(self, lines: list[bytes]) -> None:
        self.lines = lines
        self.closed = 0
        self.body = b'{"error": "nope"}'

    def __iter__(self) -> Iterator[bytes]:
        yield from self.lines

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def read(self) -> bytes:
        return self.body

    def close(self) -> None:
        self.closed += 1


class FlakyResponse(FakeResponse):
    """A connection that dies between two chunks, which is the interesting position."""

    def __iter__(self) -> Iterator[bytes]:
        yield b"data:one\n\n"
        raise ConnectionResetError("connection reset by peer")


class FakeUrlopen:
    """Stand-in for ``urllib.request.urlopen``: hands out scripted responses."""

    def __init__(self, *responses: object) -> None:
        self.responses = list(responses)
        self.requests: list[urllib.request.Request] = []
        self.timeouts: list[float] = []

    def __call__(self, request: urllib.request.Request, timeout: float = 0.0) -> object:
        self.requests.append(request)
        self.timeouts.append(timeout)
        assert self.responses, "more requests than this test scripted responses for"
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def sse_lines(*payloads: str) -> list[bytes]:
    """The lines a provider puts on the wire, including the fields we ignore."""
    lines: list[bytes] = []
    for payload in payloads:
        lines.append(b"id: 7\n")
        lines.append(f"data:{payload}\n".encode())
        lines.append(b"\n")
    return lines


def make_transport(monkeypatch: pytest.MonkeyPatch, *responses: object) -> FakeUrlopen:
    fake = FakeUrlopen(*responses)
    monkeypatch.setattr("urllib.request.urlopen", fake)
    return fake


def post_sse(timeout: float = 5.0) -> Generator[str, None, None]:
    return UrllibTransport().post_sse(
        "https://api.test.example/v1/chat/completions", headers={}, payload={}, timeout=timeout
    )


class TestPostSse:
    def test_only_the_data_payloads_come_through(self, monkeypatch: pytest.MonkeyPatch) -> None:
        make_transport(monkeypatch, FakeResponse(sse_lines('{"a": 1}', "[DONE]")))
        assert list(post_sse()) == ['{"a": 1}', "[DONE]"]

    def test_the_answer_stops_being_fetched_the_moment_the_caller_walks_away(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The assertion that makes 停止 mean "stop" rather than "ignore".

        Nothing in the client can close a socket; it can only stop asking for the next
        chunk. Whether the provider then keeps generating -- and billing -- a reply
        nobody reads is decided by whether this iterator drops the connection.
        """
        response = FakeResponse(sse_lines("a", "b", "c"))
        make_transport(monkeypatch, response)
        stream = post_sse()
        assert next(stream) == "a"
        assert response.closed == 0
        stream.close()
        assert response.closed == 1

    def test_a_stream_read_to_the_end_is_closed_too(self, monkeypatch: pytest.MonkeyPatch) -> None:
        response = FakeResponse(sse_lines("a"))
        make_transport(monkeypatch, response)
        assert list(post_sse()) == ["a"]
        assert response.closed == 1

    def test_a_connection_that_drops_halfway_reports_a_transport_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        response = FlakyResponse([])
        make_transport(monkeypatch, response)
        stream = post_sse()
        assert next(stream) == "one"
        with pytest.raises(TransportNetworkError):
            next(stream)
        assert response.closed == 1

    def test_a_stalled_stream_reports_a_timeout_not_a_hang(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = make_transport(monkeypatch, TimeoutError("read timed out"))
        with pytest.raises(TransportTimeoutError):
            next(
                UrllibTransport().post_sse(
                    "https://api.test.example/v1/chat/completions",
                    headers={"Authorization": "Bearer k"},
                    payload={},
                    timeout=12.5,
                )
            )
        assert fake.timeouts == [12.5]

    def test_a_refused_request_is_reported_with_its_body(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        error = urllib.error.HTTPError(
            "https://api.test.example/v1/chat/completions",
            400,
            "Bad Request",
            Message(),
            io.BytesIO(b'{"error":"streaming not supported"}'),
        )
        make_transport(monkeypatch, error)
        with pytest.raises(TransportStatusError) as excinfo:
            next(post_sse())
        assert excinfo.value.status == 400
        assert excinfo.value.body == '{"error":"streaming not supported"}'

    def test_the_request_carries_a_json_body_and_the_streaming_accept_header(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = make_transport(monkeypatch, FakeResponse(sse_lines("[DONE]")))
        list(
            UrllibTransport().post_sse(
                "https://api.test.example/v1/chat/completions",
                headers={"Authorization": "Bearer k"},
                payload={"model": "m", "prompt": "你好"},
                timeout=5,
            )
        )
        request = fake.requests[0]
        body: object = request.data
        assert isinstance(body, bytes)
        assert json.loads(body.decode("utf-8")) == {"model": "m", "prompt": "你好"}
        headers = {name.lower(): value for name, value in request.header_items()}
        assert headers["authorization"] == "Bearer k"
        assert headers["accept"] == "text/event-stream"
        assert headers["content-type"] == "application/json"
        assert request.method == "POST"


class TestPostJson:
    def test_a_body_is_read_decoded_and_the_connection_released(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        response = FakeResponse([])
        response.body = '{"ok": true, "中文": "是"}'.encode()
        make_transport(monkeypatch, response)
        parsed = UrllibTransport().post_json(
            "https://api.test.example/v1/chat/completions",
            headers={},
            payload={"a": 1},
            timeout=5,
        )
        assert parsed == {"ok": True, "中文": "是"}
        assert response.closed == 1

    def test_a_non_object_body_is_a_status_error_not_a_crash(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        response = FakeResponse([])
        response.body = b"[1, 2, 3]"
        make_transport(monkeypatch, response)
        with pytest.raises(TransportStatusError) as excinfo:
            UrllibTransport().post_json(
                "https://api.test.example/v1/chat/completions",
                headers={},
                payload={},
                timeout=5,
            )
        assert excinfo.value.status == 200
