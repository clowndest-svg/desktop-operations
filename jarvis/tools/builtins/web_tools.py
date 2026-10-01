"""Network tools.

One tool, and it is deliberately narrow: a GET, http/https only, with a byte cap
and a timeout. No POST, no headers, no auth — a tool that can send arbitrary
bodies to arbitrary endpoints is an exfiltration primitive wearing a raincoat,
and nothing JARVIS needs to answer a question requires it.

The URL is checked *after* redirects too. ``urllib`` follows them silently, so a
harmless-looking ``http://localhost`` that 302s to ``file:///`` would otherwise
read the local disk through a tool that claims to fetch web pages.
"""

from __future__ import annotations

import ipaddress
import logging
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from typing import Final

from jarvis.core.constants import DEFAULT_ENCODING
from jarvis.core.exceptions import ToolExecutionError
from jarvis.tools.types import (
    PERMISSION_NETWORK,
    RiskLevel,
    ToolHandler,
    ToolSpec,
    integer_property,
    object_schema,
    string_property,
)

logger = logging.getLogger("jarvis.tools.builtins.web_tools")

ALLOWED_SCHEMES: Final[frozenset[str]] = frozenset({"http", "https"})
"""Only these. ``file://`` and ``ftp://`` are how a fetch tool becomes a
filesystem reader."""

MAX_BYTES: Final[int] = 512 * 1024
"""Hard cap on downloaded bytes.

A structural bound, not a tunable: the result goes into a prompt, and half a
megabyte of HTML is already far past what any model should be asked to read.
"""

DEFAULT_TIMEOUT_SECONDS: Final[float] = 15.0
"""A request that has not answered in fifteen seconds is not going to help the
conversation that is waiting on it."""

_FETCH_URL = ToolSpec(
    name="fetch_url",
    description=(
        "抓取一个网页或接口的文本内容（仅支持 http/https 的 GET 请求）。"
        "需要了解某个网址上的内容时使用。"
    ),
    parameters=object_schema(
        {
            "url": string_property("完整网址，例如 https://example.com/page"),
            "max_chars": integer_property("最多返回多少个字符", default=4000, minimum=1),
        },
        required=["url"],
    ),
    risk=RiskLevel.CAUTION,
    permissions=frozenset({PERMISSION_NETWORK}),
)


def validate_url(raw: str) -> str:
    """Normalise and vet a URL.

    Raises:
        ToolExecutionError: on a missing scheme, a non-http(s) scheme, or a
            private/loopback address (a "fetch this page" tool must not become
            a port scanner for the user's own network).
    """
    text = raw.strip()
    if not text:
        raise ToolExecutionError("网址不能为空")
    parsed = urllib.parse.urlsplit(text)
    if not parsed.scheme:
        raise ToolExecutionError(f"网址缺少协议头：{raw}（需要 http:// 或 https://）")
    if parsed.scheme.lower() not in ALLOWED_SCHEMES:
        raise ToolExecutionError(f"不支持的协议：{parsed.scheme}（只允许 http/https）")
    if not parsed.hostname:
        raise ToolExecutionError(f"网址缺少主机名：{raw}")
    _reject_private_host(parsed.hostname)
    return urllib.parse.urlunsplit(parsed)


def _reject_private_host(host: str) -> None:
    """Refuse loopback / link-local / private-range hosts and bare localhost."""
    lowered = host.lower()
    if lowered in {"localhost", "localhost.localdomain"} or lowered.endswith(".local"):
        raise ToolExecutionError(f"拒绝访问本机地址：{host}")
    try:
        address = ipaddress.ip_address(lowered)
    except ValueError:
        return  # a hostname; DNS resolution is the network's problem, not ours
    if address.is_private or address.is_loopback or address.is_link_local:
        raise ToolExecutionError(f"拒绝访问内网地址：{host}")


def _fetch_url(
    *, timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS, opener: object | None = None
) -> ToolHandler:
    def handler(arguments: Mapping[str, object]) -> str:
        raw = arguments.get("url")
        if not isinstance(raw, str):
            raise ToolExecutionError("缺少网址")
        url = validate_url(raw)
        raw_max = arguments.get("max_chars")
        limit = raw_max if isinstance(raw_max, int) and raw_max > 0 else 4000
        request = urllib.request.Request(
            url,
            headers={"User-Agent": "JARVIS/0.1 (+desktop assistant)"},
            method="GET",
        )
        try:
            if opener is not None:
                response = opener.open(request, timeout=timeout_seconds)  # type: ignore[attr-defined]
            else:
                response = urllib.request.urlopen(request, timeout=timeout_seconds)
            with response:
                final_url = response.geturl()
                validate_url(final_url)
                body = response.read(MAX_BYTES + 1)
                content_type = response.headers.get_content_type()
        except ToolExecutionError:
            raise
        except urllib.error.HTTPError as exc:
            raise ToolExecutionError(f"请求返回 HTTP {exc.code}：{url}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise ToolExecutionError(f"请求失败：{exc}") from exc

        text = body.decode(DEFAULT_ENCODING, errors="replace")
        truncated = len(body) > MAX_BYTES
        if truncated:
            text = text[:MAX_BYTES]
        logger.debug("fetched %s (%d bytes, %s)", final_url, len(body), content_type)
        header = f"{final_url}（{content_type}）"
        if truncated:
            header += f"\n（内容超过 {MAX_BYTES} 字节，已截断）"
        return f"{header}\n{text[:limit]}"

    return handler


def build(
    *, timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS, opener: object | None = None
) -> list[tuple[ToolSpec, ToolHandler]]:
    """Return the tools this module contributes.

    Args:
        timeout_seconds: Per-request timeout.
        opener: Optional ``urllib`` opener override; tests inject one so the
            suite never touches the network.
    """
    return [(_FETCH_URL, _fetch_url(timeout_seconds=timeout_seconds, opener=opener))]


__all__ = ["ALLOWED_SCHEMES", "MAX_BYTES", "build", "validate_url"]
