"""`数据/扩展/servers.json` —— 市场装进来的 MCP server 归这里，`config.yaml` 一个字都不改。

为什么不写 config.yaml：那是人手维护的文件，里面有注释；用 `yaml.safe_dump` 写回去会把
注释和排版一起抹掉，用户下一次打开看见的是另一份东西。**扩展拥有的数据放在扩展自己的文件里**，
装配点在内存里合并，两个真相各有各的写法（合并规则见 :func:`merge`）。

`env` 这一栏的形状是本轮定的，值得写明：文件里存的是一串**环境变量名**（`env_from`），
不是 `name=value`。理由是既有的红线「密钥只进环境变量」——把 key 写进一个 JSON，
它就变成了一个躺在数据目录里、会被打包、被同步、被截图的 secret。名字在合并时从进程环境里取值，
取不到的那条会连不上，而**为什么连不上会原样出现在页面上**（不是静默少一个变量）。
"""

from __future__ import annotations

import json
import logging
import os
import re
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import Final

from jarvis.config.schema import McpSection, McpServerSection
from jarvis.core.exceptions import ConfigurationError
from jarvis.extensions.types import string_list

logger = logging.getLogger(__name__)

FILENAME: Final = "servers.json"
VALID_TRANSPORTS: Final = ("stdio", "http")
_KEY = re.compile(r"[a-z0-9][a-z0-9_-]{0,39}")
"""server 的键：会变成 `mcp__<server>__<tool>` 的一部分，所以只许小写、数字、下划线、连字符。"""


class ServerStoreError(ValueError):
    """这份文件读不了 / 这条写不了。原因要能直接上屏。"""


def path_for(extensions_dir: Path) -> Path:
    return Path(extensions_dir) / FILENAME


def load(path: Path) -> dict[str, object]:
    """读整个文件；不存在 = 一份空结构（新机器本来就没装过东西）。

    坏 JSON **不当"没有装过"**：那样用户看到的是一句"你还没有装任何 MCP server"，
    而盘上确实有一份没被采纳的东西。这里把原文带着抛出去，由调用方显示。
    """
    if not path.is_file():
        return {"version": 1, "enabled": False, "servers": {}}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ServerStoreError(f"读不到 {FILENAME}：{exc}") from exc
    except ValueError as exc:
        raise ServerStoreError(f"{FILENAME} 不是合法 JSON：{exc}") from exc
    if not isinstance(raw, dict) or not isinstance(raw.get("servers"), dict):
        raise ServerStoreError(f'{FILENAME} 形状不对：要 {{"servers": {{…}}}}')
    return {"version": 1, **raw}


def entries(data: Mapping[str, object]) -> dict[str, dict[str, object]]:
    raw = data.get("servers")
    if not isinstance(raw, Mapping):
        return {}
    return {str(name): dict(row) for name, row in raw.items() if isinstance(row, Mapping)}


def normalise_key(name: str) -> str:
    """把任何"名字"压成一个合法键，压不出来就直接拒绝。

    必须拒绝而不是替换字符：`mcp__<server>__<tool>` 是模型看到的函数名，
    两个不同的 server 被消毒成同一个键，工具表就自己撞自己了。
    """
    key = str(name or "").strip().lower().replace(" ", "-")
    key = re.sub(r"[^a-z0-9_-]+", "-", key).strip("-")[:40]
    if not _KEY.fullmatch(key):
        raise ServerStoreError(
            f"这个名字当不了 server 的键：{name!r}（要 a-z0-9_- 开头是字母数字）"
        )
    return key


def validate(spec: Mapping[str, object]) -> dict[str, object]:
    """在**写盘之前**验一遍，用配置层同一套校验（不在这儿再发明一套规则）。

    `McpServerSection.from_mapping` 会拒绝未知键、stdio 缺 command、http 缺 url，
    以及 transport 不在白名单里 —— 这些正是"装完连不上，而界面说不出为什么"的来源。
    """
    name = normalise_key(str(spec.get("name") or spec.get("id") or ""))
    transport = str(spec.get("transport") or "stdio").strip().lower()
    if transport not in VALID_TRANSPORTS:
        raise ServerStoreError(f"传输方式只认 {'/'.join(VALID_TRANSPORTS)}，收到 {transport!r}")
    body: dict[str, object] = {
        "transport": transport,
        "command": str(spec.get("command") or "").strip(),
        "args": string_list(spec.get("args")),
        "url": str(spec.get("url") or "").strip(),
        "env": {},
        "enabled": bool(spec.get("enabled", True)),
    }
    try:
        McpServerSection.from_mapping(name, body)
    except ConfigurationError as exc:
        raise ServerStoreError(str(exc)) from exc
    return {
        "name": name,
        "transport": transport,
        "command": body["command"],
        "args": body["args"],
        "url": body["url"],
        "env_from": sorted({one for one in string_list(spec.get("env_from")) if one.strip()}),
        "enabled": bool(body["enabled"]),
        "source": str(spec.get("source") or ""),
        "homepage": str(spec.get("homepage") or ""),
        "needs": sorted({one for one in string_list(spec.get("needs")) if one.strip()}),
        "note": str(spec.get("note") or "")[:200],
    }


def upsert(data: Mapping[str, object], spec: Mapping[str, object]) -> tuple[dict[str, object], str]:
    """返回（新的一份, 这个键）。同键覆盖 —— 重装一个已经装过的 server 是正常动作，
    但**旧的 env_from 不会静默留着**：整条替换，不留"上次那个 key 还在文件里"这种事。
    """
    row = validate(spec)
    name = str(row["name"])
    servers = {key: dict(value) for key, value in entries(data).items()}
    servers[name] = {key: value for key, value in row.items() if key != "name"}
    return {"version": 1, "enabled": bool(data.get("enabled")) or True, "servers": servers}, name


def remove(data: Mapping[str, object], name: str) -> tuple[dict[str, object], bool]:
    key = str(name or "").strip()
    servers = {k: dict(v) for k, v in entries(data).items()}
    existed = servers.pop(key, None) is not None
    return (
        {"version": 1, "enabled": bool(data.get("enabled")), "servers": servers},
        existed,
    )


def set_entry_enabled(data: Mapping[str, object], name: str, on: object) -> dict[str, object]:
    servers = {k: dict(v) for k, v in entries(data).items()}
    if name in servers:
        servers[name]["enabled"] = bool(on)
    return {"version": 1, "enabled": bool(data.get("enabled")), "servers": servers}


def set_layer_enabled(data: Mapping[str, object], on: object) -> dict[str, object]:
    servers = {key: dict(value) for key, value in entries(data).items()}
    return {"version": 1, "enabled": bool(on), "servers": servers}


def save(path: Path, data: Mapping[str, object]) -> None:
    """原子写：先落 `.partial` 再 `os.replace`。

    理由和被打包的那些 zip 一样：中途掉电留下一个截断的 JSON，下次启动这一层就整块读不了，
    而用户看见的是"我装过的东西都不见了"。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".json.partial")
    temp.write_text(json.dumps(dict(data), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temp, path)


def resolve_env(
    names: tuple[str, ...] | list[str], environ: Mapping[str, str] | None = None
) -> tuple[dict[str, str], list[str]]:
    """把"要哪些环境变量"翻成 (能给的, 缺的)。

    缺的那一份**必须带回来说话**：少了 API key 的 server 连不上，界面上只写"连不上"的话，
    用户会去查网络。
    """
    source = environ if environ is not None else os.environ
    given: dict[str, str] = {}
    missing: list[str] = []
    for name in names:
        key = str(name).strip()
        if not key:
            continue
        if key in source:
            given[key] = str(source[key])
        else:
            missing.append(key)
    return given, missing


def to_section(
    name: str, row: Mapping[str, object], environ: Mapping[str, str] | None = None
) -> tuple[McpServerSection, list[str]]:
    body: dict[str, object] = {
        "transport": str(row.get("transport") or "stdio"),
        "command": str(row.get("command") or ""),
        "args": string_list(row.get("args")),
        "url": str(row.get("url") or ""),
        "enabled": bool(row.get("enabled", True)),
    }
    given, missing = resolve_env(tuple(string_list(row.get("env_from"))), environ)
    body["env"] = given
    return McpServerSection.from_mapping(name, body), missing


def merge(
    section: McpSection,
    path: Path,
    environ: Mapping[str, str] | None = None,
) -> tuple[McpSection, dict[str, list[str]], str]:
    """把扩展文件里的 server 合并进配置那一份。

    优先级是 **config.yaml 赢**：人自己写下去的那一条比"市场里装的那一条"更有理解释这台机器。
    返回 (合并后的段, 每条缺哪些环境变量, 读这份文件时的错)。

    `enabled` 取两边之或 —— 装了并启用了一条 server，就是打开了这一层；配置里那个总开关
    仍然是"要不要在启动时连接"的老含义，不改它的语义。
    """
    try:
        data = load(path)
    except ServerStoreError as exc:
        return section, {}, str(exc)
    extra: dict[str, McpServerSection] = {}
    missing: dict[str, list[str]] = {}
    for name, row in entries(data).items():
        try:
            built, absent = to_section(name, row, environ)
        except ConfigurationError as exc:
            missing[name] = [f"这条的形状不对：{exc}"]
            continue
        extra[name] = built
        if absent:
            missing[name] = absent
    servers = {**extra, **dict(section.servers)}
    return (
        replace(
            section, enabled=bool(section.enabled) or bool(data.get("enabled")), servers=servers
        ),
        missing,
        "",
    )


__all__ = [
    "FILENAME",
    "ServerStoreError",
    "entries",
    "load",
    "merge",
    "normalise_key",
    "path_for",
    "remove",
    "resolve_env",
    "save",
    "set_entry_enabled",
    "set_layer_enabled",
    "to_section",
    "upsert",
    "validate",
]
