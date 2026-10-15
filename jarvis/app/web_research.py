"""Conservative, key-free web routing; external text is evidence, never policy."""

from __future__ import annotations

import re
from urllib.parse import urlsplit

_OFFLINE = re.compile(
    r"不要联网|别联网|禁止联网|离线回答|仅[用查看].{0,8}(资料|文档)|只[用查看].{0,8}(资料|文档)"
)
_CURRENT = re.compile(
    r"联网|上网|网上|搜索|探索|调研|查一下|查下|最新|今天|今年|目前|现在.{0,8}(谁|多少|哪些)|"
    r"新闻|天气|股价|汇率|排名|排行榜|前十|top\s*10|大学.{0,8}有哪些",
    re.IGNORECASE,
)
_URL = re.compile(r"https?://[^\s<>）)\]\"']+")


def wants_web(question: str) -> bool:
    if re.search(r"今天几号|现在几点|今天星期|今天周几|现在时间", question):
        return False
    return not bool(_OFFLINE.search(question)) and bool(_CURRENT.search(question))


def public_sources(text: str) -> tuple[str, ...]:
    """Only link-shaped sources, excluding signed/private-looking query strings."""
    result: list[str] = []
    headings = re.findall(r"^\[\d+\].*?（(https?://[^）]+)）", text, re.MULTILINE)
    for found in headings or _URL.findall(text):
        url = found.rstrip("，。；,.;")
        parsed = urlsplit(url)
        if parsed.username or parsed.password or not parsed.hostname:
            continue
        if any(key in parsed.query.lower() for key in ("token=", "key=", "signature=")):
            continue
        if url not in result:
            result.append(url)
    return tuple(result[:5])
