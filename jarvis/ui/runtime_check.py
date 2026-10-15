"""启动之前先看一眼这台机器有没有 WebView2。

**为什么这一层存在**：pywebview 在 import 平台后端时按注册表挑实现，找不到 WebView2
Runtime 就**静默退回 MSHTML（IE 内核）**。表现不是报错，是"窗口开起来了，里面是白的"
—— 而一个 98 MB 的 exe 双击之后白屏，买家只会得出一个结论：这软件是坏的。
这一层把那条静默分支换成一句人话 + 一个能照做的下一步。

只读注册表，不装东西、不联网下载运行时：那是操作者（或他公司的 IT）的决定，
不是一个聊天助手该替机器做的事。
"""

from __future__ import annotations

import contextlib
import ctypes
import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Final

CLIENT_GUIDS: Final[tuple[tuple[str, str], ...]] = (
    ("Microsoft Edge WebView2 Runtime", "{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"),
    # 下面三个不是凑数：pywebview 自己就认这四个（winforms.py:100-112）。只查正式版的
    # 话，装了 Edge Beta/Dev/Canary 附带运行时的开发机会被误报成"缺运行时"而开不了窗。
    ("Microsoft Edge WebView2 Beta", "{2CD8A64A-F25C-4869-8872-9D1F23B8D8FF}"),
    ("Microsoft Edge WebView2 Developer", "{F59118F0-2367-4D1E-8B39-8C7F8672131F}"),
    ("Microsoft Edge WebView2 Canary", "{2F8E9D56-54F1-4B06-9496-B96F96891698}"),
)

_HIVES: Final[tuple[str, ...]] = ("HKEY_CURRENT_USER", "HKEY_LOCAL_MACHINE")
"""HKCU 那条是"只给当前用户装"的免管理员安装 —— 没有管理员权限的公司机走的就是它。"""


def _key_paths(guid: str) -> tuple[str, ...]:
    return (
        rf"SOFTWARE\Microsoft\EdgeUpdate\Clients\{guid}",
        rf"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{guid}",
    )


_ZERO: Final = ("0", "0.0", "0.0.0", "0.0.0.0", "")
"""`pv=0.0.0.0` 是"键在但运行时没装"的写法，必须当成缺。"""

Reader = Callable[[str, str], str | None]
"""(注册表根, 值名) → 版本号或 None。测试注一个假的进来。"""


def _read_value(root: str, key: str) -> str | None:
    import winreg

    for hive_name in (root,):
        hive = getattr(winreg, hive_name, None)
        if hive is None:
            continue
        with contextlib.suppress(OSError), winreg.OpenKey(hive, key) as handle:
            value, _ = winreg.QueryValueEx(handle, "pv")
            return str(value).strip()
    return None


def webview2_version(read: Reader = _read_value) -> str:
    """这台机器上 WebView2 运行时的版本号，没装返回空串。

    三个位置挨个问，第一个非零的算数。读注册表失败（权限、精简系统上键根本不存在）
    一律按"没装"处理 —— 宁可多提示一次，也不要因为读不动就放过去开一扇白窗。
    """
    if getattr(sys, "frozen", False):
        runtime = Path(sys.executable).resolve().parent / "运行库" / "WebView2"
        if (runtime / "msedgewebview2.exe").is_file():
            os.environ["WEBVIEW2_BROWSER_EXECUTABLE_FOLDER"] = str(runtime)
            return "bundled-fixed"
    for _, guid in CLIENT_GUIDS:
        for key in _key_paths(guid):
            for hive in _HIVES:
                found = read(hive, key)
                if found and found not in _ZERO:
                    return found
    return ""


def missing_message() -> str:
    return (
        "这台电脑上没有 Microsoft Edge WebView2 运行时，界面开不起来。\n\n"
        "小夜的窗口是用 WebView2 画的。缺它的时候 pywebview 不会报错，"
        "会退回系统里那个老的 IE 内核 —— 结果是窗口开起来但里面一片空白，"
        "看上去像软件坏了，所以这一步先拦下来。\n\n"
        "装法（约 2 分钟，一次就好）：\n"
        "  1) 打开 https://developer.microsoft.com/microsoft-edge/webview2/ 下载 "
        '"Evergreen Bootstrapper"；\n'
        '  2) 双击安装（没有管理员权限时选 "Peruser" 那种安装方式也能装）；\n'
        "  3) 重新双击 小夜.exe。\n\n"
        "Win10 1803 之后一般自带；公司机器上被 IT 精简过的才会缺。"
    )


def warn_missing_webview2() -> bool:
    """缺运行时就在屏幕上说一句，返回 True 表示"别起了"。

    打包版是 `console=False` 起的：`print` 和日志买家一个字都看不见，所以这里必须用
    一个真窗口（`MessageBoxW`）。弹窗本身失败（无桌面会话、服务里跑）也不能抛 ——
    那样会把"起不来"变成"崩了"，退回写日志。
    """
    if webview2_version():
        return False
    text = missing_message()
    import logging

    logging.getLogger("jarvis.ui").warning("WebView2 runtime missing: %s", text.splitlines()[0])
    with contextlib.suppress(Exception):
        user32 = ctypes.windll.user32
        user32.MessageBoxW(0, text, "小夜起不来：缺 WebView2 运行时", 0x00000010 | 0x00040000)
    return True
