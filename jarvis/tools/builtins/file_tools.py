"""File tools.

Every path argument goes through :meth:`jarvis.tools.policy.ToolPolicy.resolve_path`
before anything touches the disk, so ``..`` and symlinks cannot walk out of the
configured roots. The split between ``write_file`` (CAUTION, needs
``tools.allow_write``) and ``delete_path`` (DANGEROUS, needs that *and* an
explicit confirmation) is deliberate: updating a note and destroying a folder
are not the same request, and only one of them should be answerable by a model
on its own.
"""

from __future__ import annotations

import datetime
import fnmatch
import logging
from collections.abc import Mapping
from pathlib import Path
from typing import Final

from jarvis.core.exceptions import ToolExecutionError
from jarvis.tools.policy import ToolPolicy
from jarvis.tools.types import (
    PERMISSION_WRITE,
    RiskLevel,
    ToolHandler,
    ToolSpec,
    boolean_property,
    integer_property,
    object_schema,
    string_property,
)

logger = logging.getLogger("jarvis.tools.builtins.file_tools")

MAX_LIST_ENTRIES: Final[int] = 500
"""How many directory entries one listing may return.

A structural bound rather than a tunable: the reply goes into a prompt, and a
listing of 40 000 files is not an answer to any question a person asks.
"""

MAX_SEARCH_HITS: Final[int] = 200
"""Same reasoning as :data:`MAX_LIST_ENTRIES`, for glob results."""

MAX_READ_CHARS: Final[int] = 200_000
"""Upper bound on a single read.

Above this the tool says how big the file is instead of returning it, so the
model can decide to read a slice rather than have its context filled by one
call. ``tools.max_result_chars`` still truncates the reply afterwards.
"""

_LIST_DIRECTORY = ToolSpec(
    name="list_directory",
    description="列出某个目录下的文件和子目录，包含大小与修改时间。只读。",
    parameters=object_schema(
        {
            "path": string_property("目录路径；留空表示用户主目录", default=""),
            "pattern": string_property("可选的过滤通配符，例如 *.log", default=""),
            "recursive": boolean_property("是否递归列出子目录", default=False),
        }
    ),
)

_READ_FILE = ToolSpec(
    name="read_file",
    description="读取一个文本文件的内容。只读。",
    parameters=object_schema(
        {
            "path": string_property("文件路径"),
            "max_chars": integer_property("最多读取多少个字符", default=20000, minimum=1),
        },
        required=["path"],
    ),
)

_SEARCH_FILES = ToolSpec(
    name="search_files",
    description="在目录下按文件名通配符查找文件，例如 *.pdf 或 报告*.docx。只读。",
    parameters=object_schema(
        {
            "path": string_property("起始目录；留空表示用户主目录", default=""),
            "pattern": string_property("文件名通配符，例如 *.log"),
        },
        required=["pattern"],
    ),
)

_WRITE_FILE = ToolSpec(
    name="write_file",
    description="把内容写入文件；append 为 true 时追加，否则覆盖。会修改磁盘。",
    parameters=object_schema(
        {
            "path": string_property("文件路径"),
            "content": string_property("要写入的内容"),
            "append": boolean_property("是否追加而不是覆盖", default=False),
        },
        required=["path", "content"],
    ),
    risk=RiskLevel.CAUTION,
    permissions=frozenset({PERMISSION_WRITE}),
)

_DELETE_PATH = ToolSpec(
    name="delete_path",
    description="删除一个文件或空目录。不可恢复，需要用户确认。",
    parameters=object_schema(
        {"path": string_property("要删除的路径")},
        required=["path"],
    ),
    risk=RiskLevel.DANGEROUS,
    permissions=frozenset({PERMISSION_WRITE}),
)


def _format_size(size: int) -> str:
    """Human-readable size; the model reasons better about "1.2 MB" than digits."""
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"


def _format_mtime(timestamp: float) -> str:
    return datetime.datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M")


def _path_argument(arguments: Mapping[str, object], policy: ToolPolicy) -> Path:
    raw = arguments.get("path")
    if not isinstance(raw, str) or not raw.strip():
        return policy.file_roots()[0]
    return policy.resolve_path(raw)


def _list_directory(policy: ToolPolicy) -> ToolHandler:
    def handler(arguments: Mapping[str, object]) -> str:
        root = _path_argument(arguments, policy)
        if not root.exists():
            raise ToolExecutionError(f"目录不存在：{root}")
        if not root.is_dir():
            raise ToolExecutionError(f"不是目录：{root}")
        pattern = arguments.get("pattern")
        glob_pattern = pattern.strip() if isinstance(pattern, str) and pattern.strip() else "*"
        recursive = bool(arguments.get("recursive"))

        entries: list[Path] = []
        try:
            if recursive:
                entries = sorted(root.rglob(glob_pattern))
            else:
                entries = sorted(root.glob(glob_pattern))
        except OSError as exc:
            raise ToolExecutionError(f"无法列出目录：{exc}") from exc

        lines = [f"{root}（共 {len(entries)} 项）"]
        truncated = len(entries) > MAX_LIST_ENTRIES
        for entry in entries[:MAX_LIST_ENTRIES]:
            try:
                stat = entry.stat()
                size = _format_size(stat.st_size)
                modified = _format_mtime(stat.st_mtime)
            except OSError:
                size, modified = "?", "?"
            marker = "/" if entry.is_dir() else ""
            lines.append(f"  {entry.name}{marker}  {size}  {modified}")
        if truncated:
            lines.append(f"  …（还有 {len(entries) - MAX_LIST_ENTRIES} 项未显示）")
        return "\n".join(lines)

    return handler


def _read_file(policy: ToolPolicy) -> ToolHandler:
    def handler(arguments: Mapping[str, object]) -> str:
        raw = arguments.get("path")
        if not isinstance(raw, str):
            raise ToolExecutionError("缺少文件路径")
        path = policy.resolve_path(raw)
        if not path.is_file():
            raise ToolExecutionError(f"文件不存在：{path}")
        raw_max = arguments.get("max_chars")
        limit = MAX_READ_CHARS if not isinstance(raw_max, int) else min(raw_max, MAX_READ_CHARS)
        try:
            size = path.stat().st_size
            with path.open("r", encoding="utf-8", errors="replace") as handle:
                content = handle.read(limit)
        except OSError as exc:
            raise ToolExecutionError(f"无法读取文件：{exc}") from exc
        header = f"{path}（{_format_size(size)}）"
        if size > limit:
            header += f"\n（只读取了前 {limit} 个字符）"
        return f"{header}\n{content}"

    return handler


def _search_files(policy: ToolPolicy) -> ToolHandler:
    def handler(arguments: Mapping[str, object]) -> str:
        raw_pattern = arguments.get("pattern")
        if not isinstance(raw_pattern, str) or not raw_pattern.strip():
            raise ToolExecutionError("缺少搜索通配符")
        root = _path_argument(arguments, policy)
        if not root.is_dir():
            raise ToolExecutionError(f"不是目录：{root}")
        pattern = raw_pattern.strip()
        hits: list[Path] = []
        try:
            for path in root.rglob("*"):
                if len(hits) >= MAX_SEARCH_HITS:
                    break
                if path.is_file() and fnmatch.fnmatch(path.name, pattern):
                    hits.append(path)
        except OSError as exc:
            raise ToolExecutionError(f"搜索失败：{exc}") from exc
        if not hits:
            return f"在 {root} 下没有找到匹配 {pattern} 的文件"
        lines = [f"在 {root} 下找到 {len(hits)} 个匹配 {pattern} 的文件："]
        lines.extend(f"  {path}" for path in hits)
        return "\n".join(lines)

    return handler


def _write_file(policy: ToolPolicy) -> ToolHandler:
    def handler(arguments: Mapping[str, object]) -> str:
        raw = arguments.get("path")
        content = arguments.get("content")
        if not isinstance(raw, str) or not isinstance(content, str):
            raise ToolExecutionError("缺少文件路径或内容")
        path = policy.resolve_path(raw)
        append = bool(arguments.get("append"))
        existed = path.exists()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a" if append else "w", encoding="utf-8") as handle:
                handle.write(content)
        except OSError as exc:
            raise ToolExecutionError(f"无法写入文件：{exc}") from exc
        verb = "追加" if append else ("覆盖" if existed else "创建")
        logger.info("%s %s (%d chars)", verb, path, len(content))
        return f"已{verb} {path}，写入 {len(content)} 个字符"

    return handler


def _delete_path(policy: ToolPolicy) -> ToolHandler:
    def handler(arguments: Mapping[str, object]) -> str:
        raw = arguments.get("path")
        if not isinstance(raw, str):
            raise ToolExecutionError("缺少要删除的路径")
        path = policy.resolve_path(raw)
        if not path.exists():
            raise ToolExecutionError(f"路径不存在：{path}")
        try:
            if path.is_dir():
                if any(path.iterdir()):
                    raise ToolExecutionError(f"目录不是空的，拒绝删除：{path}")
                path.rmdir()
            else:
                path.unlink()
        except OSError as exc:
            raise ToolExecutionError(f"无法删除：{exc}") from exc
        logger.warning("deleted %s", path)
        return f"已删除 {path}"

    return handler


def build(policy: ToolPolicy) -> list[tuple[ToolSpec, ToolHandler]]:
    """Return the tools this module contributes.

    Args:
        policy: Shared policy, so path containment uses the same configured
            roots as every other tool rather than a private copy.
    """
    return [
        (_LIST_DIRECTORY, _list_directory(policy)),
        (_READ_FILE, _read_file(policy)),
        (_SEARCH_FILES, _search_files(policy)),
        (_WRITE_FILE, _write_file(policy)),
        (_DELETE_PATH, _delete_path(policy)),
    ]


__all__ = ["build"]
