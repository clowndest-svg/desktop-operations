"""Conversations on disk: the two tables behind 历史.

What these pin is the part a user feels: close the app, open it again, last
Tuesday is still there -- and 清空 did not delete it, it only stepped aside.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from jarvis.app.transcript_service import TranscriptService
from jarvis.database import SqliteStore


@pytest.fixture()
def store(tmp_path: Path) -> Iterator[SqliteStore]:
    database = SqliteStore(tmp_path / "chat.db")
    database.start()
    yield database
    database.stop()


@pytest.fixture()
def service(store: SqliteStore) -> TranscriptService:
    transcript = TranscriptService(store)
    transcript.start()
    return transcript


class TestSessions:
    def test_a_new_session_is_listed_newest_first(self, service: TranscriptService) -> None:
        first = service.new_session("早上的问题")
        second = service.new_session()

        listing = service.list_sessions()

        assert [entry["id"] for entry in listing] == [second, first]
        assert listing[1]["title"] == "早上的问题"

    def test_an_unstarted_store_answers_empty_instead_of_raising(self, store: SqliteStore) -> None:
        service = TranscriptService(store)

        assert service.list_sessions() == []
        assert service.new_session() == ""
        assert service.append("x", "user", "hi") is False

    def test_rename_and_delete_only_touch_their_row(self, service: TranscriptService) -> None:
        kept = service.new_session("留下")
        gone = service.new_session("删掉")

        assert service.rename(gone, "改过") is True
        assert service.delete(gone) is True

        assert [entry["id"] for entry in service.list_sessions()] == [kept]
        assert (
            service.delete(gone) is False
        ), "deleting twice is not an error to hide, but it is a no-op"


class TestTurns:
    def test_turns_come_back_oldest_first_and_capped(self, service: TranscriptService) -> None:
        session = service.new_session()
        for index in range(5):
            service.append(session, "user", f"问 {index}")
            service.append(session, "assistant", f"答 {index}")

        rows = service.messages(session, limit=4)

        assert [row["content"] for row in rows] == ["问 3", "答 3", "问 4", "答 4"]
        assert len(service.whole_session(session)) == 10

    def test_the_first_question_becomes_the_title(self, service: TranscriptService) -> None:
        session = service.new_session()

        service.append(session, "user", "今天星期几来着？")

        assert service.list_sessions()[0]["title"] == "今天星期几来着？"

    def test_deleting_a_session_takes_its_turns_with_it(self, service: TranscriptService) -> None:
        session = service.new_session()
        service.append(session, "user", "一句")

        service.delete(session)

        assert service.whole_session(session) == []

    def test_blank_and_empty_turns_are_refused_quietly(self, service: TranscriptService) -> None:
        session = service.new_session()

        assert service.append(session, "user", "   ") is False
        assert service.append("", "user", "有内容但没有会话") is False
        assert service.whole_session(session) == []
