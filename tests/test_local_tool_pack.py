"""The join: a real question through ``ChatService`` reaches a real registry narrowed.

Unit tests for :mod:`jarvis.app.tool_routing` prove the classifier picks names. That is
not the claim that matters. The claim is that the ``tools`` array on the wire to a
bundled local model contains ``runtime_versions`` and not ``mouse_click`` -- which only
a test that runs the actual registry and the actual request builder can show. Hand a
test a hand-written dict of tool names and it will happily validate a wiring bug.

Provider ``local`` is the one the shipped llama.cpp answers to (``LOCAL_PROVIDER``),
so these are the turns the 4K screenshot was about.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

from jarvis.app.chat_service import ChatService
from jarvis.config.schema import ToolsSection
from jarvis.knowledge import KnowledgeService
from jarvis.llm.types import ChatResponse, GenerationOptions
from jarvis.tools.disk_cleaner import DiskCleaner
from jarvis.tools.monitor import SystemMonitor
from jarvis.tools.service import ToolService
from tests._fakes import FakeLlmClient
from tests.test_recall_tools import _knowledge_service, _store, _vector

BUNDLED = ("local", "qwen3-4b")
HOSTED = ("qwenai", "qwen3.8-flash")


def _section() -> ToolsSection:
    return ToolsSection.from_mapping(
        {
            "enabled": True,
            "confirm_dangerous": True,
            "allow_write": False,
            "allow_shell": False,
            "file_roots": [],
            "max_result_chars": 8000,
        }
    )


class _RecordingClient(FakeLlmClient):
    """Keeps the tool table of every request, the way the wire would carry it."""

    def __init__(self, reply: str = "答") -> None:
        super().__init__(reply)
        self.carried: list[list[str]] = []

    def complete(self, messages: Any, *, options: GenerationOptions | None = None) -> ChatResponse:
        tools = list(options.tools) if options is not None and options.tools else []
        self.carried.append([str(tool["function"]["name"]) for tool in tools])  # type: ignore[index]
        return super().complete(messages, options=options)


def _registry(tmp_path: Path, *, knowledge: KnowledgeService | None = None) -> ToolService:
    """The desktop surface: everything ``__main__`` hands her, stand-ins behind it.

    A bare ``ToolService`` registers thirteen tools, because the panel-backed ones are
    left out when nothing is behind them. Routing tested against that would hide the
    interesting cases -- ``open_app`` and ``run_powershell`` are exactly the tools the
    classifier has to place.
    """
    stub = MagicMock()
    service = ToolService(
        _section,
        monitor_factory=lambda: SystemMonitor(top_processes=5),
        cleaner_factory=lambda: DiskCleaner(tmp_path / "audit.jsonl"),
        computer_factory=lambda: stub,
        shell_gate_factory=lambda: stub,
        speaker=stub,
        reminders=stub,
        memory=stub,
        knowledge=knowledge if knowledge is not None else stub,
        monitor=SystemMonitor(top_processes=5),
        usage=stub,
        proposals=stub,
        settings=stub,
        voices=stub,
        conversations=stub,
        alerts=stub,
    )
    service.start()
    return service


def _chat(
    tmp_path: Path, provider: tuple[str, str], *, knowledge: KnowledgeService | None = None
) -> tuple[ChatService, _RecordingClient]:
    client = _RecordingClient()
    service = ChatService(
        lambda: client,
        tool_provider=lambda: _registry(tmp_path, knowledge=knowledge),
        pair_provider=lambda: provider,
        session_id="t",
    )
    service.start()
    return service, client


class TestTheTableOnTheWire:
    def test_a_version_question_carries_the_runtime_tool_and_little_else(
        self, tmp_path: Path
    ) -> None:
        service, client = _chat(tmp_path, BUNDLED)

        reply = service.ask("检查本机 JDK 版本")

        sent = client.carried[-1]
        assert "runtime_versions" in sent, "没有这条，她只能回答「我查不了 JDK」"
        assert "system_report" in sent
        assert "mouse_click" not in sent and "open_app" not in sent
        assert len(sent) < 10, f"本地那一发不该再拖走整张表：{sent}"
        assert reply.tool_pack == "system+runtime"
        assert reply.tool_count == len(sent)

    def test_opening_an_app_carries_open_app(self, tmp_path: Path) -> None:
        service, client = _chat(tmp_path, BUNDLED)

        service.ask("打开记事本")

        assert set(client.carried[-1]) == {"current_time", "calculate", "open_app"}

    def test_a_web_question_carries_search_and_fetch(self, tmp_path: Path) -> None:
        service, client = _chat(tmp_path, BUNDLED)

        service.ask("搜一下 Qwen3 最新的上下文长度")

        assert {"search_web", "fetch_url"} <= set(client.carried[-1])

    def test_a_hosted_pair_keeps_every_tool(self, tmp_path: Path) -> None:
        """The narrowing is a limit of the endpoint, not a policy about her abilities."""
        service, client = _chat(tmp_path, HOSTED)

        service.ask("检查本机 JDK 版本")

        sent = client.carried[-1]
        assert len(sent) > 30, "云端那一发要带走整张表"
        assert "mouse_click" in sent and "runtime_versions" in sent

    def test_an_ordinary_sentence_still_gets_the_conversational_minimum(
        self, tmp_path: Path
    ) -> None:
        service, client = _chat(tmp_path, BUNDLED)

        service.ask("你好啊")

        assert set(client.carried[-1]) <= {
            "current_time",
            "calculate",
            "memory_search",
            "remember",
            "knowledge_search",
            "chat_search",
        }

    def test_the_fallback_pack_can_still_be_told_something(self, tmp_path: Path) -> None:
        """「记一下…」 matched no pack in r89's run and she answered 「无法直接保存」.

        The sentence was the single most assistant-shaped thing in the list and a word
        rule decided whether the tool was even on the wire.
        """
        service, client = _chat(tmp_path, BUNDLED)

        service.ask("嗯，我们先聊点别的")

        assert "remember" in client.carried[-1], "兜底那一包必须还能被吩咐记事"

    def test_nothing_is_ever_advertised_twice(self, tmp_path: Path) -> None:
        """A tool shared by two matched packs must appear once -- tokens are the budget."""
        service, client = _chat(tmp_path, BUNDLED)

        service.ask("看下内存，再查下 JDK 和 Python 的版本")

        sent = client.carried[-1]
        assert len(sent) == len(set(sent))


class TestThePointerIsConditional:
    def test_a_machine_question_is_not_framed_as_a_corpus_question(self, tmp_path: Path) -> None:
        """The r87 screenshot, as a test.

        「打开记事本」 came back as 「资料库里没有关于“打开记事本”相关的内容」 because every
        turn was padded with a sentence about the 资料库, and a small model answers the
        framing it is handed rather than the question sitting above it.
        """
        store = _store(tmp_path)
        knowledge = _knowledge_service(store, _vector(store), tmp_path)
        client = _RecordingClient()
        service = ChatService(
            lambda: client,
            knowledge_provider=lambda: knowledge,
            tool_provider=lambda: _registry(tmp_path, knowledge=knowledge),
            pair_provider=lambda: BUNDLED,
            session_id="t",
        )
        service.start()
        try:
            service.ask("打开记事本")
            asked = client.calls[-1][-1].content
            assert "资料库" not in asked, f"本机操作被框成了资料问题：{asked}"

            service.ask("我上次说的那个方案怎么落地")
            remembered = client.calls[-1][-1].content
            assert "knowledge_search" in remembered, "资料类的问题仍然该给回查的指路"
        finally:
            store.stop()

    def test_the_pointer_is_withheld_when_no_tool_table_goes_out(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        knowledge = _knowledge_service(store, _vector(store), tmp_path)
        client = _RecordingClient()
        service = ChatService(
            lambda: client,
            knowledge_provider=lambda: knowledge,
            pair_provider=lambda: BUNDLED,
            session_id="t",
        )
        service.start()
        try:
            service.ask("我上次说的那个方案怎么落地")
            assert client.calls[-1][-1].content == "我上次说的那个方案怎么落地"
        finally:
            store.stop()


class TestDegradationIsReported:
    def test_a_turn_that_carried_tools_and_used_none_says_model(self, tmp_path: Path) -> None:
        service, _client = _chat(tmp_path, BUNDLED)

        reply = service.ask("查看 CPU 和内存")

        assert reply.degraded == "model", "带了工具她没用，屏上要写这一条而不是「资料库没有」"
        assert reply.tools_used == ()

    def test_a_turn_that_needed_nothing_is_not_reported_as_degraded(self, tmp_path: Path) -> None:
        service, _client = _chat(tmp_path, BUNDLED)

        reply = service.ask("你好啊")

        assert reply.degraded == ""

    def test_the_report_reaches_the_page_as_one_nested_object(self, tmp_path: Path) -> None:
        service, _client = _chat(tmp_path, BUNDLED)

        payload = service.ask("查看 CPU 和内存").to_dict()

        assert payload["tools"] == {
            "count": 6,
            "used": [],
            "pack": "system",
            "degraded": "model",
        }
