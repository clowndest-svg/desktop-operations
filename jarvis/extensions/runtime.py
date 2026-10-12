"""本机运行时检测：装一个 MCP server 之前，先确认那条命令在这台机器上**起得来**。

"有"的定义是"我们能把它 Popen 起来"，不是"PATH 里有这么个文件" —— 这个区别本轮真装真连
才暴露：Windows 上 `npx` 是 `.cmd` 壳，`shutil.which` 报有、`Popen` 报找不到文件，
于是市场里 npm 那一整类每条都能"装成功、连不上"。检测和启动用的是同一个解析器
（`jarvis/core/commands.py`），两处各算一遍迟早会不一致。

也**不跑 `--version`**：跑一次子进程在 Windows 上是 50~300 ms，而"能起"和"能干活"本来就是
两件事，界面上要分开说。真起不来时连接那一步会给原文，这一层只负责别让人去点一个
注定装不上的按钮。

把"有"定成"能起"而不是"PATH 里有"，守的是这条老规矩：注册一条永远连不上的路就是假能力 ——
页面上那条亮着、点下去必失败，比根本没有这一条更坏。
"""

from __future__ import annotations

import logging
import re
import sys
from pathlib import Path
from typing import Final, TypedDict

from jarvis.core.commands import can_launch

logger = logging.getLogger(__name__)

REQUIRED_LABELS: Final = {
    "node": "Node.js（npm 那类 MCP server 要用）",
    "npx": "npx（随 Node 一起来）",
    "python": "Python（自研的 stdio 服务用当前解释器）",
    "uvx": "uvx（Python 那类 MCP server 的免安装跑法）",
    "deno": "Deno（少数 server 用 deno run）",
}

_VERSION = re.compile(r"(\d+(?:\.\d+){0,2})")


class RuntimeState(TypedDict):
    """`detect()` 那一格的形状。

    写死它是因为每个消费者都要拿它做一次"能不能装"的判断：留给调用方各自 cast，就会有一处
    把 `present` 当列表、另一处当集合，两边都"对"，行为却不一样。
    """

    present: list[str]
    paths: dict[str, str]
    labels: dict[str, str]
    missing: list[str]
    error: str


def detect(extra: tuple[str, ...] = ()) -> RuntimeState:
    """每台机器现查一次，不做跨进程缓存。

    缓存"装没装 Node"是有意义的 —— 用户可能刚装完 Node 就回来点这个页面。
    """
    names = list(REQUIRED_LABELS) + [str(item) for item in extra if str(item).strip()]
    found: dict[str, str] = {}
    for name in names:
        # 和真要起它的那一段同一个答案：`shutil.which("npx")` 在 Windows 上给出
        # `npx.cmd`，可 `Popen` 拿裸名字起不来 —— 只报"PATH 里有"就会变成
        # "检测说有、连接说没装"，市场里每条 npm 都装得成、连不上（本轮实测）。
        where = can_launch(name)
        if where:
            found[name] = where
    # 当前解释器一定在：打包版里 `sys.executable` 就是小夜自己那个 exe。
    # 这一条对安装器有用（装一个 Python 写的 stdio server 时，command 就是它，不需要额外运行时），
    # 但**不能写成 `python`** —— 那是用户的 Python，不是随包那个。
    found["this-runtime"] = str(sys.executable or Path(sys.argv[0]).resolve())
    return {
        "present": sorted(found),
        "paths": found,
        "labels": REQUIRED_LABELS,
        "missing": sorted(
            key for key in REQUIRED_LABELS if key not in found and key != "this-runtime"
        ),
        "error": "",
    }


def missing_for(needs: tuple[str, ...], present: tuple[str, ...] | list[str]) -> list[str]:
    """这个条目要的东西里，本机缺哪几样。空 = 可以装。"""
    have = set(present)
    return [need for need in needs if need not in have]


def version_of(text: str) -> str:
    """从一段 `--version` 输出里取第一个版本号形状。

    留这个是给"连接失败时把版本带上"那条路径用的：同一句 npx 报错，
    Node 18 和 Node 22 的解决办法完全不同。
    """
    match = _VERSION.search(str(text or ""))
    return match.group(1) if match else ""


__all__ = ["REQUIRED_LABELS", "RuntimeState", "detect", "missing_for", "version_of"]
