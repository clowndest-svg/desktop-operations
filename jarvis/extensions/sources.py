"""三路搜索：作者的精选清单、npm（MCP server）、GitHub（技能仓库）。

抓取这一层刻意保持"可注入 opener"：真 HTTP 用 `urllib`，测试里换成假响应。
**URL 的vetting 复用 `jarvis.tools.builtins.web_tools.validate_url`** —— 复制一份安全规则
迟早会漂，而漂了的那一份看起来完全正常：这一句是对着本项目历史上几次"两处校验不一致"写的。

三条设计约束：

1. **每一路单独返回失败原因**（`SourceOutcome.error`），空列表 ≠ 没通；
2. **每一路都有墙钟预算和条数上限**：页面是人在等的，不是爬虫；
3. GitHub 未认证限流是 60 次/小时 —— 所以带一层结果缓存，并在被限流时把"什么时候可以再试"
   原样带回去。装作能无限搜，是让用户每小时撞一次墙。
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import replace
from typing import Final

from jarvis.extensions.types import (
    CURATED_URL,
    GITHUB_CONTENTS_URL,
    GITHUB_SEARCH_URL,
    KIND_MCP,
    KIND_SKILL,
    MAX_ITEMS_PER_SOURCE,
    NPM_SEARCH_URL,
    SOURCE_BUDGET_SECONDS,
    CatalogItem,
    SearchResult,
    SourceOutcome,
)
from jarvis.tools.builtins.web_tools import validate_url

logger = logging.getLogger(__name__)

USER_AGENT: Final = "xiaoYe/extensions"
MAX_BYTES: Final = 900 * 1024
"""清单和搜索结果都不该超过这个量；超了是拿错了东西，不是"内容多"。"""

CACHE_SECONDS: Final = 300.0
"""同一个搜索词在五分钟内重复问，走缓存。GitHub 的未认证配额撑不住"边打字边搜"。"""

_TIMEOUT: Final = 8.0
_SKILL_REPO_QUERY: Final = "skills in:name,description fork:false stars:>30"
_GITHUB_REPOS: Final = 3
"""GitHub 那一路最多看几个仓库（每个还要再列一次目录，串行）。"""

GITHUB_SOFT_SECONDS: Final = 6.5
"""到点就带回已有结果，并说明"只来得及看前几条"（见 :func:`github_items`）。"""


Fetcher = Callable[[str, float], tuple[str, bytes]]
"""(url, timeout) → (最终地址, 正文)。测试注一个假的进来。"""


def guarded_fetch(url: str, timeout: float = _TIMEOUT) -> tuple[str, bytes]:
    """真抓取：先 vet URL，再取，再看**重定向之后**落在哪儿。

    重定向后再验一次不是洁癖：`http://公开地址 → http://127.0.0.1:后台` 这一跳
    把"公开搜索"变成"她替你看你的内网"，是本机端口扫描那类事故的入口。
    """
    start = validate_url(url)
    request = urllib.request.Request(
        start,
        headers={
            "User-Agent": f"{USER_AGENT}/1.0 (desktop assistant)",
            "Accept": "application/json, text/plain;q=0.8, */*;q=0.5",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            final = str(response.geturl() or start)
            body = response.read(MAX_BYTES + 1)
            if len(body) > MAX_BYTES:
                raise ValueError("这份清单超过 900 KB，读错了东西还是发错了东西？")
    except urllib.error.HTTPError as exc:
        retry = exc.headers.get("Retry-After") if exc.headers else None
        hint = f"（{retry} 秒后再试）" if retry else ""
        raise ValueError(f"那边回了 {exc.code}{hint}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        reason = getattr(exc, "reason", None) or exc
        raise ValueError(f"连不上：{reason}") from exc
    validate_url(final)
    return final, body


def _json(body: bytes) -> object:
    try:
        return json.loads(body.decode("utf-8", errors="replace"))
    except ValueError as exc:
        raise ValueError("返回的不是 JSON —— 地址可能被别的网站占了") from exc


# --------------------------------------------------------------------------- 精选清单


def curated_items(fetch: Fetcher) -> list[CatalogItem]:
    raw = _json(fetch(CURATED_URL, _TIMEOUT)[1])
    if not isinstance(raw, Mapping):
        raise ValueError("精选清单不是一份对象")
    rows = raw.get("items")
    if not isinstance(rows, list):
        raise ValueError("精选清单里没有 items 这一栏")
    out: list[CatalogItem] = []
    for row in rows:
        item = _item_from_curated(row)
        if item is not None:
            out.append(item)
    return out[:MAX_ITEMS_PER_SOURCE]


def _item_from_curated(row: object) -> CatalogItem | None:
    if not isinstance(row, Mapping):
        return None
    kind = str(row.get("kind") or "")
    install = row.get("install")
    if kind not in {KIND_SKILL, KIND_MCP} or not isinstance(install, Mapping):
        return None
    return CatalogItem(
        kind=kind,
        id=str(row.get("id") or f"{kind}-{row.get('name')}"),
        name=str(row.get("name") or ""),
        summary=str(row.get("summary") or ""),
        source="curated",
        install=dict(install),
        homepage=str(row.get("homepage") or ""),
        needs=tuple(str(one) for one in row.get("needs") or ()),
        pinned=str(row.get("ref") or ""),
    )


# --------------------------------------------------------------------------- npm


def npm_items(query: str, fetch: Fetcher) -> list[CatalogItem]:
    """npm 搜 MCP server。绝大多数 npm 的 MCP server 靠 `npx -y <包>` 起。

    只认包名里带 `mcp` 的（`@modelcontextprotocol/server-*` 或 `mcp-server-*`）：
    npm 的搜索是模糊的，返回一堆"名字里含 mcp"的无关包，装上去起不来。
    """
    url = f"{NPM_SEARCH_URL}?text={urllib.parse.quote(f'mcp {query}')}&size=25"
    raw = _json(fetch(url, _TIMEOUT)[1])
    if not isinstance(raw, Mapping):
        raise ValueError("npm 返回的不是对象")
    out: list[CatalogItem] = []
    for row in raw.get("objects") or ():
        package = row.get("package") if isinstance(row, Mapping) else None
        if not isinstance(package, Mapping):
            continue
        name = str(package.get("name") or "")
        if not _looks_like_mcp(name):
            continue
        score = package.get("score") or {}
        stats = package.get("statistics") or {}
        out.append(
            CatalogItem(
                kind=KIND_MCP,
                id=f"npm:{name}",
                name=name,
                summary=str(package.get("description") or "")[:280],
                source="npm",
                install={"transport": "stdio", "command": "npx", "args": ["-y", name]},
                homepage=str(package.get("url") or ""),
                downloads=(
                    int(float(stats.get("monthly") or 0)) if isinstance(stats, Mapping) else 0
                ),
                stars=int(float(score.get("final") or 0)) if isinstance(score, Mapping) else 0,
                needs=("npx",),
                pinned=str(package.get("version") or ""),
            )
        )
    return out[:MAX_ITEMS_PER_SOURCE]


# --------------------------------------------------------------------------- GitHub


def github_items(query: str, fetch: Fetcher) -> list[CatalogItem]:
    """GitHub 搜技能仓库，并把每个仓库里那一层技能目录摊平成条目。

    一次仓库搜索 + 每仓一次目录列举：技能安装需要**具体的 SKILL.md 路径**，只给"某个仓库
    有技能"这种结果，装的时候还得再问一次，界面上就会变成一个不会动的按钮。

    **软截止是这条必须的**：6 个仓库 × 每个最多 4 次目录列举，每次 0.6~1 秒，串起来
    10 秒起步 —— 而整页只给一路 9 秒（本轮实测这一路每次都恰好被判超时，返回 0 条）。
    现在最多看 3 个仓库、单次 5 秒、到点就把手上已有的带回来：页面上看到的是"只来得及
    看前两条"，而不是空列表。
    """
    deadline = time.monotonic() + GITHUB_SOFT_SECONDS
    text = f"{_SKILL_REPO_QUERY} {query}".strip()
    url = (
        f"{GITHUB_SEARCH_URL}?q={urllib.parse.quote(text)}&sort=stars&order=desc"
        f"&per_page={_GITHUB_REPOS}"
    )
    raw = _json(fetch(url, 5.0)[1])
    if not isinstance(raw, Mapping):
        raise ValueError("GitHub 返回的不是对象")
    items: list[CatalogItem] = []
    partial = False
    for repo in raw.get("items") or ():
        if not isinstance(repo, Mapping):
            continue
        full = str(repo.get("full_name") or "")
        if not full:
            continue
        if time.monotonic() > deadline:
            partial = True
            break
        ref = str(repo.get("default_branch") or "main")
        items.extend(
            _skills_of_repo(
                full,
                ref,
                str(repo.get("description") or ""),
                int(repo.get("stargazers_count") or 0),
                fetch,
            )
        )
    if partial:
        items.append(
            CatalogItem(
                kind=KIND_SKILL,
                id="github:partial",
                name="（这一路只来得及看前几条）",
                summary=(
                    "GitHub 的仓库搜索是串行的，到点就先把手上的带回来；" "再搜一次通常会多看几条。"
                ),
                source="github",
                install={"repo": "", "ref": "", "path": ""},
            )
        )
    return items[:MAX_ITEMS_PER_SOURCE]


_SKILL_FOLDERS: Final = ("skills", "plugins/skills", ".claude/skills")


def _skills_of_repo(
    repo: str, ref: str, description: str, stars: int, fetch: Fetcher
) -> list[CatalogItem]:
    """一个仓库 → 若干条"可直接装"的技能条目。

    三种形状都要认（这些都是社区里真在用的写法）：`skills/<名字>/SKILL.md`、
    `.claude/skills/…`、以及整个仓库就是一个技能（根目录直接放 SKILL.md）。
    一个都不认的时候仍回**仓库那一条**并让它带着原因 —— 结果是"没有可装的具体技能"，
    而不是从列表里消失。
    """
    whole = CatalogItem(
        kind=KIND_SKILL,
        id=f"github:{repo}",
        name=repo,
        summary=description[:280] or "一个技能仓库",
        source="github",
        install={"repo": repo, "ref": ref, "path": ""},
        homepage=f"https://github.com/{repo}",
        stars=stars,
        pinned=ref,
    )
    for folder in _SKILL_FOLDERS:
        listing = _contents(repo, ref, folder, fetch)
        if listing is None:
            continue
        rows = [
            replace(
                whole,
                id=f"github:{repo}:{entry['name']}",
                name=f"{repo} · {entry['name']}",
                install={"repo": repo, "ref": ref, "path": f"{folder}/{entry['name']}"},
            )
            for entry in listing
            if isinstance(entry, Mapping) and entry.get("type") == "dir" and entry.get("name")
        ][: MAX_ITEMS_PER_SOURCE // 2]
        if rows:
            return rows
    root = _contents(repo, ref, "", fetch)
    if root and any(
        isinstance(entry, Mapping) and entry.get("name") == "SKILL.md" for entry in root
    ):
        return [whole]
    return [
        replace(
            whole,
            summary=(whole.summary + "（没在里面找到标准形状的技能目录，装这一条会说明缺了什么）")[
                :280
            ],
        )
    ]


def _contents(repo: str, ref: str, path: str, fetch: Fetcher) -> list[Mapping[str, object]] | None:
    """列一个目录；不存在 / 读不到都回 ``None``（"这里没有"和"读不了"在这一步不必区分）。"""
    url = (
        GITHUB_CONTENTS_URL.format(repo=repo, path=urllib.parse.quote(path))
        + f"?ref={urllib.parse.quote(ref)}"
    )
    try:
        raw = _json(fetch(url, _TIMEOUT)[1])
    except ValueError:
        return None
    return list(raw) if isinstance(raw, list) else None


# --------------------------------------------------------------------------- 并发总入口


def _looks_like_mcp(name: str) -> bool:
    """这个包名像不像一个 MCP server。

    官方那批叫 `@modelcontextprotocol/server-*` —— 里面**没有**"mcp"这三个连着的字母，
    只按子串筛就会把最正经的那几条全丢掉（本轮实测撞出来的）。社区那批是 `mcp-server-*` /
    `*-mcp`。npm 的搜索是模糊的，装一个不是 MCP server 的包 = 起一个永远连不上的进程，
    那比搜不到更坏，所以宁可窄。
    """
    lowered = name.lower()
    parts = re.split(r"[/.@-]+", lowered)
    return (
        lowered.startswith("@modelcontextprotocol/")
        or "mcp" in parts
        or "mcp-server" in lowered
        or "server-mcp" in lowered
    )


class SearchCache:
    """按 (query, kinds) 缓存五分钟。

    缓存存在的唯一理由是 GitHub 那 60 次/小时的未认证配额：一个"边打字边搜"的页面
    在两分钟内就能把它烧光，然后把用户锁在外面一小时。缓存不是性能优化，是配额保护。
    """

    def __init__(
        self, ttl: float = CACHE_SECONDS, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self._ttl = ttl
        self._clock = clock
        self._rows: dict[tuple[str, tuple[str, ...]], tuple[float, SearchResult]] = {}
        self._lock = threading.Lock()

    def get(self, key: tuple[str, tuple[str, ...]]) -> SearchResult | None:
        with self._lock:
            row = self._rows.get(key)
        if row is None or self._clock() - row[0] > self._ttl:
            return None
        return row[1]

    def put(self, key: tuple[str, tuple[str, ...]], value: SearchResult) -> None:
        with self._lock:
            self._rows[key] = (self._clock(), value)

    def clear(self) -> None:
        with self._lock:
            self._rows.clear()


_Build = Callable[[str, Fetcher], list[CatalogItem]]
# 类型写死是为了 builder 的返回值不用调用方猜：这一路失败要抛，成功要回一个列表。

_BUILDERS: Final[dict[str, tuple[str, _Build]]] = {
    "curated": ("精选（作者逐条验过）", lambda query, fetch: curated_items(fetch)),
    "npm": ("npm · MCP server", lambda query, fetch: npm_items(query, fetch)),
    "github": ("GitHub · 技能仓库", lambda query, fetch: github_items(query, fetch)),
}


def search_all(
    query: str,
    *,
    kinds: tuple[str, ...] = (KIND_SKILL, KIND_MCP),
    fetch: Fetcher | None = None,
    sources: tuple[str, ...] = ("curated", "npm", "github"),
    budget_seconds: float = SOURCE_BUDGET_SECONDS,
) -> SearchResult:
    """三路并发搜，返回合并结果 + 每路自己的成败。

    精选清单不看搜索词是否命中：它小、由我校验，条数本来就该全给你（页面按 kind 筛）。
    npm / GitHub 两路按词搜，词为空时那两路直接回"没给词"，不发请求 —— 免得空搜索
    把配额烧光。
    """
    do_fetch = fetch or guarded_fetch
    clean = " ".join(str(query or "").split())
    rows: list[SourceOutcome] = []

    def one(name: str) -> SourceOutcome:
        label, builder = _BUILDERS[name]
        started = time.monotonic()
        try:
            if name != "curated" and not clean:
                raise ValueError("这一路要给关键词才有结果")
            found = [item for item in builder(clean, do_fetch) if item.kind in kinds]
            error = ""
        except Exception as exc:
            found, error = [], f"{type(exc).__name__}: {exc}"[:220]
            logger.info("扩展搜索这一路没通：%s %s", name, error)
        spent = int((time.monotonic() - started) * 1000)
        return SourceOutcome(
            source=name,
            label=label,
            items=tuple(found[:MAX_ITEMS_PER_SOURCE]),
            error=error,
            milliseconds=spent,
        )

    from concurrent.futures import ThreadPoolExecutor, wait

    with ThreadPoolExecutor(
        max_workers=len(sources), thread_name_prefix="jarvis-extensions"
    ) as pool:
        futures = [pool.submit(one, name) for name in sources]
        # 只给 0.25 秒的余量：加两秒的话，「0.2 秒预算」这种测试会等满两秒再拿到结果，
        # 于是超时那一条分支永远不会被走到，而测试仍然是绿的（本仓库的老坑）。
        wait(futures, timeout=budget_seconds + 0.25)
        for index, name in enumerate(sources):
            future = futures[index]
            if future.done():
                rows.append(future.result())
            else:
                future.cancel()
                rows.append(
                    SourceOutcome(
                        source=name,
                        label=_BUILDERS[name][0],
                        error=f"这一路超过 {budget_seconds:.0f} 秒没回，先不等它",
                        milliseconds=int(budget_seconds * 1000),
                    )
                )
    items = tuple(item for row in rows for item in row.items)
    return SearchResult(query=clean, sources=tuple(rows), items=items)


__all__ = [
    "MAX_BYTES",
    "USER_AGENT",
    "SearchCache",
    "curated_items",
    "github_items",
    "guarded_fetch",
    "npm_items",
    "search_all",
]
