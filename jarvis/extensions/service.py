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
from jarvis.extensions.installer import install_mcp, install_skill
from jarvis.extensions.runtime import RuntimeState, detect
from jarvis.extensions.servers import (
    ServerStoreError,
    entries,
    load,
    path_for,
    remove,
    save,
    set_entry_enabled,
    set_layer_enabled,
)
from jarvis.extensions.sources import SearchCache, search_all
from jarvis.extensions.types import (
    KIND_MCP,
    KIND_SKILL,
    CatalogItem,
    SearchResult,
    string_list,
)
from jarvis.skills.service import SkillService
from jarvis.tools import RiskLevel, ToolRegistry, ToolSpec
from jarvis.tools.types import object_schema, string_property

logger = logging.getLogger(__name__)

EXTENSION_SOURCE: Final = "extension:market"
LIST_TOOL: Final = "extensions_list"
CACHE_SECONDS: Final = 900.0
"""搜索结果留 15 分钟：装的那一下要能从缓存里按 id 取回原始 spec。"""

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


__all__ = ["EXTENSION_SOURCE", "LIST_TOOL", "ExtensionsService"]
