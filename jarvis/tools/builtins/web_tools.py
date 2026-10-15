"""Network tools.

Two tools, both deliberately narrow: a GET, http/https only, with a byte cap and a
timeout. No POST, no headers, no auth — a tool that can send arbitrary bodies to
arbitrary endpoints is an exfiltration primitive wearing a raincoat, and nothing
JARVIS needs to answer a question requires it.

The URL is checked *after* redirects too. ``urllib`` follows them silently, so a
harmless-looking ``http://localhost`` that 302s to ``file:///`` would otherwise
read the local disk through a tool that claims to fetch web pages.

``search_web`` takes a query, not a URL. That is the whole security argument: the
host is a constant in this file, and the model's text is percent-encoded into the
query string, so it cannot aim the request anywhere.
"""

from __future__ import annotations

import html as htmlmod
import ipaddress
import logging
import re
import socket
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections.abc import Mapping
from typing import Any, Final

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

USER_AGENT: Final[str] = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
"""A browser-shaped UA. Not an evasion attempt: the public HTML search endpoints
answer a ``Python-urllib`` agent with a consent wall or nothing at all, so a
generic agent would make the tool look broken rather than look unpolished."""

SEARCH_ENDPOINT: Final[str] = "https://www.bing.com/search"
"""The only host ``search_web`` can ever contact.

Bing's HTML endpoint, because it needs no key, no account and no quota, and it
answers from this machine's own network. A paid search API would be a better
product and a worse default: the buyer who has not configured a key gets an error
instead of an answer, and the tool would then be a visible capability that only
ever fails.
"""

MAX_RESULTS: Final[int] = 5
"""Five rows is what fits the point. More is a wall of text a 4K model cannot read."""

MAX_RESULT_CHARS: Final[int] = 4000
"""Total characters of search output handed to the model."""

_FETCH_URL = ToolSpec(
    name="fetch_url",
    description=(
        "抓取一个网页或接口的文本内容（仅支持 http/https 的 GET 请求）。"
        "需要了解某个网址上的内容时使用。网页会被转成纯文本再返回，"
        "所以拿到的是正文而不是 HTML 标签。"
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

_SEARCH_WEB = ToolSpec(
    name="search_web",
    description=(
        "联网搜索一个关键词，返回最多 5 条结果的标题、出处网址和摘要。"
        "用户问「最新的」「网上怎么说」「今天有什么」这类本机资料里没有、"
        "又没给出网址的问题时用这个。找到感兴趣的再用 fetch_url 打开那一条。"
        "只搜公开网页，不查任何需要登录的内容。"
    ),
    parameters=object_schema(
        {
            "query": string_property("搜索词，尽量具体，例如「Qwen3 4B 上下文长度」"),
            "max_results": integer_property(
                f"最多返回几条结果（1-{MAX_RESULTS}）", default=MAX_RESULTS, minimum=1
            ),
        },
        required=["query"],
    ),
    risk=RiskLevel.CAUTION,
    permissions=frozenset({PERMISSION_NETWORK}),
)


_SCRIPTISH = re.compile(r"<(script|style|noscript|template)\b.*?</\1>", re.IGNORECASE | re.DOTALL)
_TAG = re.compile(r"<[^>]{0,4000}>")
_BLOCK = re.compile(
    r"</?(p|div|br|li|ul|ol|h[1-6]|tr|table|section|article|header|footer|blockquote)\b[^>]*>",
    re.IGNORECASE,
)
_WS = re.compile(r"[ \t\f\v]+")
_BLANK = re.compile(r"\n{3,}")


def html_to_text(raw: str) -> str:
    """Roughly what a person would read, from a page's markup.

    Not a browser and not trying to be. The reason this exists is measured: a
    result handed straight to the model was 4000 characters of ``<div class=...>``,
    which on a 4K local window means the actual article never reached the prompt
    at all. Tags cost tokens and carry no answer.
    """
    text = _SCRIPTISH.sub(" ", raw)
    text = _BLOCK.sub("\n", text)
    text = _TAG.sub(" ", text)
    text = htmlmod.unescape(text)
    text = _WS.sub(" ", text)
    lines = [line.strip() for line in text.splitlines()]
    return _BLANK.sub("\n\n", "\n".join(line for line in lines if line)).strip()


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
    if parsed.username or parsed.password:
        raise ToolExecutionError("不支持包含账号密码的网址")
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
    if not address.is_global:
        raise ToolExecutionError(f"拒绝访问内网地址：{host}")


def _check_public_dns(url: str) -> None:
    parsed = urllib.parse.urlsplit(validate_url(url))
    assert parsed.hostname is not None
    for address in socket.getaddrinfo(
        parsed.hostname,
        parsed.port or (443 if parsed.scheme == "https" else 80),
        type=socket.SOCK_STREAM,
    ):
        if not ipaddress.ip_address(address[4][0]).is_global:
            raise ToolExecutionError("网页域名解析到非公网地址，已停止访问")


class _PublicRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str
    ) -> Any:
        _check_public_dns(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _get(url: str, *, timeout_seconds: float, opener: object | None) -> tuple[str, bytes, str]:
    """One GET, vetted before and after redirects.

    Returns ``(final_url, body, content_type)``. Failures carry the real reason: a
    network tool that answers 「搜索失败」 with nothing further is indistinguishable
    from one that found nothing, and the model keeps trying the same dead path.
    """
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9",
        },
        method="GET",
    )
    try:
        if opener is not None:
            response = opener.open(request, timeout=timeout_seconds)  # type: ignore[attr-defined]
        else:
            _check_public_dns(url)
            response = urllib.request.build_opener(_PublicRedirect()).open(
                request, timeout=timeout_seconds
            )
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
        raise ToolExecutionError(f"网络请求失败：{getattr(exc, 'reason', exc)}（{url}）") from exc
    return final_url, body, content_type


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
        final_url, body, content_type = _get(url, timeout_seconds=timeout_seconds, opener=opener)
        truncated = len(body) > MAX_BYTES
        text = body.decode(DEFAULT_ENCODING, errors="replace")
        if content_type in {"text/html", "application/xhtml+xml"}:
            text = html_to_text(text)
        logger.debug("fetched %s (%d bytes, %s)", final_url, len(body), content_type)
        header = f"{final_url}（{content_type}）"
        if truncated:
            header += f"\n（内容超过 {MAX_BYTES} 字节，已截断）"
        if not text.strip():
            return f"{header}\n（这个页面没有可读正文，可能是一段脚本或一个空响应）"
        return f"{header}\n{text[:limit]}"

    return handler


_ALGO = re.compile(r'<li class="b_algo".*?</li>', re.IGNORECASE | re.DOTALL)
_HEAD = re.compile(r'<h2[^>]*>\s*<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', re.IGNORECASE | re.DOTALL)
_CITE = re.compile(r"<cite[^>]*>(.*?)</cite>", re.IGNORECASE | re.DOTALL)
_SNIPPET = re.compile(r'<p[^>]*class="[^"]*b_[^"]*"[^>]*>(.*?)</p>', re.IGNORECASE | re.DOTALL)


def _clean(fragment: str) -> str:
    return _WS.sub(" ", htmlmod.unescape(_TAG.sub("", fragment))).strip()


def _parse_results(page: str, limit: int) -> list[tuple[str, str, str]]:
    """``(title, source, snippet)`` per organic result on a Bing HTML results page.

    The ``href`` on these pages is a ``/ck/a`` redirect whose target is now
    encrypted, so the readable ``<cite>`` is what gets reported. That is the honest
    choice rather than a shortcut: a wrapped link is something the model can
    neither cite nor read, and would only hand straight back to ``fetch_url``.
    """
    rows: list[tuple[str, str, str]] = []
    for block in _ALGO.finditer(page):
        chunk = block.group(0)
        head = _HEAD.search(chunk)
        if head is None:
            continue
        title = _clean(head.group(2))
        if not title:
            continue
        cite = _CITE.search(chunk)
        source = _clean(cite.group(1)).replace(" › ", "/") if cite else ""
        body = _SNIPPET.search(chunk)
        rows.append((title[:120], source[:160], _clean(body.group(1))[:300] if body else ""))
        if len(rows) >= limit:
            break
    return rows


def _search_web(
    *, timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS, opener: object | None = None
) -> ToolHandler:
    def handler(arguments: Mapping[str, object]) -> str:
        raw = arguments.get("query")
        if not isinstance(raw, str) or not raw.strip():
            raise ToolExecutionError("缺少搜索词")
        query = raw.strip()[:200]
        raw_max = arguments.get("max_results")
        wanted = raw_max if isinstance(raw_max, int) and 0 < raw_max <= MAX_RESULTS else MAX_RESULTS
        params = urllib.parse.urlencode({"q": query, "count": wanted, "setlang": "zh-hans"})
        rows: list[tuple[str, str, str]] = []
        failures: list[str] = []
        engine = "bing"
        received = False
        for endpoint, provider in (
            (f"{SEARCH_ENDPOINT}?{params}&format=rss", "bing"),
            (f"{SEARCH_ENDPOINT}?{params}", "bing"),
            ("https://www.sogou.com/web?" + urllib.parse.urlencode({"query": query}), "sogou"),
            (
                "https://html.duckduckgo.com/html/?" + urllib.parse.urlencode({"q": query}),
                "duckduckgo",
            ),
        ):
            try:
                _, body, _ = _get(endpoint, timeout_seconds=timeout_seconds, opener=opener)
                received = True
            except ToolExecutionError as exc:
                failures.append(str(exc))
                continue
            page = body.decode(DEFAULT_ENCODING, errors="replace")
            rows = _parse_rss(page, wanted) or _parse_results(page, wanted)
            if not rows and provider == "duckduckgo":
                rows = _parse_duckduckgo(page, wanted)
            if not rows and provider == "sogou":
                rows = _parse_sogou(page, wanted)
            # A search engine sometimes ignores the rest of a Chinese query. Do not
            # call a generic country page evidence about university rankings/weather.
            subject = " ".join(title + " " + snippet for title, _, snippet in rows).lower()
            if "大学" in query and not re.search(r"大学|university|universities|college", subject):
                rows = []
            if "天气" in query and not re.search(r"天气|weather|forecast", subject):
                rows = []
            if rows:
                engine = provider
                break
        if not rows:
            if not received and failures:
                raise ToolExecutionError("免Key搜索暂不可用：" + "；".join(failures))
            return (
                f"「{query}」没有搜到结果（已尝试免Key搜索备用入口）。"
                "可能是搜索页改版，也可能这个词确实没命中——换个说法再试，"
                "或者用 fetch_url 直接打开你已经知道的网址。"
            )
        lines = [f"联网搜索「{query}」，{len(rows)} 条结果（搜索引擎：{engine}）："]
        for index, (title, source, snippet) in enumerate(rows, start=1):
            where = f"（{source}）" if source else ""
            lines.append(f"[{index}] {title}{where}\n{snippet}")
        return "\n".join(lines)[:MAX_RESULT_CHARS]

    return handler


def _parse_rss(page: str, limit: int) -> list[tuple[str, str, str]]:
    if "<!DOCTYPE" in page.upper() or "<rss" not in page[:500].lower():
        return []
    try:
        root = ET.fromstring(page)
    except ET.ParseError:
        return []
    rows = []
    for item in root.findall("./channel/item"):
        title = html_to_text(item.findtext("title", ""))
        link = item.findtext("link", "").strip()
        if title and link.startswith(("https://", "http://")):
            rows.append(
                (title[:120], link[:700], html_to_text(item.findtext("description", ""))[:300])
            )
        if len(rows) >= limit:
            break
    return rows


def _parse_duckduckgo(page: str, limit: int) -> list[tuple[str, str, str]]:
    rows = []
    pattern = r'<a\b[^>]*class="[^"]*result__a[^"]*"[^>]*href="([^"]+)"[^>]*>(.*?)</a>'
    for match in re.finditer(pattern, page, re.DOTALL):
        link, title = htmlmod.unescape(match[1]), html_to_text(match[2])
        if link.startswith("//"):
            link = "https:" + link
        wrapped = urllib.parse.parse_qs(urllib.parse.urlsplit(link).query).get("uddg")
        if wrapped:
            link = wrapped[0]
        if title and link.startswith(("https://", "http://")):
            rows.append((title[:120], link[:700], ""))
        if len(rows) >= limit:
            break
    return rows


def _parse_sogou(page: str, limit: int) -> list[tuple[str, str, str]]:
    """Read organic result headings, excluding generated answers and ad scripts."""
    rows = []
    for match in re.finditer(r"<h3\b[^>]*>(.*?)</h3>(.{0,1800})", page, re.DOTALL):
        anchor = re.search(r'<a\b[^>]*href="([^"]+)"[^>]*>(.*?)</a>', match[1], re.DOTALL)
        if not anchor:
            continue
        title, link = html_to_text(anchor[2]), htmlmod.unescape(anchor[1])
        link = urllib.parse.urljoin("https://www.sogou.com", link)
        if title and link.startswith(("https://", "http://")):
            fragment = match[2].rsplit("<", 1)[0]
            rows.append((title[:120], link[:700], html_to_text(fragment)[:300]))
        if len(rows) >= limit:
            break
    return rows


def build(
    *, timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS, opener: object | None = None
) -> list[tuple[ToolSpec, ToolHandler]]:
    """Return the tools this module contributes.

    Args:
        timeout_seconds: Per-request timeout.
        opener: Optional ``urllib`` opener override; tests inject one so the
            suite never touches the network.
    """
    return [
        (_FETCH_URL, _fetch_url(timeout_seconds=timeout_seconds, opener=opener)),
        (_SEARCH_WEB, _search_web(timeout_seconds=timeout_seconds, opener=opener)),
    ]


__all__ = [
    "ALLOWED_SCHEMES",
    "MAX_BYTES",
    "MAX_RESULTS",
    "SEARCH_ENDPOINT",
    "build",
    "html_to_text",
    "validate_url",
]
