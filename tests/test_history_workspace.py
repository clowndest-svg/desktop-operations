"""History beyond the old display caps, with migration and real bridge exports."""

from __future__ import annotations

import types
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from jarvis.app.chat_service import ChatService
from jarvis.app.transcript_service import MIGRATIONS, NAMESPACE, TranscriptService
from jarvis.database import SqliteStore
from jarvis.ui.desktop import HudBridge
from tests._fakes import FakeLlmClient


@pytest.fixture()
def history(tmp_path: Path) -> Iterator[tuple[SqliteStore, TranscriptService, HudBridge]]:
    store = SqliteStore(tmp_path / "history.db")
    store.start()
    transcript = TranscriptService(store)
    transcript.start()
    chat = ChatService(lambda: FakeLlmClient(), transcript=transcript)
    chat.start()
    bridge = HudBridge.__new__(HudBridge)
    bridge._chat = chat
    bridge._paths = types.SimpleNamespace(exports_dir=tmp_path / "exports")  # type: ignore[assignment]
    yield store, transcript, bridge
    chat.stop()
    store.stop()


def test_search_finds_old_content_and_literal_characters(
    history: tuple[SqliteStore, TranscriptService, HudBridge],
) -> None:
    _, transcript, bridge = history
    old = transcript.new_session("旧项目")
    transcript.append(old, "user", "开头" * 250 + "预算 100%_O'Reilly Python 资料")
    for n in range(65):
        transcript.new_session(f"其他 {n}")
    first = bridge.chat_sessions()
    assert len(first["sessions"]) == 50  # type: ignore[arg-type]
    assert first["has_more"] is True
    for term in ("预算", "100%_O'Reilly", "pYTHON", "旧项目"):
        page = bridge.chat_sessions(term)
        assert not page["error"]
        rows = page["sessions"]
        assert isinstance(rows, list)
        assert [row["id"] for row in rows] == [old]
    matched = transcript.session_page("预算")
    assert "预算" in matched["sessions"][0]["excerpt"]
    assert len(matched["sessions"][0]["excerpt"]) <= 160
    assert transcript.session_page("100%X")["sessions"] == []
    assert transcript.session_page("' OR 1=1 --")["sessions"] == []


def test_pagination_is_stable_for_equal_timestamps_and_pin_preserves_time(
    history: tuple[SqliteStore, TranscriptService, HudBridge],
) -> None:
    store, transcript, bridge = history
    keys = [transcript.new_session(f"项目 {n}") for n in range(126)]
    with store.transaction() as conn:
        conn.execute("UPDATE chat_sessions SET updated_at = '2026-10-26'")
    collected: list[str] = []
    offset = 0
    while True:
        page = transcript.session_page(offset=offset)
        collected.extend(row["id"] for row in page["sessions"])
        offset = page["next_offset"]
        if not page["has_more"]:
            break
    assert set(collected) == set(keys)
    assert len(collected) == len(set(collected))
    assert bridge.chat_pin(keys[0], True)["error"] == ""
    pinned = transcript.session_page(pinned_only=True)["sessions"]
    assert len(pinned) == 1 and pinned[0]["id"] == keys[0]
    assert pinned[0]["updated_at"] == "2026-10-26"
    assert transcript.list_sessions()[0]["id"] == keys[0]
    assert bridge.chat_pin("missing", True)["error"]
    assert bridge.chat_pin(keys[0], "false")["error"]  # type: ignore[arg-type]
    assert bridge.chat_sessions(offset="invalid")["error"]  # type: ignore[arg-type]
    assert bridge.chat_pin(keys[0], False)["error"] == ""
    assert transcript.session_page(pinned_only=True)["sessions"] == []


def test_old_database_migrates_and_pins_survive_restart(tmp_path: Path) -> None:
    path = tmp_path / "old.db"
    store = SqliteStore(path)
    store.start()
    store.migrate(NAMESPACE, MIGRATIONS[:3])
    with store.transaction() as conn:
        conn.execute("INSERT INTO chat_sessions VALUES ('old', '旧会话', '2020', '2020')")
        conn.execute(
            "INSERT INTO chat_messages(session_id,role,content,at) "
            "VALUES('old','user','原记录','2020')"
        )
    transcript = TranscriptService(store)
    transcript.start()
    assert not transcript.list_sessions()[0]["pinned"]
    assert transcript.pin("old", True)
    store.stop()
    reopened = SqliteStore(path)
    reopened.start()
    try:
        after = TranscriptService(reopened)
        after.start()
        assert after.list_sessions()[0]["pinned"] is True
        assert after.whole_session("old")[0]["content"] == "原记录"
    finally:
        reopened.stop()


def test_open_tab_metadata_is_not_limited_to_recent_two_hundred_sessions(
    history: tuple[SqliteStore, TranscriptService, HudBridge],
) -> None:
    _, transcript, bridge = history
    old = transcript.new_session("很久以前")
    transcript.append(old, "user", "你好")
    transcript.append(old, "assistant", "你好呀")
    for n in range(205):
        transcript.new_session(f"新 {n}")
    assert bridge._chat is not None
    bridge._chat.focus(old)
    card = next(row for row in bridge._chat.conversations() if row["id"] == old)
    assert card["title"] == "很久以前"
    assert card["stored_turns"] == 1


def test_full_export_uses_old_title_and_all_messages_without_overwriting(
    history: tuple[SqliteStore, TranscriptService, HudBridge],
) -> None:
    _, transcript, bridge = history
    old = transcript.new_session("完整旧对话")
    for n in range(262):
        transcript.append(old, "user" if n % 2 == 0 else "assistant", f"消息-{n:04}")
    for n in range(60):
        transcript.new_session(f"新对话 {n}")
    assert len(transcript.whole_session(old)) == 200
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(bridge.chat_export, [old] * 4))
    assert all(result["ok"] and result["count"] == 262 for result in results)
    paths = [Path(str(result["path"])) for result in results]
    assert len(set(paths)) == 4
    for path in paths:
        text = path.read_text(encoding="utf-8")
        assert text.startswith("# 完整旧对话\n")
        assert "消息-0000" in text and "消息-0261" in text
        assert "消息条数：262" in text
    assert bridge.chat_export("missing")["ok"] is False


def test_database_failure_is_visible_and_creates_no_export(
    history: tuple[SqliteStore, TranscriptService, HudBridge],
) -> None:
    store, _, bridge = history
    store.stop()
    assert bridge.chat_sessions()["error"]
    result = bridge.chat_export("old")
    assert result["ok"] is False and result["error"]
