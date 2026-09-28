"""Tests for the desktop chat service: text in, text out, and never a raise.

The interesting failures are the quiet ones -- a turn that disappears, a persona
that claims to have deleted files it never touched, a request that grows every
message the operator types. Those are what these assert.
"""

from __future__ import annotations

import pytest

from jarvis.app.chat_service import HUD_SYSTEM_PROMPT, ChatReply, ChatService
from jarvis.core.exceptions import LlmError
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
