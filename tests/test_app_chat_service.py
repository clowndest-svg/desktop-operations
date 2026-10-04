"""Tests for the desktop chat service: text in, text out, and never a raise.

The interesting failures are the quiet ones -- a turn that disappears, a persona
that claims to have deleted files it never touched, a request that grows every
message the operator types. Those are what these assert.
"""

from __future__ import annotations

import re
from dataclasses import replace
from pathlib import Path
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
from jarvis.llm.openai_compat import _serialize_message
from jarvis.llm.types import ChatMessage, Role
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

        # ``conversation`` names the tab the answer belongs to rather than staying blank:
        # with several conversations open, the page has to be able to file a late answer
        # under the tab that asked, not under whichever one is showing when it lands.
        assert reply == ChatReply("今天星期几", "今天是星期六", conversation="hud")
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

        assert payload == {
            "question": "问",
            "answer": "答",
            "error": "",
            "reasoning": "",
            "model": "",
            "provider": "",
            "conversation": "",
            "task_id": "",
            "cancelled": False,
            "record": "",
        }

    def test_the_thinking_chain_reaches_the_page_and_nothing_else_does(self) -> None:
        """What crosses the bridge is what the page cannot work out for itself.

        ``reasoning`` has to cross or the box is permanently empty, and
        ``model``/``conversation``/``cancelled`` have to cross because with several tabs
        open and a round table answering into one panel, "which model said this, in
        which conversation, and was it stopped" is not something the page can infer from
        the order it received things in. ``record`` crosses for the same reason: the
        discussion only exists on this side of the bridge, and a page that was handed the
        merged conclusion cannot reconstruct who argued for it.
        ``sources``/``grounded``/token counts still do not: they travel through the state
        bridge, and every key added here is a key the built bundle has to be rebuilt in
        step with.
        """
        reply = ChatReply(
            "问",
            "答",
            sources=("手册.md 第 3 段",),
            grounded=True,
            reasoning="先查手册",
            reasoning_tokens=312,
            provider="deepseek",
            model="deepseek-chat",
            conversation="tab-1",
            task_id="aabbccddeeff",
        )

        assert reply.to_dict()["reasoning"] == "先查手册"
        assert reply.to_dict()["model"] == "deepseek-chat"
        assert reply.to_dict()["conversation"] == "tab-1"
        assert set(reply.to_dict()) == {
            "question",
            "answer",
            "error",
            "reasoning",
            "model",
            "provider",
            "conversation",
            "task_id",
            "cancelled",
            "record",
        }

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

    def test_the_picture_is_written_into_the_request_body(self) -> None:
        """What the provider actually receives, not what the field says.

        The gap used to be pinned from the other side: the attachment note had to say the
        picture stayed on the page, because ``_serialize_message`` threw ``images`` away.
        Testing only ``ChatMessage.images`` would start passing again if the array were
        dropped one layer further down -- and the model would then be told it had been
        handed something it could not see, which is the failure this whole class exists to
        prevent.
        """
        sent = _serialize_message(replace(ChatMessage.user("看图"), images=(self.IMAGE,)))
        assert sent["content"] == [
            {"type": "text", "text": "看图"},
            {"type": "image_url", "image_url": {"url": self.IMAGE}},
        ]
        assert "images" not in sent

    def test_a_message_without_a_picture_is_still_a_bare_string(self) -> None:
        """Uniformity would cost turns that never had a picture to lose.

        Some strict OpenAI-compatible endpoints reject a ``content`` array outright, so
        the array appears only when there is an image to carry.
        """
        assert _serialize_message(ChatMessage.user("只有字"))["content"] == "只有字"

    def test_a_picture_that_went_is_announced_as_one_that_went(self) -> None:
        client = FakeLlmClient("答")
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

        turn = client.calls[-1][-1]
        assert turn.images == (self.IMAGE,)
        assert "已随本条消息附上" in (turn.content or "")
        assert "没有随请求发出" not in (turn.content or "")

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
        # The cap still earns its place: an uncapped data URL would be stored in the
        # transcript and replayed into every later request. Now that a picture small
        # enough to keep *is* sent, the wording has to follow the same decision -- a
        # dropped one described as attached is the hallucination this class exists to stop.
        assert user_turn.images == ()
        assert "big.png" in (user_turn.content or "")
        assert "超过了随请求发送的上限" in (user_turn.content or "")
        assert "已随本条消息附上" not in (user_turn.content or "")

    def test_a_model_measured_as_blind_is_never_sent_the_picture(self) -> None:
        """The finding from 测一下 decides what goes on the wire.

        Sending it anyway costs a request that answers with a provider's own error text,
        and the operator then reads that as "she is broken" -- while the model that was
        measured blind would have answered the question happily without the picture.
        """
        client = FakeLlmClient("答")
        service = _service(client, vision_for=lambda provider, model: False)

        service.ask("这张图里是什么", attachments=[self._picture()])

        assert client.calls[-1][-1].images == ()
        content = client.calls[-1][-1].content or ""
        assert "没有随请求发出" in content
        assert "已随本条消息附上" not in content

    def test_a_model_that_was_never_tested_still_gets_the_picture(self) -> None:
        """ "Unknown" is not "cannot".

        The provider's own refusal is a truer report than a rule invented here silently
        throwing the screenshot away -- and the first refusal is what the panel's 测一下
        button then records.
        """
        client = FakeLlmClient("答")
        service = _service(client, vision_for=lambda provider, model: None)
        service.ask("这张图里是什么", attachments=[self._picture()])
        assert client.calls[-1][-1].images == (self.IMAGE,)

    def test_a_blind_verdict_for_one_pair_does_not_blind_the_whole_window(self) -> None:
        client = FakeLlmClient("答")
        seen: list[tuple[str, str]] = []

        def vision_for(provider: str, model: str) -> bool:
            seen.append((provider, model))
            return model != "blind-model"

        service = _service(client, vision_for=vision_for, client_for=lambda provider, model: client)
        service.ask("图", provider="testai", model="blind-model", attachments=[self._picture()])
        service.ask("图", provider="testai", model="sighted-model", attachments=[self._picture()])
        blind, sighted = (client.calls[index][-1].images for index in (0, 1))
        assert blind == ()
        assert sighted == (self.IMAGE,)
        assert seen == [("testai", "blind-model"), ("testai", "sighted-model")]

    @staticmethod
    def _picture() -> dict[str, object]:
        return {
            "name": "a.png",
            "kind": "image",
            "mime": "image/png",
            "size": 9,
            "data": TestAttachments.IMAGE,
        }

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


FRONTEND = Path(__file__).resolve().parent.parent / "frontend" / "src"


def _page(relative: str) -> str:
    return (FRONTEND / relative).read_text(encoding="utf-8")


def _number_in(source: str, pattern: str) -> int:
    match = re.search(pattern, source)
    assert match, f"页面里没有 {pattern}：数字搬走之前先改这里"
    return int(match.group(1).replace("_", ""))


class TestThePageShrinksWhatItSends:
    """The cap only works if the page does its half before 发送 is pressed.

    A picture over ``MAX_IMAGE_DATA_CHARS`` is dropped by :func:`_clean_attachments` and
    the model is told it never arrived -- correct, and useless: the operator pasted a
    screenshot and watched a chip that looked attached. So the browser compresses first,
    and these read the page source to check that it still does, because no Python test can
    run a canvas.
    """

    def test_the_desktop_budget_fits_under_the_backend_cap(self) -> None:
        budget = _number_in(_page("api/thumbnail.ts"), r"export const MAX_DATA_CHARS = ([\d_]+)")
        assert budget <= MAX_IMAGE_DATA_CHARS, "页面发得出去的上限不能超过后端收得下的上限"
        assert budget >= MAX_IMAGE_DATA_CHARS * 0.9, "余量留太多等于把本来发得出去的图也压掉了"

    def test_the_picture_paths_run_through_the_ladder(self) -> None:
        panel = _page("components/ChatPanel.vue")
        assert "shrinkToBudget" in panel, "粘贴/选图那条路没走压缩"
        assert "from '@/api/thumbnail'" in panel

    def test_no_second_compressor_appears_in_the_chat_panel(self) -> None:
        """One ladder, or the two drift and only one of them gets fixed.

        ``avatar/PetStage.vue`` encodes a canvas for the pet frame and that is a different
        problem; a second ``toDataURL`` in the chat panel would not be.
        """
        assert "toDataURL" not in _page("components/ChatPanel.vue")

    def test_a_picture_it_cannot_shrink_is_said_out_loud(self) -> None:
        """Silence here is the invisible failure this project keeps getting told about.

        Dropping ``data`` quietly sends a chip that looks attached to a model that only
        got a filename, so the panel has to say why it refused.
        """
        panel = _page("components/ChatPanel.vue")
        assert "catch (error)" in panel
        assert "localError.value = `${base.name}" in panel

    def test_the_phone_and_the_desktop_ask_for_the_same_room(self) -> None:
        """Two clients, one server-side cap.

        400_000 on one side and 12_000 on the other would both "work" -- and one of them
        would lose pictures on a phone with no explanation anywhere.
        """
        mobile = _number_in(_page("mobile/api.ts"), r"export const MAX_DATA_CHARS = ([\d_]+)")
        desktop = _number_in(_page("api/thumbnail.ts"), r"export const MAX_DATA_CHARS = ([\d_]+)")
        assert mobile == desktop, "两边的余量要对得上，否则一边压完另一边还是要丢"
