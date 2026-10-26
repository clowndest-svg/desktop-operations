"""Render one stored conversation as Markdown for the user to keep."""

from __future__ import annotations

import time
from collections.abc import Mapping, Sequence


def _text(turn: Mapping[str, object], key: str) -> str:
    value = turn.get(key)
    return value if isinstance(value, str) else ""


def _attachment_names(turn: Mapping[str, object]) -> list[str]:
    raw = turn.get("attachments")
    if not isinstance(raw, list):
        return []
    names: list[str] = []
    for item in raw:
        if isinstance(item, Mapping):
            name = item.get("name") or item.get("path")
            if isinstance(name, str) and name.strip():
                names.append(name.replace("\\", "/").rsplit("/", 1)[-1])
        elif isinstance(item, str) and item.strip():
            names.append(item.replace("\\", "/").rsplit("/", 1)[-1])
    return names


def render_conversation(
    title: str,
    turns: Sequence[Mapping[str, object]],
    *,
    exported_at: str | None = None,
) -> str:
    """A readable transcript: who spoke, when, which model, and the attachments."""
    stamp = exported_at or time.strftime("%Y-%m-%d %H:%M:%S")
    lines = [
        f"# {title or '对话'}",
        "",
        f"- 导出时间：{stamp}",
        f"- 对话轮次：{len(turns)}",
        "",
        "---",
        "",
    ]
    for turn in turns:
        role = _text(turn, "role")
        label = "你" if role == "user" else "小夜" if role == "assistant" else role or "未知"
        at = _text(turn, "at")
        model = _text(turn, "model")
        heading = f"## {label}" + (f" · {at}" if at else "") + (f" · {model}" if model else "")
        lines.append(heading)
        lines.append("")
        content = _text(turn, "content").strip()
        lines.append(content or "（这一轮没有文字）")
        lines.append("")
        names = _attachment_names(turn)
        if names:
            lines.append("附件：" + "、".join(f"`{name}`" for name in names))
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"
