"""把"这条命令到底起不起得来"放在 core 里，因为两层都要同一个答案。

`jarvis/mcp/transport.py` 要拿它起进程，`jarvis/extensions/runtime.py` 要拿它告诉用户
"这台机器缺 npx，所以 npm 那类装不了"。**两边各算一遍的话，迟早一边说有一边起不来** ——
本轮真装真连时撞的就是这个：`shutil.which("npx")` 给出 `npx.cmd`（检测说"有"），
`Popen(["npx", ...])` 报 WinError 2（连接说"找不到文件"），于是市场里每条 npm 服务
都能"装成功、连不上"。

规则本身也不新：Windows 的 `CreateProcess` 不给裸名字套 `PATHEXT`（它会去找 `npx.exe`），
而 `.cmd` / `.bat` 又不是可执行映像，必须经 `cmd.exe /c`。

放在 `core`（L1）而不是留在 `mcp` 里，是为了不逼着扩展层去依赖 MCP 层：分层表里
`extensions` 只许 `core/config/tools/skills`，那条线是"市场不许假装自己会连服务"。
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Iterable
from pathlib import Path
from typing import Final

SHELL_ONLY_SUFFIXES: Final[tuple[str, ...]] = (".cmd", ".bat")
"""只能经 `cmd.exe /c` 起来的脚本壳。Windows 上 `npx` / `npm` / 部分 `uvx` 安装就是这个。"""


SHELL_METACHARACTERS: Final = frozenset("&|<>^%")
"""经 `cmd.exe` 时会被再解释一遍一次的字符。

`Popen([...], shell=False)` 平时不解释这些，可一旦前面挂了 `cmd /c` 就会 —— 那时候一个
精心构造的包名或路径能把"起一个 MCP 服务"变成"执行任意一句"。所以这一类参数直接拒，
并且拒的那句要说明是这条规则拦的，不是"你没装 Node"。
"""

CommandPrefix = tuple[str, tuple[str, ...]]
"""(真正要 Popen 的可执行文件, 挂在它前面的参数)。"""


def has_shell_metacharacters(args: object) -> bool:
    if not isinstance(args, Iterable):
        return False
    return any(char in SHELL_METACHARACTERS for arg in args for char in str(arg))


def resolve_command(command: str) -> CommandPrefix:
    """把配置里那一个词，翻成真能 `Popen` 起来的 argv 前缀。

    找不到就**原样带回**，让 `Popen` 报它自己的错（那句仍然比我们自己编一句"没装 Node"
    准确 —— 可能是权限、可能是被杀软拦了）。绝对路径不去 `which`：那会拿 PATH 拼出一个
    同名但无关的文件来。
    """
    raw = str(command or "").strip()
    if not raw:
        return "", ()
    if Path(raw).is_absolute():
        candidate = raw if Path(raw).is_file() else ""
        if not candidate:
            return raw, ()
    else:
        # Windows 的 `shutil.which` 自己会套 PATHEXT（实测给出 `npx.CMD`），不再自己补一遍：
        # 补的那一段永远走不到，只会多出一条杀不死的变异。本轮真正的 bug 不是"找不到 npx"，
        # 是"找到了 .cmd 却直接 Popen"—— 那是下面那一步的事。
        candidate = shutil.which(raw) or ""
    if not candidate:
        return raw, ()
    if Path(candidate).suffix.lower() in SHELL_ONLY_SUFFIXES:
        shell = str(os.environ.get("COMSPEC") or "cmd.exe")
        return shell, ("/c", candidate)
    return candidate, ()


def can_launch(command: str) -> str:
    """能起 → 返回实际会用到的可执行文件路径（经 cmd 时带上标记）；不能起 → 空串。

    给运行时检测用：它报的必须是"我们起得来"，不是"PATH 里有个同名文件"。
    """
    executable, prefix = resolve_command(command)
    if not executable:
        return ""
    if not Path(executable).is_file() and not shutil.which(executable):
        return ""
    if prefix:
        # 报给用户看的是**那个脚本本身**（`npx.cmd` 在哪），不是"我们借道 cmd.exe"这件事的
        # 那半边 —— 界面上一句 `C:\WINDOWS\system32\cmd.exe（经 cmd /c）` 等于没说什么。
        return f"{prefix[-1]}（经 cmd /c）"
    return executable


__all__ = [
    "SHELL_METACHARACTERS",
    "SHELL_ONLY_SUFFIXES",
    "CommandPrefix",
    "can_launch",
    "has_shell_metacharacters",
    "resolve_command",
]
