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
import logging
import urllib.error
import urllib.request
from collections.abc import Generator, Iterator
from email.message import Message
from typing import Any

import pytest

from jarvis.core.constants import USER_AGENT
from jarvis.llm import transport
from jarvis.llm.transport import (
    TransportNetworkError,
    TransportStatusError,
    TransportTimeoutError,
    UrllibTransport,
    is_local_address,
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
        # Without this the stdlib announces ``Python-urllib``, and a Cloudflare-fronted
        # gateway answers that with 403 "error code: 1010" -- which reaches the operator
        # as 「连不上」 about an endpoint that is perfectly healthy.
        assert headers["user-agent"] == USER_AGENT
        assert request.method == "POST"

    def test_a_caller_that_names_itself_is_not_overridden(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The default is a floor, not a mask.

        A caller that sets its own ``User-Agent`` -- some gateways route on it, and a
        support ticket needs to be traceable -- must get what it asked for rather than
        whatever this library prefers to be called.
        """
        fake = make_transport(monkeypatch, FakeResponse(sse_lines("[DONE]")))
        list(
            UrllibTransport().post_sse(
                "https://api.test.example/v1/chat/completions",
                headers={"Authorization": "Bearer k", "User-Agent": "partner-agent/2"},
                payload={"model": "m"},
                timeout=5,
            )
        )
        sent = {name.lower(): value for name, value in fake.requests[0].header_items()}
        assert sent["user-agent"] == "partner-agent/2"


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


class TestLocalEndpointsSkipTheSystemProxy:
    """设了 ``HTTP_PROXY`` 的机器上，"她连不上我的 Ollama"通常是代理的锅。

    实测：环境里有 ``HTTP_PROXY`` 时，stdlib 会把 ``http://127.0.0.1:55555`` 也交给代理，
    代理回 502 —— 于是界面说「厂商自己返回 5xx」，而"厂商"就是这台机器自己。
    """

    @pytest.mark.parametrize(
        ("url", "local"),
        [
            ("http://127.0.0.1:11434/v1/chat/completions", True),
            ("http://localhost:1234/v1", True),
            ("http://[::1]:8080/v1", True),
            ("http://127.5.5.5:8000/v1", True),
            ("https://api.deepseek.com/v1/chat/completions", False),
            ("http://192.168.1.20:11434/v1", False),
            ("http://llama-box.internal:8080/v1", False),
        ],
    )
    def test_only_this_machine_counts_as_local(self, url: str, local: bool) -> None:
        assert is_local_address(url) is local

    def test_a_loopback_request_is_opened_with_no_proxy_at_all(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: dict[str, object] = {}

        def fake_build_opener(*handlers: object) -> Any:
            seen["handlers"] = handlers

            class _Opener:
                def open(self, request, timeout=None):  # type: ignore[no-untyped-def]
                    seen["opened"] = request.full_url
                    return FakeResponse([b"data:[DONE]\n"])

            return _Opener()

        def never(*args: object, **kwargs: object) -> None:
            raise AssertionError("本机的地址不该走 urlopen（那才会用系统代理）")

        monkeypatch.setattr("urllib.request.build_opener", fake_build_opener)
        monkeypatch.setattr("urllib.request.urlopen", never)
        monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:7897")

        drain("http://127.0.0.1:11434/v1/chat/completions")

        handlers = seen.get("handlers")
        assert isinstance(handlers, tuple) and handlers, "没换 opener，代理还是照走"
        proxy_handler = handlers[0]
        assert isinstance(proxy_handler, urllib.request.ProxyHandler)
        assert vars(proxy_handler)["proxies"] == {}
        assert seen.get("opened") == "http://127.0.0.1:11434/v1/chat/completions"

    def test_a_remote_endpoint_still_uses_the_system_proxy(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """云端那几家常常**就是**要靠代理才出得去，这条不能顺手一起改掉。"""
        called: list[str] = []

        def open_remote(*args: Any, **kwargs: Any) -> FakeResponse:
            called.append("urlopen")
            return FakeResponse([b"data:[DONE]\n"])

        monkeypatch.setattr("urllib.request.urlopen", open_remote)
        monkeypatch.setattr(
            "urllib.request.build_opener", lambda *a: pytest.fail("远端地址不该另建 opener")
        )
        monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:7897")

        drain("https://api.test.example/v1/chat/completions")

        assert called == ["urlopen"]

    def test_skipping_the_proxy_says_so_once(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.INFO, logger="jarvis.llm.transport")
        monkeypatch.setattr(transport, "_notice_given", False)
        monkeypatch.setattr("urllib.request.build_opener", lambda *a: _QuietOpener())
        monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:7897")

        for _ in range(3):
            drain("http://localhost:1234/v1/chat/completions")

        notes = [r for r in caplog.records if "不走系统代理" in r.getMessage()]
        assert len(notes) == 1, "每次请求都念一遍是噪音，一次都不念是静默改行为"

    def test_no_notice_when_there_is_no_proxy_to_skip(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.INFO, logger="jarvis.llm.transport")
        monkeypatch.setattr(transport, "_notice_given", False)
        monkeypatch.setattr("urllib.request.build_opener", lambda *a: _QuietOpener())
        for name in ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy"):
            monkeypatch.delenv(name, raising=False)

        drain("http://127.0.0.1:11434/v1/chat/completions")

        assert not [r for r in caplog.records if "不走系统代理" in r.getMessage()]


class _QuietOpener:
    """An opener stand-in that answers like a server with nothing to say."""

    def open(self, request, timeout=None):  # type: ignore[no-untyped-def]
        return FakeResponse([b"data:[DONE]\n"])


def drain(url: str) -> None:
    """Actually run one ``post_sse``: it is a generator, so nothing happens until read."""

    list(UrllibTransport().post_sse(url, headers={}, payload={}, timeout=5.0))
