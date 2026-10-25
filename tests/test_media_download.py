"""A truncated image/video must fail before a partial file reaches the user."""

from __future__ import annotations

import http.client

import pytest

from jarvis.app.media_download import read_media


class _Resp:
    def __init__(self, chunks: list[bytes], length: str | None = None) -> None:
        self._chunks = list(chunks)
        self.headers = {} if length is None else {"Content-Length": length}
        self.read_bytes = 0

    def read(self, size: int) -> bytes:
        self.read_bytes += size
        if not self._chunks:
            return b""
        return self._chunks.pop(0)[:size]


def test_reads_a_complete_body() -> None:
    reply = _Resp([b"abc", b"def"], "6")
    assert read_media(reply, limit=10) == b"abcdef"


def test_rejects_oversized_declared_body_before_reading() -> None:
    reply = _Resp([b"x" * 20], "20")
    with pytest.raises(ValueError, match="超过"):
        read_media(reply, limit=10)
    assert reply.read_bytes == 0


def test_rejects_truncated_declared_body() -> None:
    reply = _Resp([b"abc"], "10")
    with pytest.raises(ValueError, match="不完整"):
        read_media(reply, limit=20)


def test_rejects_body_that_grows_past_the_limit() -> None:
    reply = _Resp([b"x" * 8, b"y" * 8])
    with pytest.raises(ValueError, match="超过"):
        read_media(reply, limit=10)


def test_rejects_incomplete_read_exception() -> None:
    class Broken(_Resp):
        def read(self, size: int) -> bytes:
            raise http.client.IncompleteRead(b"abc", 10)

    with pytest.raises(ValueError, match="不完整"):
        read_media(Broken([]), limit=20)


def test_stop_callback_aborts_without_returning_partial_bytes() -> None:
    reply = _Resp([b"abc", b"def"])
    with pytest.raises(ValueError, match="已停止"):
        read_media(reply, limit=20, should_stop=lambda: True)


def test_zero_byte_body_is_an_error() -> None:
    with pytest.raises(ValueError, match="0 字节"):
        read_media(_Resp([]), limit=20)
