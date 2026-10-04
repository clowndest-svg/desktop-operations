"""P0-1: the assistant may look her own memory and corpus up, mid-turn.

Before these tools existed, memory and knowledge were only ever *pushed* at the model
once per turn, using the user's sentence as the search term. "接着上次那个说" therefore
retrieved "接着上次那个说", found nothing, and the model concluded there was nothing to
find. These tests pin the pull half: the tools exist when (and only when) the store
behind them can answer, they go through the *real* registry so argument validation is
part of the test, and one round trip through ``ChatService`` shows up in ``tools_used``.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any, cast

import pytest

from jarvis.app.chat_service import ChatService
from jarvis.config.schema import KnowledgeSection, MemorySection, ToolsSection, VectorSection
from jarvis.core.exceptions import JarvisError
from jarvis.database import SqliteStore
from jarvis.knowledge import KnowledgeService
from jarvis.llm.types import ChatMessage, ChatResponse, GenerationOptions, ToolCall
from jarvis.memory import MemoryService
from jarvis.tools.builtins import assistant_tools
from jarvis.tools.disk_cleaner import DiskCleaner
from jarvis.tools.monitor import SystemMonitor
from jarvis.tools.registry import ToolRegistry
from jarvis.tools.service import ToolService
from jarvis.vector import HashingEmbedder, VectorService

MANUAL = """# 部署手册

数据库端口是 5433，不是默认的 5432。

备份窗口固定在每周日凌晨三点，由 deploy-backup 服务执行。
"""


def _tools_section() -> ToolsSection:
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


def _store(tmp_path: Path) -> SqliteStore:
    store = SqliteStore(tmp_path / "recall.db", journal_mode="MEMORY")
    store.start()
    return store


def _vector(store: SqliteStore, dimension: int = 256) -> VectorService:
    section = VectorSection.from_mapping(
        {
            "embedding": {
                "engine": "hashing",
                "dimension": dimension,
                "base_url": "https://x.test/v1",
                "model": "m",
                "api_key_env": "NOPE",
                "timeout_seconds": 5.0,
                "batch_size": 8,
            }
        }
    )
    service = VectorService(store, lambda: section, embedder=HashingEmbedder(dimension=dimension))
    service.start()
    return service


def _memory_service(store: SqliteStore, vector: VectorService) -> MemoryService:
    section = MemorySection.from_mapping(
        {
            "enabled": True,
            "max_recall": 5,
            "min_score": 0.15,
            "history_turns": 10,
            "auto_extract": False,
            "max_extract": 3,
            "summarize_after_turns": 0,
            "max_memories": 500,
        }
    )
    service = MemoryService(store, vector, lambda: section)
    service.start()
    return service


def _knowledge_section(*, enabled: bool = True) -> KnowledgeSection:
    return KnowledgeSection.from_mapping(
        {
            "enabled": enabled,
            "chunk_size": 600,
            "chunk_overlap": 120,
            "top_k": 5,
            "min_score": 0.1,
            "max_file_mb": 20,
        }
    )


def _manual(tmp_path: Path) -> Path:
    path = tmp_path / "手册.md"
    path.write_text(MANUAL, encoding="utf-8")
    return path


def _knowledge_service(
    store: SqliteStore, vector: VectorService, tmp_path: Path, *, enabled: bool = True
) -> KnowledgeService:
    service = KnowledgeService(store, vector, lambda: _knowledge_section(enabled=enabled))
    service.start()
    service.ingest(_manual(tmp_path))
    return service


@pytest.fixture
def stack(tmp_path: Path) -> Iterator[dict[str, Any]]:
    """A real database, a real vector index, a real registry -- nothing hand-wired."""
    store = _store(tmp_path)
    vector = _vector(store)
    memory = _memory_service(store, vector)
    knowledge = _knowledge_service(store, vector, tmp_path)
    registry = ToolRegistry(_tools_section)
    for spec, handler in assistant_tools.build(memory=memory, knowledge=knowledge):
        registry.register(spec, handler)
    yield {
        "store": store,
        "vector": vector,
        "memory": memory,
        "knowledge": knowledge,
        "registry": registry,
    }
    store.stop()


class TestRegistrationGates:
    def test_nothing_injected_advertises_no_recall(self) -> None:
        assert assistant_tools.build() == []

    def test_a_memory_service_that_never_started_is_not_offered(self) -> None:
        """``recall`` raises on every call once the service is stopped, so an advertised
        tool would answer "记忆服务未启动" to every question -- including the ones she
        could have answered from the block the chat service pushes."""
        stopped = _StoppedMemory()

        assert assistant_tools.build(memory=cast(Any, stopped)) == []

    def test_a_knowledge_base_the_operator_switched_off_is_not_offered(self) -> None:
        """A disabled base returns ``[]`` to everything, which the model would report
        as "你的资料里没有" about a corpus the operator merely switched off."""
        disabled = _DisabledKnowledge()

        assert assistant_tools.build(knowledge=cast(Any, disabled)) == []

    def test_the_three_tools_appear_once_the_stores_are_up(self, stack: dict[str, Any]) -> None:
        assert set(stack["registry"].names()) == {
            "memory_search",
            "remember",
            "knowledge_search",
        }

    def test_every_one_of_them_is_safe(self, stack: dict[str, Any]) -> None:
        """Reading her own notes and adding one row to her own database is not a
        machine-touching capability. If a parameter ever reaches for something else,
        this is where that has to be argued."""
        for spec in stack["registry"].specs():
            assert spec.risk.value == "safe", spec.name


class TestKnowledgeSearch:
    def test_a_fact_that_is_only_in_the_manual_comes_back_with_a_citation(
        self, stack: dict[str, Any]
    ) -> None:
        result = stack["registry"].invoke("knowledge_search", {"query": "数据库端口"})
        assert result.ok, result.error
        assert "5433" in result.output
        assert "手册.md" in result.output

    def test_a_second_query_with_the_model_s_own_words_finds_what_the_first_missed(
        self, stack: dict[str, Any]
    ) -> None:
        """This is the whole reason the tool exists: the automatic search on the raw
        sentence came up empty, and she gets to try again with a keyword she chose."""
        registry = stack["registry"]
        vague = registry.invoke("knowledge_search", {"query": "接着上次那个说"})
        assert vague.ok and "资料检索" not in vague.output
        specific = registry.invoke("knowledge_search", {"query": "备份窗口"})
        assert specific.ok and "凌晨三点" in specific.output

    def test_an_empty_result_tells_her_what_to_do_instead_of_stopping(
        self, stack: dict[str, Any]
    ) -> None:
        result = stack["registry"].invoke("knowledge_search", {"query": "恐龙化石拍卖行"})
        assert result.ok
        assert "换" in result.output and "没找到" in result.output

    def test_a_missing_query_is_refused_before_the_service_sees_it(
        self, stack: dict[str, Any]
    ) -> None:
        result = stack["registry"].invoke("knowledge_search", {})
        assert not result.ok and "缺少必填参数 query" in result.error

    def test_a_non_integer_limit_is_refused_by_the_validator(self, stack: dict[str, Any]) -> None:
        result = stack["registry"].invoke("knowledge_search", {"query": "端口", "limit": "三"})
        assert not result.ok and "limit" in result.error


class TestMemoryTools:
    def test_a_remembered_fact_is_found_again_through_the_tools(
        self, stack: dict[str, Any]
    ) -> None:
        wrote = stack["registry"].invoke(
            "remember", {"content": "用户用的是 Temurin JDK 17", "kind": "fact"}
        )
        assert wrote.ok, wrote.error
        found = stack["registry"].invoke("memory_search", {"query": "JDK"})
        assert found.ok and "Temurin JDK 17" in found.output

    def test_it_is_really_on_disk_and_not_only_in_the_process(self, stack: dict[str, Any]) -> None:
        """Acceptance criterion: a *new session* must still know it. A new session after
        a restart is a new service over the same database, which is what this builds."""
        stack["registry"].invoke("remember", {"content": "用户的显卡是 4070 Super"})
        reopened = _memory_service(stack["store"], stack["vector"])
        hits = reopened.recall("显卡")
        assert any("4070 Super" in hit.record.content for hit in hits)

    def test_a_preference_shows_up_in_the_block_that_is_replayed_every_turn(
        self, stack: dict[str, Any]
    ) -> None:
        stack["registry"].invoke(
            "remember", {"content": "用户希望回答尽量简短", "kind": "preference"}
        )
        assert "[偏好] 用户希望回答尽量简短" in stack["memory"].profile_block()

    def test_an_unknown_kind_is_refused_by_the_tool_with_the_choices_named(
        self, stack: dict[str, Any]
    ) -> None:
        """Refused by the handler rather than by the enum conversion, so the model gets the
        allowed list instead of ``ValueError: summary`` -- and the registry records it as a
        failed call, which is what puts "她想记一条但被拒了" in 最近动作."""
        result = stack["registry"].invoke("remember", {"content": "任何内容", "kind": "summary"})
        assert not result.ok and "preference" in result.error
        assert stack["memory"].list_memories() == []

    def test_blank_content_is_not_stored(self, stack: dict[str, Any]) -> None:
        result = stack["registry"].invoke("remember", {"content": "   "})
        assert result.ok and "没有要记的内容" in result.output
        assert stack["memory"].list_memories() == []

    def test_remembering_the_same_thing_twice_says_so(self, stack: dict[str, Any]) -> None:
        first = stack["registry"].invoke("remember", {"content": "用户住杭州"})
        again = stack["registry"].invoke("remember", {"content": "用户住杭州"})
        assert first.ok and "已记住" in first.output
        assert again.ok and "早就记住了" in again.output
        assert len(stack["memory"].list_memories()) == 1

    def test_a_memory_she_does_not_have_is_reported_as_absent(self, stack: dict[str, Any]) -> None:
        result = stack["registry"].invoke("memory_search", {"query": "我的自行车什么颜色"})
        assert result.ok and "没有" in result.output


class TestThroughTheChatDoor:
    """One real question through ``ChatService``, with the real ``ToolService``.

    ``tools_used`` is the evidence that matters here: a tool that is registered but never
    chosen by the model is a tool the operator will never see working.
    """

    def test_a_tool_call_the_model_makes_appears_in_tools_used(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        vector = _vector(store)
        knowledge = _knowledge_service(store, vector, tmp_path)
        service = ToolService(
            _tools_section,
            monitor_factory=lambda: SystemMonitor(top_processes=5),
            cleaner_factory=lambda: DiskCleaner(tmp_path / "audit.jsonl"),
            memory=_memory_service(store, vector),
            knowledge=knowledge,
        )
        service.start()
        try:
            client = _AsksForAToolThenAnswers()
            chat = ChatService(
                lambda: client,
                knowledge_provider=lambda: knowledge,
                tool_provider=lambda: service,
                session_id="t",
            )
            chat.start()
            reply = chat.ask("备份窗口是几点？")
            assert reply.error == "", reply.error
            assert "knowledge_search" in reply.tools_used
            assert "凌晨三点" in reply.answer
        finally:
            store.stop()

    def test_an_empty_automatic_search_points_at_the_tool_she_can_call(
        self, tmp_path: Path
    ) -> None:
        """The retrieval term used to be the user's own sentence, and nothing said the
        search had missed. Now the miss is visible and names the way out."""
        store = _store(tmp_path)
        vector = _vector(store)
        knowledge = _knowledge_service(store, vector, tmp_path)
        client = _RecordingClient("答")
        chat = ChatService(lambda: client, knowledge_provider=lambda: knowledge, session_id="t")
        chat.start()
        try:
            chat.ask("接着上次那个说")
            assert "knowledge_search" in client.calls[-1][-1].content
        finally:
            store.stop()

    def test_an_empty_knowledge_base_does_not_send_her_searching_nothing(
        self, tmp_path: Path
    ) -> None:
        store = _store(tmp_path)
        vector = _vector(store)
        empty = KnowledgeService(store, vector, lambda: _knowledge_section())
        empty.start()
        client = _RecordingClient("答")
        chat = ChatService(lambda: client, knowledge_provider=lambda: empty, session_id="t")
        chat.start()
        try:
            chat.ask("备份窗口是几点")
            assert "knowledge_search" not in client.calls[-1][-1].content
        finally:
            store.stop()


class _StoppedMemory:
    """A memory service that never started: only ``running`` is read before refusal."""

    running = False

    def recall(self, query: str, *, top_k: int = 0) -> list[Any]:
        raise JarvisError("记忆服务未启动")

    def remember(self, content: str, *, kind: str = "fact", source: str = "") -> Any:
        raise JarvisError("记忆服务未启动")


class _DisabledKnowledge:
    running = True
    enabled = False

    def retrieve(self, query: str, *, top_k: int = 0) -> list[Any]:
        return []


class _RecordingClient:
    """Answers with a fixed string and keeps every message list it was handed."""

    provider_name = "fake"
    model = "fake"

    def __init__(self, reply: str) -> None:
        self._reply = reply
        self.calls: list[list[ChatMessage]] = []

    def complete(
        self,
        messages: Sequence[ChatMessage],
        *,
        options: GenerationOptions | None = None,
    ) -> ChatResponse:
        del options
        self.calls.append(list(messages))
        return ChatResponse(content=self._reply, model="fake")

    def stream(
        self,
        messages: Sequence[ChatMessage],
        *,
        options: GenerationOptions | None = None,
    ) -> Any:
        del messages, options
        yield from ()


class _AsksForAToolThenAnswers:
    """Round one: call ``knowledge_search``. Round two: answer from what came back.

    The answer is read out of the tool result the real registry produced, so it can only
    come out right if the validator, the registry and the knowledge service all agreed.
    """

    provider_name = "fake"
    model = "fake"

    def __init__(self) -> None:
        self._round = 0

    def complete(
        self,
        messages: Sequence[ChatMessage],
        *,
        options: GenerationOptions | None = None,
    ) -> ChatResponse:
        del options
        self._round += 1
        if self._round == 1:
            return ChatResponse(
                content="",
                model="fake",
                tool_calls=(
                    ToolCall(id="c1", name="knowledge_search", arguments='{"query": "备份窗口"}'),
                ),
            )
        evidence = messages[-1].content
        if "凌晨三点" not in evidence:
            raise AssertionError(f"the tool result carried no backup window: {evidence!r}")
        return ChatResponse(content="备份窗口是每周日凌晨三点。", model="fake")

    def stream(
        self,
        messages: Sequence[ChatMessage],
        *,
        options: GenerationOptions | None = None,
    ) -> Any:
        del messages, options
        yield from ()
