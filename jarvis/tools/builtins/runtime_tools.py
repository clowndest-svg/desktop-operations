"""Read-only probe of the development runtimes installed on this machine.

Why this is a tool rather than a ``run_powershell`` call: asked 「检查本机 JDK 版本」
with only ``system_report`` on hand, a local model answered that it could check CPU,
memory and disk but not Java -- which was the correct reading of the tools it was
given. The version of an installed runtime is a fixed, non-model-authored question,
so it gets a fixed, non-model-authored command list instead of a shell.

Nothing here takes a command, a path, or an argument from the model. The six rows
below are the whole surface.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from collections.abc import Mapping
from typing import Final

from jarvis.core.constants import DEFAULT_ENCODING
from jarvis.tools.types import RiskLevel, ToolHandler, ToolSpec

logger = logging.getLogger("jarvis.tools.builtins.runtime_tools")

_NAME: Final[str] = "runtime_versions"

#: ``(label, executable, argv-tail)``. The whole allow-list -- the model picks
#: nothing here. ``java -version`` (not ``--version``) because it is the only form
#: that answers on a JDK 8 install too, and it writes to stderr rather than stdout.
COMMANDS: Final[tuple[tuple[str, str, tuple[str, ...]], ...]] = (
    ("Java/JDK", "java", ("-version",)),
    ("javac", "javac", ("-version",)),
    ("Python", "python", ("--version",)),
    ("Node.js", "node", ("--version",)),
    ("npm", "npm", ("--version",)),
    ("Git", "git", ("--version",)),
)

_TIMEOUT_SECONDS: Final[float] = 5.0
"""Six probes at most; one that has not answered in five seconds is broken, not slow."""

_MAX_LINE_CHARS: Final[int] = 120
"""``java -version`` prints three lines of build banner; the first is the answer."""

_SPEC = ToolSpec(
    name=_NAME,
    description=(
        "查这台电脑上装了哪些开发环境、各是什么版本：Java/JDK、javac、Python、Node.js、"
        "npm、Git。只读，不装也不改任何东西。用户问「我电脑上有没有装 X」「X 是什么版本」"
        "「检查本机 JDK」时用这一条，不要改用 PowerShell 去翻目录 —— 一次调用就把这六个"
        "都答完了。没装的会明确写「未安装」。"
    ),
    parameters={"type": "object", "properties": {}},
    risk=RiskLevel.SAFE,
)


def _first_line(raw: bytes) -> str:
    """The one line that names the version, cleaned up.

    Both streams are merged by the caller because ``java -version`` and
    ``javac -version`` print to stderr while everything else prints to stdout;
    picking a stream here would mean guessing which tool the model just asked.
    """
    text = raw.decode(DEFAULT_ENCODING, errors="replace")
    for line in text.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped[:_MAX_LINE_CHARS]
    return ""


def _probe(label: str, exe: str, args: tuple[str, ...]) -> str:
    """One row of the report, as a sentence the model can quote verbatim."""
    path = shutil.which(exe)
    if path is None:
        return f"{label}：未安装（PATH 里找不到 {exe}）"
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    try:
        completed = subprocess.run(
            [path, *args],
            capture_output=True,
            timeout=_TIMEOUT_SECONDS,
            shell=False,
            creationflags=flags,
        )
    except subprocess.TimeoutExpired:
        return f"{label}：{exe}  {_TIMEOUT_SECONDS:.0f} 秒内没回答"
    except OSError as exc:
        return f"{label}：查不了（{type(exc).__name__}: {exc}）"
    version = _first_line(completed.stdout + completed.stderr)
    if not version:
        return f"{label}：{path} 存在，但它没吐出版本号"
    return f"{label}：{version}"


def _runtime_versions(arguments: Mapping[str, object]) -> str:
    """Every runtime on the fixed list, one line each.

    ``arguments`` is ignored: the tool takes nothing. Kept in the signature because
    that is the handler contract.
    """
    rows = [_probe(label, exe, args) for label, exe, args in COMMANDS]
    return "本机开发环境（只读探测，按 PATH 查找）：\n" + "\n".join(rows)


def build() -> list[tuple[ToolSpec, ToolHandler]]:
    """Return the tools this module contributes."""
    return [(_SPEC, _runtime_versions)]


__all__ = ["COMMANDS", "build"]
