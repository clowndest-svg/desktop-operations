"""Markdown export of a stored conversation stays inside the data directory."""

from __future__ import annotations

import types
from pathlib import Path
from typing import Any

from jarvis.app.chat_export import render_conversation
from jarvis.ui.desktop import HudBridge


def _turns() -> list[dict[str, Any]]:
    return [
        {
            "id": 1,
            "role": "user",
            "content": "你好",
            "at": "2026-10-25 10:00:00",
            "model": "",
            "attachments": [{"name": "C:\\secret\\note.txt"}],
        },
        {
            "id": 2,
            "role": "assistant",
            "content": "你好，我在。",
            "at": "2026-10-25 10:00:01",
            "model": "local",
            "attachments": [],
        },
    ]


def test_markdown_renders_roles_times_models_and_attachment_names() -> None:
    body = render_conversation("测试对话", _turns(), exported_at="2026-10-25 12:00:00")
    assert body.startswith("# 测试对话\n")
    assert "- 导出时间：2026-10-25 12:00:00" in body
    assert "## 你 · 2026-10-25 10:00:00" in body
    assert "## 小夜 · 2026-10-25 10:00:01 · local" in body
    assert "附件：`note.txt`" in body
    assert "secret" not in body, "只留文件名，不在导出里展开原路径"


def _bridge(tmp_path: Path, turns: list[dict[str, Any]] | None) -> HudBridge:
    class Chat:
        def session_messages(self, _session: str) -> dict[str, object]:
            if turns is None:
                return {"error": "没有这个对话", "messages": []}
            return {"error": "", "messages": turns}

        def sessions(self) -> dict[str, object]:
            return {"current": "c1", "sessions": [{"id": "c1", "title": "测试对话"}]}

    bridge = HudBridge.__new__(HudBridge)
    bridge._chat = Chat()  # type: ignore[assignment]
    bridge._paths = types.SimpleNamespace(exports_dir=tmp_path)  # type: ignore[assignment]
    return bridge


def test_export_writes_unique_markdown_under_the_dialogue_folder(tmp_path: Path) -> None:
    bridge = _bridge(tmp_path, _turns())
    first = bridge.chat_export("c1")
    second = bridge.chat_export("c1")
    assert first["ok"] is True and second["ok"] is True
    assert Path(str(first["path"])).parent == tmp_path / "对话"
    assert Path(str(first["path"])) != Path(str(second["path"]))
    assert "你好，我在。" in Path(str(first["path"])).read_text(encoding="utf-8")
    assert first["count"] == 2


def test_empty_or_missing_conversation_is_an_error_without_a_file(tmp_path: Path) -> None:
    assert _bridge(tmp_path, []).chat_export("c1")["ok"] is False
    missing = _bridge(tmp_path, None).chat_export("missing")
    assert missing["ok"] is False and "没有这个对话" in str(missing["error"])
    assert not (tmp_path / "对话").exists()
