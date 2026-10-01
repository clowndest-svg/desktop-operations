"""Tests for the desktop chat service: text in, text out, and never a raise.

The interesting failures are the quiet ones -- a turn that disappears, a persona
that claims to have deleted files it never touched, a request that grows every
message the operator types. Those are what these assert.
"""

from __future__ import annotations

from typing import Any

import pytest

from jarvis.app.chat_service import (
    HUD_SYSTEM_PROMPT,
    MAX_ATTACHMENTS,
    MAX_IMAGE_DATA_CHARS,
    ChatReply,
    ChatService,
)
from jarvis.app.transcript_service import TranscriptService
from jarvis.core.exceptions import LlmError
from jarvis.database import SqliteStore
from jarvis.llm.types import Role
from tests._fakes import FakeLlmClient


class ExplodingClient(FakeLlmClient):
    """A client whose ``complete`` raises whatever the provider would have."""

    def __init__(self, error: Exception) -> None:
        super().__init__()
        self._error = error

    def complete(self, messages, *, options=None):  # type: ignore[no-untyped-def]
        raise self._error


def _service(client: FakeLlmClient, **kwargs: object) -> ChatService:
    service = ChatService(lambda: client, **kwargs)  # type: ignore[arg-type]
    service.start()
    return service


class TestLifecycle:
    def test_asking_before_start_reports_instead_of_calling_the_model(self) -> None:
        client = FakeLlmClient("不该被调用")

        service = ChatService(lambda: client)
        reply = service.ask("现在几点")

        assert reply.error == "对话服务未启动"
        assert client.calls == []

    def test_start_loads_nothing(self) -> None:
        service = _service(FakeLlmClient())

        assert service.running is True
        assert service.history_length == 0


class TestTurns:
    def test_a_question_gets_the_canned_answer(self) -> None:
        client = FakeLlmClient("今天是星期六")
        service = _service(client)

        reply = service.ask("今天星期几")

        assert reply == ChatReply("今天星期几", "今天是星期六")
        assert len(client.calls) == 1

    def test_the_persona_is_the_first_message(self) -> None:
        client = FakeLlmClient("ok")
        service = _service(client)

        service.ask("你好")

        sent = client.calls[0]
        assert sent[0].role is Role.SYSTEM
        assert sent[0].content == HUD_SYSTEM_PROMPT
        assert sent[-1].role is Role.USER

    def test_whitespace_only_is_refused_without_a_model_call(self) -> None:
        client = FakeLlmClient()
        service = _service(client)

        assert service.ask(" " + chr(10) + chr(9) + " ").error == "问题是空的"
        assert client.calls == []

    def test_an_empty_model_reply_is_reported_not_shown_as_blank(self) -> None:
        service = _service(FakeLlmClient("   "))

        reply = service.ask("讲个笑话")

        assert reply.answer == ""
        assert reply.error == "模型没有返回内容"

    def test_a_follow_up_sees_the_previous_exchange(self) -> None:
        client = FakeLlmClient("好的")
        service = _service(client)

        service.ask("把音量调到 20")
        service.ask("再说一遍")

        sent = client.calls[-1]
        assert [message.role for message in sent] == [
            Role.SYSTEM,
            Role.USER,
            Role.ASSISTANT,
            Role.USER,
        ]
        assert sent[1].content == "把音量调到 20"
        assert sent[2].content == "好的"

    def test_history_is_capped(self) -> None:
        service = _service(FakeLlmClient("ok"), max_history_turns=2)

        for index in range(6):
            service.ask(f"问题 {index}")

        assert service.history_length == 2

    def test_clear_history_forgets_the_conversation(self) -> None:
        service = _service(FakeLlmClient("ok"))
        service.ask("第一问")
        assert service.history_length == 1

        service.clear_history()

        assert service.history_length == 0

    def test_a_failed_turn_does_not_poison_history(self) -> None:
        client = FakeLlmClient("ok")
        service = _service(client)
        service.ask("第一问")

        service.ask("")  # refused before the model

        assert service.history_length == 1


class TestFailuresStayOrdinaryReturns:
    """Nothing here may raise: the caller is a JS bridge, not a try block."""

    @pytest.mark.parametrize(
        ("error", "expected"),
        [
            (LlmError("鉴权失败"), "鉴权失败"),
            (RuntimeError("socket closed"), "RuntimeError"),
        ],
    )
    def test_model_errors_become_an_error_field(self, error: Exception, expected: str) -> None:
        service = _service(ExplodingClient(error))

        reply = service.ask("你好")

        assert reply.answer == ""
        assert expected in reply.error

    def test_a_broken_provider_is_reported(self) -> None:
        def boom() -> FakeLlmClient:
            raise RuntimeError("配置未加载")

        service = ChatService(boom)
        service.start()

        reply = service.ask("你好")

        assert "配置未加载" in reply.error

    def test_a_provider_that_raises_a_jarvis_error_keeps_its_message(self) -> None:
        def boom() -> FakeLlmClient:
            raise LlmError("没有配置 API Key")

        service = ChatService(boom)
        service.start()

        assert service.ask("你好").error == "没有配置 API Key"


class TestPayloadShape:
    def test_to_dict_carries_exactly_what_the_page_renders(self) -> None:
        payload = ChatReply("问", "答").to_dict()

        assert payload == {"question": "问", "answer": "答", "error": ""}

    def test_the_persona_does_not_claim_unperformed_actions(self) -> None:
        """The panel next to it really can delete files; the model must not bluff."""
        assert "不要声称你执行了没有执行的操作" in HUD_SYSTEM_PROMPT


class TestUnrememberedTurns:
    """``remembered=False``: a turn nobody said.

    The junk panel's 协助分析 hands over a machine-written list of paths. Left in
    the history, that list would be replayed into every later answer and quietly
    steer conversations about something else entirely.
    """

    def test_the_exchange_does_not_enter_the_history(self) -> None:
        service = _service(FakeLlmClient("ok"))

        service.ask("扫描结果：A、B、C", remembered=False)

        assert service.history_length == 0

    def test_a_normal_turn_still_replays_the_previous_ones(self) -> None:
        client = FakeLlmClient("ok")
        service = _service(client)
        service.ask("第一问")

        service.ask("机器写的清单", remembered=False)
        service.ask("第二问")

        digest = client.calls[-1]
        assert [message.role for message in digest].count(Role.USER) == 2
        assert digest[-1].content == "第二问"

    def test_an_unremembered_turn_still_answers(self) -> None:
        service = _service(FakeLlmClient("这三项都像是构建缓存"))

        reply = service.ask("扫描结果", remembered=False)

        assert reply.answer == "这三项都像是构建缓存"
        assert reply.error == ""


class TestStoredConversations:
    """History that outlives the process, and 清空 that stops meaning "erase".

    The complaint this answers is exact: clear the screen or quit the app and the
    conversation was gone, because it lived in one Python list. These pin the two
    halves of the fix -- turns reach the store, and blanking the screen opens a new
    session instead of deleting the old one.
    """

    @staticmethod
    def _stored(client: FakeLlmClient, tmp_path: Any) -> tuple[ChatService, TranscriptService]:
        store = SqliteStore(tmp_path / "chat.db")
        store.start()
        transcript = TranscriptService(store)
        transcript.start()
        service = ChatService(lambda: client, transcript=transcript)
        service.start()
        return service, transcript

    def test_an_answered_turn_lands_in_a_session_titled_by_the_question(
        self, tmp_path: Any
    ) -> None:
        service, transcript = self._stored(FakeLlmClient("星期六"), tmp_path)

        service.ask("今天星期几")

        sessions = transcript.list_sessions()
        assert len(sessions) == 1
        assert sessions[0]["title"] == "今天星期几"
        rows = transcript.whole_session(sessions[0]["id"])
        assert [(row["role"], row["content"]) for row in rows] == [
            ("user", "今天星期几"),
            ("assistant", "星期六"),
        ]

    def test_an_unremembered_turn_is_not_stored(self, tmp_path: Any) -> None:
        service, transcript = self._stored(FakeLlmClient("ok"), tmp_path)

        service.ask("机器写的清单", remembered=False)

        assert transcript.list_sessions() == []

    def test_clearing_the_screen_opens_a_new_session_and_keeps_the_old_one(
        self, tmp_path: Any
    ) -> None:
        service, transcript = self._stored(FakeLlmClient("答"), tmp_path)
        service.ask("第一问")

        service.clear_history()
        service.ask("第二问")

        sessions = transcript.list_sessions()
        assert len(sessions) == 2
        older = next(entry for entry in sessions if entry["title"] == "第一问")
        assert [row["content"] for row in transcript.whole_session(older["id"])] == ["第一问", "答"]

    def test_switching_replays_the_stored_turns_into_the_model_context(self, tmp_path: Any) -> None:
        client = FakeLlmClient("答")
        service, transcript = self._stored(client, tmp_path)
        service.ask("上周的问题")
        first = transcript.list_sessions()[0]["id"]
        service.new_session()

        service.switch_session(first)
        service.ask("接着上周的聊")

        sent = client.calls[-1]
        assert [message.content for message in sent[1:-1]] == ["上周的问题", "答"]

    def test_a_spoken_turn_joins_the_same_conversation_as_a_typed_one(self, tmp_path: Any) -> None:
        service, transcript = self._stored(FakeLlmClient("答"), tmp_path)
        service.append_turn("user", "说出来的问题")
        service.append_turn("assistant", "说出来的回答")

        sessions = transcript.list_sessions()
        assert len(sessions) == 1
        assert [row["role"] for row in transcript.whole_session(sessions[0]["id"])] == [
            "user",
            "assistant",
        ]

    def test_deleting_the_open_conversation_opens_a_fresh_one(self, tmp_path: Any) -> None:
        service, transcript = self._stored(FakeLlmClient("答"), tmp_path)
        service.ask("要被删掉的")
        gone = transcript.list_sessions()[0]["id"]

        listing = service.delete_session(gone)

        # Deleting the open conversation opens a blank one, so "empty list" is not
        # the shape of the answer; "the old id is gone and a new one is open" is.
        assert [entry["id"] for entry in transcript.list_sessions()] != [gone]
        assert gone not in [entry["id"] for entry in transcript.list_sessions()]
        assert listing["current"] != gone


class TestAttachments:
    """Files handed in with a question: carried, described, capped, never trusted.

    The page is a boundary. A data URL that arrives 40 MB long would be stored,
    replayed into every later request and drawn by the bubble -- so the cap is
    enforced here, where the string first becomes the server's problem.
    """

    IMAGE = "data:image/jpeg;base64,/9j/4AAQSkZJRg=="

    def test_an_image_reaches_the_model_as_a_message_part(self) -> None:
        client = FakeLlmClient("看见了")
        service = _service(client)

        service.ask(
            "这张图里是什么",
            attachments=[
                {
                    "name": "a.png",
                    "kind": "image",
                    "mime": "image/png",
                    "size": 9,
                    "data": self.IMAGE,
                }
            ],
        )

        user_turn = client.calls[-1][-1]
        assert user_turn.images == (self.IMAGE,)
        assert "a.png" in (user_turn.content or "")

    def test_a_video_travels_as_facts_only(self) -> None:
        client = FakeLlmClient("看不了视频")
        service = _service(client)

        service.ask(
            "这段视频呢",
            attachments=[{"name": "clip.mp4", "kind": "video", "mime": "video/mp4", "size": 4096}],
        )

        user_turn = client.calls[-1][-1]
        assert user_turn.images == ()
        assert "clip.mp4" in (user_turn.content or "")
        assert "不解析" in (user_turn.content or "")

    def test_an_oversize_image_is_named_but_not_carried(self) -> None:
        client = FakeLlmClient("ok")
        service = _service(client)

        service.ask(
            "大图",
            attachments=[
                {
                    "name": "big.png",
                    "kind": "image",
                    "mime": "image/png",
                    "size": 9,
                    "data": "data:image/png;base64," + "A" * (MAX_IMAGE_DATA_CHARS + 1),
                }
            ],
        )

        user_turn = client.calls[-1][-1]
        assert user_turn.images == ()
        assert "图太大" in (user_turn.content or "")

    def test_more_than_the_cap_is_dropped_rather_than_truncated(self) -> None:
        client = FakeLlmClient("ok")
        service = _service(client)

        service.ask(
            "一堆图",
            attachments=[
                {
                    "name": f"f{index}.png",
                    "kind": "image",
                    "mime": "image/png",
                    "size": 1,
                    "data": self.IMAGE,
                }
                for index in range(MAX_ATTACHMENTS + 3)
            ],
        )

        assert len(client.calls[-1][-1].images) == MAX_ATTACHMENTS

    def test_garbage_entries_are_dropped_without_raising(self) -> None:
        client = FakeLlmClient("ok")
        service = _service(client)

        reply = service.ask("乱来的附件", attachments=["not a mapping", {"name": 3}, None])  # type: ignore[list-item]

        assert reply.error == ""
        assert client.calls[-1][-1].images == ()

    def test_attachments_are_stored_with_the_turn(self, tmp_path: Any) -> None:
        store = SqliteStore(tmp_path / "chat.db")
        store.start()
        transcript = TranscriptService(store)
        transcript.start()
        service = ChatService(lambda: FakeLlmClient("答"), transcript=transcript)
        service.start()

        service.ask(
            "带图的问句",
            attachments=[
                {
                    "name": "a.png",
                    "kind": "image",
                    "mime": "image/png",
                    "size": 9,
                    "data": self.IMAGE,
                }
            ],
        )

        session = transcript.list_sessions()[0]["id"]
        rows = transcript.whole_session(session)
        assert rows[0]["attachments"][0]["name"] == "a.png"
        assert rows[1]["attachments"] == []
