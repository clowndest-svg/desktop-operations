"""Bounded, complete binary reads shared by image and video downloads."""

from __future__ import annotations

import http.client
from collections.abc import Callable
from typing import Any

IMAGE_MAX_BYTES = 40 * 1024 * 1024
VIDEO_MAX_BYTES = 400 * 1024 * 1024
_READ_CHUNK = 512 * 1024


def read_media(
    response: Any,
    *,
    limit: int,
    should_stop: Callable[[], bool] | None = None,
) -> bytes:
    """Reject overflow and truncated responses instead of returning partial media."""
    headers = getattr(response, "headers", None)
    raw_length = headers.get("Content-Length") if headers is not None else None
    expected: int | None = None
    if raw_length is not None:
        try:
            expected = int(raw_length)
        except (TypeError, ValueError) as exc:
            raise ValueError("媒体响应的 Content-Length 无效") from exc
        if expected < 0:
            raise ValueError("媒体响应的 Content-Length 无效")
        if expected > limit:
            raise ValueError(f"媒体文件超过{limit // (1024 * 1024)}MB限制，未保存残缺文件")
    chunks: list[bytes] = []
    total = 0
    try:
        while True:
            if should_stop is not None and should_stop():
                raise ValueError("已停止下载媒体文件")
            chunk = bytes(response.read(min(_READ_CHUNK, limit + 1 - total)))
            if not chunk:
                break
            total += len(chunk)
            if total > limit:
                raise ValueError(f"媒体文件超过{limit // (1024 * 1024)}MB限制，未保存残缺文件")
            chunks.append(chunk)
    except http.client.IncompleteRead as exc:
        raise ValueError("媒体下载不完整，未保存残缺文件") from exc
    if expected is not None and total != expected:
        raise ValueError(f"媒体下载不完整：应有 {expected} 字节，实际 {total} 字节，未保存残缺文件")
    if not total:
        raise ValueError("媒体地址回了 0 字节")
    return b"".join(chunks)
