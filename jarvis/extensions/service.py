"""``ExtensionsService`` —— 页面和对话共用的那一层门面。

三个决定值得先写明，因为它们是这层的安全边界，不是实现细节：

1. **`install(item_id)` 只认 id，不认页面传来的安装形状。** 搜索结果在本服务里缓存一份，
   装的时候按 id 取回**我们自己发出去的那份 spec**。否则"调用桥面的人"可以顺手把
   `command` 改成任何一条本机命令 —— 那就不再是装扩展，是给任意程序开个入口。
2. **`stdio` 类（要起进程）只能从搜索结果装；页面上手工加的那一条只许 `http`。**
   http 的 MCP server 不起任何本机进程，风险和 `fetch_url` 同阶；stdio 一旦能手工填，
   这页就等价于一个命令行输入框。真要手工加，路径是 `config.yaml`（那是人在写自己的机器）。
3. **它不知道 MCP 怎么连、技能怎么读**：连接状态靠注入的 provider 拿，技能动作直接调
   `SkillService`。分层表里 extensions 不许 import `jarvis.mcp`，这条约束也是它更好测的原因。
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Final, cast

from jarvis.core.constants import DEFAULT_ENCODING
from jarvis.extensions.installer import install_mcp, install_skill, read_provenance, update_skill
from jarvis.extensions.runtime import RuntimeState, detect
from jarvis.extensions.servers import (
    ServerStoreError,
    entries,
    load,
    normalise_key,
    path_for,
    remove,
    save,
    set_entry_enabled,
    set_layer_enabled,
)
from jarvis.extensions.sources import SearchCache, curated_items, guarded_fetch, search_all
from jarvis.extensions.types import (
    KIND_MCP,
    KIND_SKILL,
    CatalogItem,
    InstallResult,
    SearchResult,
    string_list,
)
from jarvis.skills.loader import slugify
from jarvis.skills.service import SkillService
from jarvis.tools import RiskLevel, ToolRegistry, ToolSpec
from jarvis.tools.types import object_schema, string_property

logger = logging.getLogger(__name__)

EXTENSION_SOURCE: Final = "extension:market"
LIST_TOOL: Final = "extensions_list"
CACHE_SECONDS: Final = 900.0
"""搜索结果留 15 分钟：装的那一下要能从缓存里按 id 取回原始 spec。"""

_RECOMMEND_KEY: Final = ("__recommended__", ())
"""推荐那一格在同一个缓存里的键（它没有搜索词，所以词那一栏是空的）。"""

RUN_LATEST_COMMANDS: Final = frozenset({"npx", "uvx", "pipx"})
"""这几个启动器**每次起进程都现拉最新版**，所以那一条没有「更新」这一步。

判断走命令行而不是走来源：精选清单里也有一条 `npx -y …`，对它说「可以更新」就是假话。
"""

_SPEC = ToolSpec(
    name=LIST_TOOL,
    description=(
        "看她自己装过什么：已安装的技能（名字 + 一句说明）、登记过的 MCP server、"
        "每一个现在连着没连着、以及这台机器有没有装 Node/npx/uvx。只读。"
        "用户问「我装了哪些扩展」「那个连不上是为什么」时用这一条，不要凭记忆答。"
    ),
    parameters=object_schema(
        {"detail": string_property("留空即可；填 any 会多带最近一次搜索的失败原因")}
    ),
    risk=RiskLevel.SAFE,
    permissions=frozenset(),
    source=EXTENSION_SOURCE,
)


class _Look:
    """一次 `state()` / `detail()` 期间的共用查表数据。

    为什么要有：判"这条装了没"要扫技能目录、读每份来历、读一次 servers.json、问一次本机命令。
    列表上几十条每条都来一遍的话，打开页面就得几十次 `which` —— 而**更要紧的是**：列表和详情
    各算一遍迟早会算出两句话，同一个东西在两个位置上说法不一样正是这个项目反复在补的那类 bug。
    """

    def __init__(self, service: ExtensionsService) -> None:
        self.skills = list(service._skills.installed())
        self.runtime = list(detect()["present"])
        self.servers: dict[str, dict[str, object]] = {}
        self.store_error = ""
        try:
            self.servers = entries(load(service._path()))
        except ServerStoreError as exc:
            self.store_error = str(exc)
        self.provenance: dict[str, dict[str, object]] = {}

    def prov(self, path: str) -> dict[str, object]:
        cached = self.provenance.get(path)
        if cached is None:
            cached = read_provenance(Path(path))
            self.provenance[path] = cached
        return cached


class ExtensionsService:
    """市场这一层的生命周期组件。"""

    name = "extensions"

    def __init__(
        self,
        *,
        skills: SkillService,
        extensions_dir_provider: Callable[[], Path],
        registry_provider: Callable[[], ToolRegistry] | None = None,
        mcp_status_provider: Callable[[], Mapping[str, object]] | None = None,
        search_fetch: Callable[[str, float], tuple[str, bytes]] | None = None,
        environ: Mapping[str, str] | None = None,
        audit_file_provider: Callable[[], Path] | None = None,
        mcp_connect_provider: Callable[[str], str] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._skills = skills
        self._dir_provider = extensions_dir_provider
        self._registry_provider = registry_provider
        self._mcp_status_provider = mcp_status_provider
        self._fetch = search_fetch
        self._environ = environ
        self._audit_file_provider = audit_file_provider
        self._mcp_connect = mcp_connect_provider
        self._clock = clock
        self._cache = SearchCache(ttl=CACHE_SECONDS, clock=clock)
        self._by_id: dict[str, CatalogItem] = {}
        self._last: SearchResult | None = None
        self._started = False

    # -- 生命周期 ----------------------------------------------------------

    def start(self) -> None:
        self._started = True
        self._sync_tools()
        logger.info("扩展层就绪（%s）", self._dir())

    def stop(self) -> None:
        self._unregister()
        self._started = False

    @property
    def running(self) -> bool:
        return self._started

    # -- 查询 --------------------------------------------------------------

    def state(self) -> dict[str, object]:
        """一整份页面状态。**永远回完整形状**：界面把 undefined 当"没这一格"就不画。"""
        servers_path = path_for(self._dir())
        try:
            data = load(servers_path)
            store_error = ""
        except ServerStoreError as exc:
            data, store_error = {"version": 1, "enabled": False, "servers": {}}, str(exc)
        rows = entries(data)
        mcp = self._mcp_status()
        return {
            "directory": str(self._dir()),
            "servers_file": str(servers_path),
            "layer_enabled": bool(data.get("enabled")),
            "servers": [
                {
                    "name": name,
                    "transport": str(row.get("transport") or "stdio"),
                    "command": str(row.get("command") or ""),
                    "args": string_list(row.get("args")),
                    "url": str(row.get("url") or ""),
                    "enabled": bool(row.get("enabled", True)),
                    "source": str(row.get("source") or ""),
                    "homepage": str(row.get("homepage") or ""),
                    "needs": string_list(row.get("needs")),
                    "env_from": string_list(row.get("env_from")),
                    "note": str(row.get("note") or ""),
                }
                for name, row in sorted(rows.items())
            ],
            "skills": self._skills.stats()["installed"],
            "mcp": mcp,
            "runtime": detect(),
            "search": self._last_search_dict(),
            "store_error": store_error,
            "log": self.log_tail(12),
            # 列表和详情读的是**同一份**判断（谁装了、谁能更新、谁要拒绝），见 `_verdicts`。
            "verdicts": self._verdicts(_Look(self)),
            "error": "",
        }

    def log_tail(self, count: int = 20) -> list[dict[str, object]]:
        """审计的最后几条（倒序）。读不到就回空列表，不抛 —— 这一格是锦上添花。"""
        path = self._audit_file()
        if path is None or not path.is_file():
            return []
        try:
            lines = path.read_text(encoding=DEFAULT_ENCODING, errors="replace").splitlines()
        except OSError as exc:
            logger.info("扩展审计读不了：%s", exc)
            return []
        rows: list[dict[str, object]] = []
        for line in lines[-max(1, count) :]:
            try:
                parsed = json.loads(line)
            except ValueError:
                continue
            if isinstance(parsed, dict):
                rows.append(parsed)
        rows.reverse()
        return rows

    # -- 搜索 --------------------------------------------------------------

    def search(
        self, query: str, kinds: tuple[str, ...] = (KIND_SKILL, KIND_MCP)
    ) -> dict[str, object]:
        clean = " ".join(str(query or "").split())
        key = (clean, tuple(sorted(kinds)))
        found = self._cache.get(key)
        if found is None:
            found = search_all(clean, kinds=kinds, fetch=self._fetch)
            self._cache.put(key, found)
        self._last = found
        for item in found.items:
            self._by_id[item.id] = item
        self._trim_cache()
        return found.to_dict()

    # -- 装 / 卸 -----------------------------------------------------------

    def install(self, item_id: str) -> dict[str, object]:
        """按 id 装。id 不在缓存里就拒绝 —— 这条是"页面不能指定要跑什么命令"的实现。"""
        wanted = str(item_id or "").strip()
        item = self._by_id.get(wanted)
        if item is None:
            return _refuse(
                f"这条不在刚才的搜索结果里（id={wanted or '空'}）。请先搜一次再装 —— "
                "安装不接受页面传来的命令行，否则这一页就等于一个命令输入框。",
                self.state(),
            )
        if item.kind == KIND_SKILL:
            result = install_skill(item, self._skills, fetch=self._fetch, audit=self._audit)
        elif item.kind == KIND_MCP:
            result = install_mcp(item, self._dir(), environ=self._environ, audit=self._audit)
        else:
            result = None
        if result is None:
            return _refuse("认不出这一条是什么类型，没动任何东西", self.state())
        connect = ""
        if result.ok and result.kind == KIND_MCP:
            connect = self._connect_right_away(result.name)
            self._note(f"装完立刻连了一次：{connect}")
        snapshot = self.state()
        row = dict(result.to_dict())
        row["connect"] = connect
        snapshot["install"] = row
        snapshot["error"] = result.reason
        return snapshot

    # -- 推荐 / 详情 / 更新 / 按条目删 ---------------------------------------

    def recommended(self) -> dict[str, object]:
        """打开页面就看得见的那一格：读的就是**精选清单那一路**。

        不掺 npm / GitHub 的"热门" —— 只有这一路是作者逐条验过能装的；而"推荐"两个字
        一旦来自星标数，就是在替用户决定，星标不是质量（`sources.py` 里早写了这句）。

        推荐条目**同样进 `_by_id`**：否则"只认给出去的 id"那条闸会把这一格变成一排
        永远灰着的按钮，而那句"没装也不影响别的本事"就成了假话。
        """
        found = self._cache.get(_RECOMMEND_KEY)
        error = ""
        if found is None:
            try:
                items = tuple(curated_items(self._fetch or guarded_fetch))
            except Exception as exc:
                # 失败原文带回去。「这份清单没读到」和「没有推荐」是两句不同的话。
                items, error = (), f"{type(exc).__name__}: {exc}"[:220]
                logger.info("推荐那一格没读到：%s", error)
            else:
                found = SearchResult(query="", sources=(), items=items)
                self._cache.put(_RECOMMEND_KEY, found)
        rows = found if found is not None else SearchResult(query="", sources=(), items=())
        for item in rows.items:
            self._by_id[item.id] = item
        return {
            "items": [item.to_dict() for item in rows.items],
            "error": error,
            "at": rows.at,
            "where": "作者逐条验过的那份精选清单",
        }

    def detail(self, item_id: str) -> dict[str, object]:
        """详情页那一张卡要的全部东西。**只认我们给出去的 id**，和装同一条规矩。"""
        wanted = str(item_id or "").strip()
        item = self._by_id.get(wanted)
        if item is None:
            out = empty_detail(wanted, reason="这一格没读到")
            out["error"] = (
                f"这条不在我们给出去的那批里（id={wanted or '空'}）。请先搜一次，"
                "或者从推荐那一格点进来 —— 装、更新、删除都只认这个凭据，"
                "页面递进来的命令行一律不收。"
            )
            return out
        look = _Look(self)
        installed = self._installed_of(item, look)
        return {
            "found": True,
            "id": item.id,
            "item": item.to_dict(),
            "installed": installed,
            "install": self._install_verdict(item, installed, look),
            "update": self._update_verdict(item, installed),
            "remove": self._remove_verdict(item, installed),
            "runtime": detect(),
            "log": self.log_tail(5),
            "directory": str(self._dir()),
            "servers_file": str(path_for(self._dir())),
            "error": "",
        }

    def update(self, item_id: str) -> dict[str, object]:
        """按盘上那份来历重新取一次。**判断不了的就明确拒绝并说明**，不装样子。"""
        item = self._by_id.get(str(item_id or "").strip())
        if item is None:
            return _refuse("这条不在我们给出去的那批里，更新不接受页面传来的形状。", self.state())
        verdict = self._update_verdict(item, self._installed_of(item))
        if not bool(verdict["can"]):
            return _refuse(str(verdict["words"]), self.state())
        if item.kind == KIND_SKILL:
            result = update_skill(item, self._skills, fetch=self._fetch, audit=self._audit)
        else:
            result = self._update_server(item)
        snapshot = self.state()
        snapshot["update"] = result.to_dict()
        snapshot["error"] = result.reason
        return snapshot

    def remove_item(self, item_id: str, *, confirmed: object) -> dict[str, object]:
        """从搜索结果或详情页删掉这一条对应的东西。

        **slug / server 名字由后端解析**，不让页面拼：页面拼出来的那个名字和
        `slugify()` / `normalise_key()` 的结果迟早会不一样，而删错东西没有回滚。
        """
        item = self._by_id.get(str(item_id or "").strip())
        if item is None:
            return _refuse("这条不在我们给出去的那批里，删除不接受页面传来的形状。", self.state())
        installed = self._installed_of(item)
        if not bool(installed["yes"]):
            return _refuse("这条本来就没装，什么都没动。", self.state())
        if confirmed is not True:
            return _refuse("删除需要明确的确认（confirmed），这一步不会替你决定", self.state())
        if item.kind == KIND_SKILL:
            slug = str(installed["slug"] or "")
            outcome = self._skills.remove(slug, confirmed=True)
            reason = str(outcome.get("error") or "")
            row = {
                "ok": not reason,
                "kind": KIND_SKILL,
                "name": str(installed["name"] or slug),
                "path": str(installed["path"] or ""),
                "reason": reason,
                "detail": "" if reason else f"删掉了这一份：{slug}",
            }
        else:
            name = str(installed["name"] or "")
            try:
                path = path_for(self._dir())
                merged, existed = remove(load(path), name)
                if not existed:
                    return _refuse(f"没有这条 MCP server：{name}", self.state())
                save(path, merged)
                reason = ""
            except ServerStoreError as exc:
                reason = str(exc)
            row = {
                "ok": not reason,
                "kind": KIND_MCP,
                "name": name,
                "path": str(path_for(self._dir())),
                "reason": reason,
                "detail": "" if reason else f"删掉了这条登记：{name}",
            }
        if not row["reason"]:
            self._note(f"从扩展页删掉 {row['kind']} {row['name']}（按条目 id 解析出来的那一个）")
        snapshot = self.state()
        snapshot["removed"] = row
        snapshot["error"] = row["reason"]
        return snapshot

    # -- 详情要用到的那几件解析 ---------------------------------------------

    def _server_key(self, item: CatalogItem) -> str:
        spec = item.install if isinstance(item.install, Mapping) else {}
        try:
            return normalise_key(str(spec.get("name") or item.name))
        except ServerStoreError:
            return ""

    def _path(self) -> Path:
        return path_for(self._dir())

    def _verdicts(self, look: _Look) -> dict[str, object]:
        """把我们给出去的每一条 id 的"装了没 / 能不能更新 / 能不能删"一次算完。

        为什么在后端算：这三句话要在**搜索结果那一行**和**详情卡**上同时出现。两处各算一遍
        迟早会算出两句话（这个项目为"同一件东西在两个位置上说法不一样"付过不止一次账）。
        """
        out: dict[str, object] = {}
        for item in list(self._by_id.values()):
            installed = self._installed_of(item, look)
            out[item.id] = {
                "installed": installed,
                "install": self._install_verdict(item, installed, look),
                "update": self._update_verdict(item, installed),
                "remove": self._remove_verdict(item, installed),
            }
        return out

    def _installed_of(self, item: CatalogItem, look: _Look | None = None) -> dict[str, object]:
        """这一条在盘上是什么样子：装在哪、启没启、什么时候装的、有没有来历记录。"""
        blank: dict[str, object] = {
            "yes": False,
            "kind": item.kind,
            "name": "",
            "slug": "",
            "path": "",
            "at": 0.0,
            "files": [],
            "digest": "",
            "enabled": False,
            "provenance": False,
            "changed": False,
            "connected": False,
            "words": "还没装",
        }
        table = look if look is not None else _Look(self)
        if item.kind == KIND_SKILL:
            spec = item.install if isinstance(item.install, Mapping) else {}
            wanted_dir = slugify(str(spec.get("path") or "")) if str(spec.get("path") or "") else ""
            for row in table.skills:
                found = table.prov(str(row.path))
                by_id = str(found.get("catalog_id") or "") == item.id
                by_dir = bool(wanted_dir) and Path(row.path).name == wanted_dir and not found
                if not (by_id or by_dir):
                    continue
                return {
                    "yes": True,
                    "kind": KIND_SKILL,
                    "name": row.slug or row.name,
                    "slug": row.slug,
                    "path": str(row.path),
                    "at": _as_time(found.get("installed_at")),
                    "files": string_list(found.get("files")),
                    "digest": str(found.get("digest") or ""),
                    "enabled": bool(row.enabled),
                    "provenance": bool(found),
                    "changed": False,
                    "connected": False,
                    "words": (
                        f"已装：{row.slug}"
                        + ("" if row.enabled else "（关着）")
                        + ("" if found else "（没留下来历记录）")
                    ),
                }
            return blank
        name = self._server_key(item)
        if not name:
            return blank
        if look is None and table.store_error:
            return {**blank, "words": f"读不了那份登记：{table.store_error}"}
        stored = table.servers.get(name)
        if stored is None:
            return blank
        changed = _row_differs(item, stored)
        return {
            "yes": True,
            "kind": KIND_MCP,
            "name": name,
            "slug": name,
            "path": str(self._path()),
            "at": 0.0,
            "files": [],
            "digest": "",
            "enabled": bool(stored.get("enabled", True)),
            "provenance": True,
            "changed": changed,
            "connected": self._is_connected(name),
            "words": f"已登记：{name}" + ("" if stored.get("enabled", True) else "（停用）"),
        }

    def _is_connected(self, name: str) -> bool:
        for row in mcp_rows(self._mcp_status()):
            if str(row.get("name") or "") == name:
                return bool(row.get("connected"))
        return False

    def _install_verdict(
        self,
        item: CatalogItem,
        installed: Mapping[str, object],
        look: _Look | None = None,
    ) -> dict[str, object]:
        if bool(installed["yes"]):
            return {"can": False, "words": "这一条已经装了，不用重复装"}
        present = list((look or _Look(self)).runtime)
        gaps = [str(one) for one in item.needs if str(one) not in present]
        if gaps:
            return {
                "can": False,
                "words": "这台机器上缺 " + "、".join(gaps) + "，装上了也起不来。",
            }
        return {"can": True, "words": "可以装（装之前还会把要跑的那一条摊给你看）"}

    def _update_verdict(
        self, item: CatalogItem, installed: Mapping[str, object]
    ) -> dict[str, object]:
        """能不能更新、为什么不能。**这一句必须说实话**，不能给一颗点了什么也不会变的按钮。"""
        if not bool(installed["yes"]):
            return {"can": False, "words": "还没装，谈不上更新"}
        if item.kind == KIND_SKILL:
            if not bool(installed["provenance"]):
                return {
                    "can": False,
                    "words": "这一份没留下来历记录（多半是更早那版装的，那时候还没记）—— "
                    "所以判断不了上游有没有出新内容。要换新内容：先删掉这一份，再装一次。",
                }
            return {
                "can": True,
                "words": "按盘上那份来历重新取一次，比内容决定要不要动（比的是文件，不是版本号）",
            }
        if bool(installed["changed"]):
            return {
                "can": True,
                "words": "清单上这一条和盘上登记的不一样了（命令行 / 地址 / 要的环境变量），"
                "重写一次登记并当场连一次",
            }
        spec = item.install if isinstance(item.install, Mapping) else {}
        launcher = str(spec.get("command") or "").strip().lower()
        if launcher in RUN_LATEST_COMMANDS:
            return {
                "can": False,
                "words": f"这一条每次起进程都是 `{launcher} -y` 现拉最新版，没有「更新」这一步。",
            }
        return {"can": False, "words": "清单上这一条和盘上登记的一模一样，没什么要重写的。"}

    def _remove_verdict(
        self, item: CatalogItem, installed: Mapping[str, object]
    ) -> dict[str, object]:
        if not bool(installed["yes"]):
            return {"can": False, "words": "这条本来就没装，没什么可删"}
        return {"can": True, "words": "删之前会再问一次，且只动它自己那一个目录 / 那一条登记"}

    def _update_server(self, item: CatalogItem) -> InstallResult:
        """MCP 的"更新" = 按清单重写这条登记，**保住它原来的启停**，再当场连一次。"""
        name = self._server_key(item)
        path = path_for(self._dir())
        try:
            was_enabled = bool(entries(load(path)).get(name, {}).get("enabled", True))
        except ServerStoreError as exc:
            return InstallResult(False, KIND_MCP, item.name, reason=f"这条读不回来：{exc}")
        result = install_mcp(item, self._dir(), environ=self._environ, audit=self._audit)
        if not result.ok:
            return result
        try:
            after = load(path)
            if bool(entries(after).get(result.name, {}).get("enabled", True)) != was_enabled:
                save(path, set_entry_enabled(after, result.name, was_enabled))
        except ServerStoreError as exc:
            return InstallResult(
                True,
                KIND_MCP,
                result.name,
                path=str(path),
                reason=f"登记写进去了，但启停状态没保住：{exc}",
            )
        connect = self._connect_right_away(result.name)
        self._note(f"更新 MCP 登记 {result.name}（保住启停={was_enabled}）：{connect}")
        return InstallResult(
            True,
            KIND_MCP,
            result.name,
            path=str(path),
            detail=f"按清单重写了一次；连了一次：{connect}",
        )

    def add_http_server(self, name: str, url: str) -> dict[str, object]:
        """手工加一条 **http** 的 MCP server（不起进程，风险和 fetch_url 同阶）。

        stdio 不在这里支持：那一类只能从搜索结果装。见模块开头的第 2 条。
        """
        try:
            data = load(path_for(self._dir()))
            merged, stored = upsert_http(data, name, url)
            save(path_for(self._dir()), merged)
        except (ServerStoreError, ValueError) as exc:
            return _refuse(str(exc), self.state())
        self._note(f"手工登记 http MCP：{stored} → {url}")
        snapshot = self.state()
        snapshot["error"] = ""
        return snapshot

    def set_server_enabled(self, name: str, on: object) -> dict[str, object]:
        try:
            path = path_for(self._dir())
            data = load(path)
            if str(name) not in entries(data):
                return _refuse(f"没有这条 MCP server：{name}", self.state())
            save(path, set_entry_enabled(data, str(name), bool(on)))
        except ServerStoreError as exc:
            return _refuse(str(exc), self.state())
        self._note(f"{'启用' if on else '停用'} MCP server {name}")
        return self.state()

    def set_layer_enabled(self, on: object) -> dict[str, object]:
        """那一大颗「允许她运行外部扩展程序」。

        它写的是扩展自己的文件，不是 `config.yaml`：配置里那个 `mcp.enabled` 仍是
        "启动时连不连"的老含义，装配点把两边取**或**（见 `servers.merge`）。
        """
        try:
            path = path_for(self._dir())
            save(path, set_layer_enabled(load(path), bool(on)))
        except ServerStoreError as exc:
            return _refuse(str(exc), self.state())
        self._note(f"扩展运行{'已允许' if on else '已禁止'}（这一层总开关）")
        return self.state()

    def remove_server(self, name: str, *, confirmed: object) -> dict[str, object]:
        if confirmed is not True:
            return _refuse("删除 MCP server 需要明确的确认（confirmed）", self.state())
        try:
            path = path_for(self._dir())
            data = load(path)
            merged, existed = remove(data, str(name))
            if not existed:
                return _refuse(f"没有这条 MCP server：{name}", self.state())
            save(path, merged)
        except ServerStoreError as exc:
            return _refuse(str(exc), self.state())
        self._note(f"删掉 MCP server 登记 {name}（配置记录，不动盘上任何别的东西）")
        return self.state()

    # -- 内部 --------------------------------------------------------------

    def _connect_right_away(self, name: str) -> str:
        """装完就地连一次，把结果换成一句能上屏的话（永远非空）。

        这一步只负责"装完立刻能用"，不负责"装完立刻证明它能用"：连不上也**不**回滚登记 ——
        第一次 npx 拉包可能要几分钟，那笔下载没理由让用户丢掉刚装的东西。失败原文留在
        页面那一行上，「重连」那颗按钮也还在。
        """
        provider = self._mcp_connect
        if provider is None:
            return "这一层没接上 MCP 服务，重启小夜之后才会去连它"
        try:
            said = provider(name)
        except Exception as exc:  # 子进程那一路什么都会抛，抛出来也要看得见
            return f"连这一次出错了：{type(exc).__name__}: {exc}"[:180]
        return said or "连上了，它的工具已经进了她的工具箱"

    def _dir(self) -> Path:
        return Path(self._dir_provider())

    @staticmethod
    def _no_mcp() -> dict[str, object]:
        """没接 MCP 那一层时的整份形状。少一个键，界面那一格就不画。"""
        return {
            "enabled": False,
            "running": False,
            "servers": 0,
            "connected": 0,
            "tools": 0,
            "statuses": [],
            "errors": {},
            "error": "",
        }

    def _mcp_status(self) -> dict[str, object]:
        provider = self._mcp_status_provider
        if provider is None:
            return {
                "enabled": False,
                "running": False,
                "servers": 0,
                "connected": 0,
                "tools": 0,
                "statuses": [],
                "errors": {},
                "error": "这一层没接上 MCP 服务",
            }
        try:
            raw = dict(provider() or {})
        except Exception as exc:
            fallback = self._no_mcp()
            fallback["error"] = f"状态读不了：{exc}"
            return fallback
        base = self._no_mcp()
        base.update(raw)
        base.setdefault("error", "")
        return base

    def _last_search_dict(self) -> dict[str, object]:
        if self._last is None:
            return {"query": "", "items": [], "sources": [], "failures": []}
        return self._last.to_dict()

    def _trim_cache(self) -> None:
        if len(self._by_id) <= 400:
            return
        self._by_id = dict(list(self._by_id.items())[-200:])

    def _sync_tools(self) -> None:
        provider = self._registry_provider
        if provider is None:
            return
        registry = provider()
        self._unregister(registry)
        registry.register(_SPEC, self._list_handler())

    def _unregister(self, registry: ToolRegistry | None = None) -> None:
        target = (
            registry
            if registry is not None
            else (self._registry_provider() if self._registry_provider is not None else None)
        )
        if target is None:
            return
        try:
            target.unregister_source(EXTENSION_SOURCE)
        except Exception:
            logger.warning("注销扩展工具失败", exc_info=True)

    def _list_handler(self) -> Callable[[Mapping[str, object]], str]:
        def handler(arguments: Mapping[str, object]) -> str:
            rows = self.state()
            skills = cast("list[dict[str, object]]", rows["skills"])
            servers = cast("list[dict[str, object]]", rows["servers"])
            mcp = cast("dict[str, object]", rows["mcp"])
            runtime = cast("RuntimeState", rows["runtime"])
            search = cast("dict[str, list[str]]", rows["search"])
            errors = mcp.get("errors") or {}
            lines = [
                f"技能 {len(skills)} 份（装在她笔记同级的 扩展/skills 里）：",
                "；".join(
                    f"{row['name']}（{'启用' if row['enabled'] else '关着'}"
                    f"{'，读不了：' + str(row['error']) if row['error'] else ''}）"
                    for row in skills
                )
                or "（一份都没装）",
                f"登记的 MCP server {len(servers)} 条：",
                "；".join(
                    f"{row['name']}（{row['transport']}，{'启用' if row['enabled'] else '关着'}）"
                    for row in servers
                )
                or "（一条都没有）",
                (
                    f"连接情况：{json.dumps(errors, ensure_ascii=False)}"
                    if errors
                    else "MCP 现在没有报错。"
                ),
                f"本机运行时：有 {','.join(runtime['present'])}"
                f"｜缺 {','.join(runtime['missing']) or '没有'}",
            ]
            if str(arguments.get("detail") or "").strip().lower() == "any":
                lines.append("最近一次搜索的失败原因：" + ("；".join(search["failures"]) or "没有"))
            return "\n".join(lines)

        return handler

    def _audit_file(self) -> Path | None:
        provider = self._audit_file_provider
        return Path(provider()) if provider is not None else None

    def _note(self, text: str) -> None:
        """两行制的第一行之外，这里只记**结果**那一行；失败也要记，见模块外注释。"""
        path = self._audit_file()
        if path is None:
            return
        row = {"at": round(time.time(), 3), "event": text}
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding=DEFAULT_ENCODING, newline="\n") as handle:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
        except OSError as exc:
            logger.warning("扩展审计写不进去：%s", exc)

    def _audit(self, text: str) -> None:
        self._note(text)


def upsert_http(data: Mapping[str, object], name: str, url: str) -> tuple[dict[str, object], str]:
    from jarvis.core.exceptions import ToolExecutionError
    from jarvis.extensions.servers import upsert
    from jarvis.tools.builtins.web_tools import validate_url

    try:
        # 只放 http(s)、拒内网和回环 —— 复用 fetch_url 那一道闸，不在这里再发明一遍。
        # 复用不是偷懒：两处校验迟早会漂，而漂了的那一处看起来完全正常。
        checked = validate_url(url)
    except ToolExecutionError as exc:
        raise ServerStoreError(f"这条地址过不了原来的那道闸：{exc}") from exc
    if not checked.startswith(("http://", "https://")):
        raise ServerStoreError("这条地址必须是 http(s) 的")
    return upsert(
        data,
        {
            "name": name,
            "transport": "http",
            "url": checked,
            "command": "",
            "args": [],
            "env_from": [],
            "enabled": True,
            "source": "manual",
            "needs": [],
            "note": "界面上手工加的（只许 http，不起本机进程）",
        },
    )


def _refuse(reason: str, snapshot: dict[str, object]) -> dict[str, object]:
    logger.info("扩展操作被拒绝：%s", reason)
    out = dict(snapshot)
    out["error"] = reason
    return out


def _row_differs(item: CatalogItem, stored: Mapping[str, object]) -> bool:
    """清单上这一条，和盘上那条登记，还是同一份吗。

    只比**会跑起来的那几样**（命令行、参数、地址、要的环境变量）：说明文字改了不算更新，
    那会让用户以为自己的 server 被动过。
    """
    spec = item.install if isinstance(item.install, Mapping) else {}

    def text(row: Mapping[str, object], key: str) -> str:
        return str(row.get(key) or "")

    def words(row: Mapping[str, object], key: str) -> list[str]:
        return [str(one) for one in string_list(row.get(key))]

    return (
        text(spec, "command") != text(stored, "command")
        or words(spec, "args") != words(stored, "args")
        or text(spec, "url") != text(stored, "url")
        or sorted(words(spec, "env_from")) != sorted(words(stored, "env_from"))
    )


def _as_time(value: object) -> float:
    """来历记录里那个 `installed_at`：不是数字就当没记过，不拿 0 装"很久以前"。"""
    return float(value) if isinstance(value, (int, float)) else 0.0


def mcp_rows(status: Mapping[str, object]) -> list[Mapping[str, object]]:
    """逐台状态。现在后端回的是 `statuses`，老形状里 `servers` 可能给成数组 —— 两种都认。"""
    rows: list[Mapping[str, object]] = []
    for key in ("statuses", "servers"):
        value = status.get(key)
        if isinstance(value, list):
            rows.extend(row for row in value if isinstance(row, Mapping))
    return rows


def empty_detail(item_id: str = "", reason: str = "") -> dict[str, object]:
    """详情页读不到时的**完整形状**。

    桥面没有扩展层、id 不认识、后端抛了异常 —— 三种都要回同一份骨架 + 一句原因。
    少一个键，界面那一块就不画，用户于是永远看不见"为什么没有"。
    """
    return {
        "found": False,
        "id": item_id,
        "item": {},
        "installed": {
            "yes": False,
            "kind": "",
            "name": "",
            "slug": "",
            "path": "",
            "at": 0.0,
            "files": [],
            "digest": "",
            "enabled": False,
            "provenance": False,
            # 和真卡同一份骨架：少一格的表现是界面那一块不画（这条被本轮新加的判据抓到过）
            "changed": False,
            "connected": False,
            "words": "",
        },
        "install": {"can": False, "words": reason},
        "update": {"can": False, "words": reason},
        "remove": {"can": False, "words": reason},
        "runtime": {"present": [], "paths": {}, "labels": {}, "missing": [], "error": ""},
        "log": [],
        "directory": "",
        "servers_file": "",
        "error": reason,
    }


def empty_recommended(reason: str) -> dict[str, object]:
    """推荐那一格读不到时的形状。**空列表必须带着一句原因回去**，否则界面只能说"没有推荐"。"""
    return {"items": [], "error": reason, "at": 0.0, "where": ""}


__all__ = [
    "EXTENSION_SOURCE",
    "LIST_TOOL",
    "ExtensionsService",
    "empty_detail",
    "empty_recommended",
]
