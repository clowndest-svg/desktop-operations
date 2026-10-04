"""语音和打字必须是同一个助手，不是两个能力不同的助手。

分叉是真的存在过的：打字问「本机有没有装 Java」走 :class:`ChatService` 和它那张真工具
表；把同一句说出来，走的是 ``AgentGraph`` —— 一个先猜一次「要不要用工具」的调度图，
它那个 tools worker 只有两个硬编码能力。于是那句「我没法直接查看你的电脑」其实是实话。

这个文件只守一件事：**问题从哪张嘴进来不重要**，回答它的是同一个 agent、同一张工具表、
同一个存下来的会话。
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

from jarvis.app.chat_service import ChatService
from jarvis.app.transcript_service import TranscriptService
from jarvis.app.voice_graph import ChatGraph
from jarvis.config.schema import ToolsSection
from jarvis.core.exceptions import JarvisError
from jarvis.database import SqliteStore
from jarvis.llm.types import ChatMessage, ChatResponse, GenerationOptions, ToolCall
from jarvis.tools import ToolService
from jarvis.tools.disk_cleaner import DiskCleaner
from jarvis.tools.monitor import SystemMonitor

TEXT_STATS_ARGUMENTS = '{"text": "你说你说"}'
"""A registry-only tool. ``calculate`` and ``get_time`` would prove nothing: those two are
exactly what the old voice worker had hardcoded, so a turn that calls one of them is
consistent with both the bug and the fix."""


class _ScriptedClient:
    """A model client answering from a list, recording what it was asked."""

    provider_name = "fake"
    model = "fake"

    def __init__(self, *replies: ChatResponse, raises: Exception | None = None) -> None:
        self._replies = list(replies)
        self._raises = raises
        self.calls: list[list[ChatMessage]] = []
        self.options: list[GenerationOptions | None] = []

    def complete(
        self,
        messages: Sequence[ChatMessage],
        *,
        options: GenerationOptions | None = None,
    ) -> ChatResponse:
        self.calls.append(list(messages))
        self.options.append(options)
        if self._raises is not None:
            raise self._raises
        return self._replies.pop(0)

    def stream(self, messages: Any, *, options: Any = None) -> Any:
        del messages, options
        yield from ()


def _tool_call(name: str = "text_stats", arguments: str = TEXT_STATS_ARGUMENTS) -> ChatResponse:
    return ChatResponse(content="", model="fake", tool_calls=(ToolCall("c1", name, arguments),))


def _answer(text: str) -> ChatResponse:
    return ChatResponse(content=text, model="fake")


def _tools(tmp_path: Path) -> ToolService:
    """The real registry, with the real policy — nothing here stands in for dispatch."""
    section = ToolsSection(
        enabled=True,
        confirm_dangerous=True,
        allow_write=False,
        allow_shell=False,
        file_roots=(),
        max_result_chars=2000,
    )
    service = ToolService(
        lambda: section,
        monitor_factory=SystemMonitor,
        cleaner_factory=lambda: DiskCleaner(audit_log=tmp_path / "audit.jsonl", sources={}),
    )
    service.start()
    return service


def _chat(client: _ScriptedClient, **kwargs: Any) -> ChatService:
    service = ChatService(lambda: client, session_id="t", **kwargs)
    service.start()
    return service


class TestASpokenTurnGetsTheRegistry:
    def test_a_spoken_question_reaches_a_tool_the_old_worker_had(self, tmp_path: Path) -> None:
        client = _ScriptedClient(_tool_call(), _answer("四个字"))
        graph = ChatGraph(_chat(client, tool_provider=lambda: _tools(tmp_path)))

        assert graph.run("帮我数数这几个字") == "四个字"

    def test_the_registry_ran_it_because_the_audit_line_is_hers(self, tmp_path: Path) -> None:
        """The complaint was that the microphone *described* a tool it could not reach.
        So the proof is not the sentence but the registry's own record of the call."""
        tools = _tools(tmp_path)
        client = _ScriptedClient(_tool_call(), _answer("四个字"))

        ChatGraph(_chat(client, tool_provider=lambda: tools)).run("帮我数数这几个字")

        first = tools.recent()[0]
        assert first.tool == "text_stats"
        assert first.ok is True

    def test_the_tools_the_model_is_shown_are_the_registrys(self, tmp_path: Path) -> None:
        """Two tools were the old ceiling, and the ceiling was invisible: the model was
        never told about the other thirty-odd, so it could not even try."""
        tools = _tools(tmp_path)
        client = _ScriptedClient(_tool_call(), _answer("四个字"))

        ChatGraph(_chat(client, tool_provider=lambda: tools)).run("帮我数数这几个字")

        sent = client.options[0]
        assert sent is not None and sent.tools is not None
        assert len(sent.tools) == len(tools.to_openai_tools()) > 2
        assert "text_stats" in str(sent.tools)

    def test_the_tool_answer_is_handed_back_before_the_model_replies(self, tmp_path: Path) -> None:
        """Not just "a tool was named": the registry's real output has to arrive in the
        second request, because that round trip is the whole difference between calling
        a tool and talking about one."""
        client = _ScriptedClient(_tool_call(), _answer("四个字"))

        ChatGraph(_chat(client, tool_provider=lambda: _tools(tmp_path))).run("帮我数数")

        assert len(client.calls) == 2
        carried = [message.content for message in client.calls[1] if message.role.value == "tool"]
        assert carried == ["字符数：4\n行数：1\n词数（按空白切分）：1\n高频字符：你×2、说×2"]


class TestOneConversation:
    def test_a_spoken_turn_is_stored_once_not_twice(self, tmp_path: Path) -> None:
        """The pipeline used to hand every turn to a ``transcript_sink`` *and* the agent
        wrote it. One writer now, so a sentence cannot be remembered twice."""
        store = SqliteStore(tmp_path / "chat.db")
        store.start()
        transcript = TranscriptService(store)
        transcript.start()
        client = _ScriptedClient(_answer("星期六"))
        service = ChatService(lambda: client, transcript=transcript)
        service.start()

        ChatGraph(service).run("今天星期几")

        sessions = transcript.list_sessions()
        assert len(sessions) == 1
        rows = transcript.whole_session(sessions[0]["id"])
        assert [(row["role"], row["content"]) for row in rows] == [
            ("user", "今天星期几"),
            ("assistant", "星期六"),
        ]

    def test_what_was_said_is_visible_to_what_is_typed_next(self) -> None:
        client = _ScriptedClient(_answer("装了，Temurin 17"), _answer("那就用它编译"))
        chat = _chat(client)

        ChatGraph(chat).run("本机有没有装 Java")
        chat.ask("那用 JDK 8 行不行")

        replayed = [message.content for message in client.calls[1]]
        assert "本机有没有装 Java" in replayed
        assert "装了，Temurin 17" in replayed

    def test_the_pipelines_own_copy_of_the_history_is_ignored(self) -> None:
        """Two copies of a conversation, and only one of them survives a restart, shows up
        in 历史, and contains the typed turns. Answering from the other is how an assistant
        starts contradicting itself about what was just said."""
        client = _ScriptedClient(_answer("答"))
        graph = ChatGraph(_chat(client))

        graph.run("这一句", [ChatMessage.user("管道自己留的那份历史")])

        assert all("管道自己留的那份历史" not in message.content for message in client.calls[0])


class TestAFailedTurnIsStillHeard:
    def test_a_turn_that_only_failed_is_said_out_loud(self) -> None:
        """Silence is the one failure an operator cannot debug — and on this path silence
        is also literally what the speaker does."""
        graph = ChatGraph(_chat(_ScriptedClient(raises=JarvisError("模型连不上"))))

        assert graph.run("你好") == "模型连不上"

    def test_a_model_that_returns_nothing_reads_as_nothing_rather_than_a_lie(self) -> None:
        graph = ChatGraph(_chat(_ScriptedClient(_answer(""))))

        assert graph.run("你好") == "模型没有返回内容"

    def test_a_turn_before_the_service_started_says_so(self) -> None:
        unstarted = ChatService(lambda: _ScriptedClient(_answer("答")))

        assert ChatGraph(unstarted).run("你好") == "对话服务未启动"
