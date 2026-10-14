"""扩展市场这一层的词汇表：清单条目、搜索结果、安装结果。

三种货只有两种**能装**（`skill` / `mcp`）。`plugin`（`plugin.yaml` + Python 入口）这个
形状仍然存在于 `jarvis/plugins/`，但**市场不卖它**：那等于从网上下代码 import 进助手自己的
进程，出事没人负责。理由写在 `docs/扩展市场与技能方案.md` §4.1，页面上也要看得见这句话。

`CatalogItem.install` 是一个不透明的 Mapping —— 形状由 `kind` 决定，由 `installer.py` 解释。
不在这里铺平成字段是有原因的：技能要的是 `repo/ref/path`，MCP 要的是 `command/args/env`，
硬塞进同一个 dataclass 会让每个字段对另一半都是 `None`，那种形状最容易写出"忘了检查 kind"的分支。
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Final

KIND_SKILL: Final = "skill"
KIND_MCP: Final = "mcp"
VALID_KINDS: Final = (KIND_SKILL, KIND_MCP)

CURATED_URL: Final = "http://124.220.54.64/latest-extensions.json"
"""作者自己那台服务器上的精选清单（这一份由我发、我校验）。

它存在的理由不是"多一路结果"：买家网络可能出不去 npm / GitHub，而这一路是我这边可控的；
同时它是唯一**逐条被我验过能装**的来源，所以页面上它标的是「作者验过」，不是「热门」。
"""

NPM_SEARCH_URL: Final = "https://registry.npmjs.org/-/v1/search"
GITHUB_SEARCH_URL: Final = "https://api.github.com/search/repositories"
GITHUB_CONTENTS_URL: Final = "https://api.github.com/repos/{repo}/contents/{path}"
RAW_BASE: Final = "https://raw.githubusercontent.com/"

SOURCE_BUDGET_SECONDS: Final = 9.0
"""一个来源的墙钟预算。超了就报"这一路太慢"，不整页卡住。"""

MAX_ITEMS_PER_SOURCE: Final = 24
"""每个来源最多带回这么多条（再多也不是人看得完的量）。"""


@dataclass(frozen=True, slots=True)
class CatalogItem:
    """一条可装的东西。"""

    kind: str
    id: str
    name: str
    summary: str
    source: str
    """哪一路给的：`curated` / `npm` / `github`。页面上按它分组，也按它解释失败。"""

    install: Mapping[str, object] = field(default_factory=dict)
    homepage: str = ""
    stars: int = 0
    downloads: int = 0
    """npm 的月下载量；GitHub 那一路是星标数。两个都只用于排序，不当质量判断。"""

    needs: tuple[str, ...] = ()
    """装完要跑起来必须在本机存在的命令（`npx` / `uvx` / `python`）。"""

    pinned: str = ""
    """技能：来源的 ref（分支或 commit）。写 commit 的那几条不会跟着上游改版漂走。"""

    icon: str = ""
    """图标：emoji 那一两个字符，或一个 https 头像地址（过 `sources.safe_icon` 的白名单）。

    空串是**正常值**，不是失败：npm 的搜索响应里根本没有图标字段。界面拿它退化成
    名字首字母方块，并在详情页写明"来源没给图标"。
    """

    description: str = ""
    """详情页那一段长说明。`summary` 是一行，这一栏是"它到底干什么、有什么前提"。"""

    version: str = ""
    """来源自己报的版本（npm 是当前版本号、精选清单是作者写的那一栏）。

    技能的"有没有新版"**不靠它** —— 那份比的是文件本身的 sha256，见 `installer.py`。
    """

    def to_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "id": self.id,
            "name": self.name,
            "summary": self.summary,
            "source": self.source,
            "install": dict(self.install),
            "homepage": self.homepage,
            "stars": self.stars,
            "downloads": self.downloads,
            "needs": list(self.needs),
            "pinned": self.pinned,
            "icon": self.icon,
            "description": self.description,
            "version": self.version,
        }


@dataclass(frozen=True, slots=True)
class SourceOutcome:
    """一路搜索的结果 —— 失败也要带得回原因，不能是空列表。

    空列表和"这路没通"在界面上长得一模一样，而用户下一步要做的完全不同：
    前者是"换个词"，后者是"看看网/等限流过去"。这是本仓库反复在补的那一课。
    """

    source: str
    label: str
    items: tuple[CatalogItem, ...] = ()
    error: str = ""
    milliseconds: int = 0

    def to_dict(self) -> dict[str, object]:
        return {
            "source": self.source,
            "label": self.label,
            "hits": len(self.items),
            "error": self.error,
            "milliseconds": self.milliseconds,
        }


@dataclass(frozen=True, slots=True)
class SearchResult:
    """三路并发的总返回。"""

    query: str
    sources: tuple[SourceOutcome, ...]
    items: tuple[CatalogItem, ...]
    at: float = field(default_factory=time.time)

    @property
    def failures(self) -> tuple[str, ...]:
        return tuple(f"{row.label}：{row.error}" for row in self.sources if row.error)

    def to_dict(self) -> dict[str, object]:
        return {
            "query": self.query,
            "items": [item.to_dict() for item in self.items],
            "sources": [row.to_dict() for row in self.sources],
            "failures": list(self.failures),
            "at": self.at,
            "error": "",
        }


@dataclass(frozen=True, slots=True)
class InstallResult:
    """一次安装的结论。`ok=False` 时 `reason` 必须是能直接上屏的一句人话。"""

    ok: bool
    kind: str
    name: str
    path: str = ""
    reason: str = ""
    detail: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "kind": self.kind,
            "name": self.name,
            "path": self.path,
            "reason": self.reason,
            "detail": self.detail,
        }


# 类型别名不放最后：上面那几个 dataclass 的字段注解要用 `Mapping`，它在文件顶上 import。


def string_list(value: object) -> list[str]:
    """把一份 `dict[str, object]` 里那一格当字符串列表读；形状不对就当没有。

    远端清单每一条都是松散映射：直接迭代 mypy 不放过，`str(value)` 一把兜底又会把半份
    配置当合法配置收下 —— 装上之后连不上而界面说不出为什么，正是这个项目明令不许的。
    """
    if isinstance(value, (list, tuple, set, frozenset)):
        return [str(one) for one in value]
    return []


__all__ = [
    "CURATED_URL",
    "GITHUB_CONTENTS_URL",
    "GITHUB_SEARCH_URL",
    "KIND_MCP",
    "KIND_SKILL",
    "MAX_ITEMS_PER_SOURCE",
    "NPM_SEARCH_URL",
    "RAW_BASE",
    "SOURCE_BUDGET_SECONDS",
    "VALID_KINDS",
    "CatalogItem",
    "InstallResult",
    "SearchResult",
    "SourceOutcome",
    "string_list",
]
