"""Conservative, key-free web routing; external text is evidence, never policy."""

from __future__ import annotations

import re
from urllib.parse import urlsplit

_OFFLINE = re.compile(
    r"不要联网|别联网|禁止联网|离线回答|仅[用查看].{0,8}(资料|文档)|只[用查看].{0,8}(资料|文档)|"
    r"\b(?:offline|do not (?:browse|search)|don't (?:browse|search)|"
    r"without (?:internet|browsing))\b|"
    r"ネットに接続しない|検索しない|オフライン|오프라인|인터넷 없이|ne cherche pas|sans internet",
    re.IGNORECASE,
)
NETWORK_QUERY = re.compile(
    r"联网|上网|网上|搜索|探索|调研|查一下|查下|最新|今天|今年|目前|现在.{0,8}(谁|多少|哪些)|"
    r"新闻|天气|股价|汇率|排名|排行榜|前十|top\s*10|大学.{0,8}有哪些|"
    r"\b(?:latest|current|today|news|weather|rankings?|exchange rates?|stock price|"
    r"search (?:the )?(?:web|online)|look up|browse (?:the )?(?:web|internet))\b|"
    r"最新|ニュース|天気|調べて|検索して|ランキング|날씨|최신|검색|순위|"
    r"\b(?:actualit[eé]s|m[eé]t[eé]o|classement|recherche|noticias|clasificaci[oó]n|"
    r"buscar en internet|nachrichten|wetter|rangliste|онлайн|погода|новости|рейтинг)\b",
    re.IGNORECASE,
)
_URL = re.compile(r"https?://[^\s<>）)\]\"']+")
_CORPUS_ONLY = re.compile(
    r"(?:仅|只)(?:能|可)?(?:根据|依据|使用|用|从).{0,6}(?:知识库|资料库|导入的?(?:资料|文档))|"
    r"\bonly (?:use|using|from) (?:the |my )?(?:knowledge base|imported documents)\b",
    re.IGNORECASE,
)


def corpus_only(question: str) -> bool:
    return bool(_CORPUS_ONLY.search(question))


def wants_web(question: str) -> bool:
    if re.search(r"今天几号|现在几点|今天星期|今天周几|现在时间", question):
        return False
    return (
        not corpus_only(question)
        and not bool(_OFFLINE.search(question))
        and bool(NETWORK_QUERY.search(question))
    )


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
