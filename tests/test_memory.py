"""Tests for long-term memory (jarvis.memory)."""

from __future__ import annotations

from collections.abc import Callable, Iterator, Sequence
from pathlib import Path

import pytest

from jarvis.config.schema import MemorySection, VectorSection
from jarvis.core.exceptions import JarvisError, LlmError
from jarvis.database import SqliteStore
from jarvis.llm.client import LlmClient
from jarvis.llm.types import ChatMessage, ChatResponse, GenerationOptions, StreamChunk
from jarvis.memory import (
    MIGRATIONS,
    MemoryExtractor,
    MemoryKind,
    MemoryRepository,
    MemoryScope,
    MemoryService,
    looks_worth_extracting,
)
from jarvis.vector import HashingEmbedder, VectorService


class ScriptedLlm:
    """Returns canned replies in order, recording every prompt it saw."""

    def __init__(self, *replies: str) -> None:
        self._replies = list(replies)
        self.calls: list[list[ChatMessage]] = []

    @property
    def provider_name(self) -> str:
        return "scripted"

    @property
    def model(self) -> str:
        return "scripted"

    def complete(
        self, messages: Sequence[ChatMessage], *, options: GenerationOptions | None = None
    ) -> ChatResponse:
        self.calls.append(list(messages))
        if not self._replies:
            raise LlmError("no scripted reply left")
        return ChatResponse(content=self._replies.pop(0), model="scripted")

    def stream(
        self, messages: Sequence[ChatMessage], *, options: GenerationOptions | None = None
    ) -> Iterator[StreamChunk]:
        yield from ()


def _vector(tmp_path: Path) -> tuple[SqliteStore, VectorService]:
    store = SqliteStore(tmp_path / "m.db")
    store.start()
    section = VectorSection.from_mapping(
        {
            "embedding": {
                "engine": "hashing",
                "dimension": 256,
                "base_url": "https://x.test/v1",
                "model": "m",
                "api_key_env": "NOPE",
                "timeout_seconds": 5.0,
                "batch_size": 8,
            }
        }
    )
    service = VectorService(store, lambda: section, embedder=HashingEmbedder(dimension=256))
    service.start()
    return store, service


def _memory_section(**overrides: object) -> MemorySection:
    raw: dict[str, object] = {
        "enabled": True,
        "max_recall": 5,
        "min_score": 0.15,
        "history_turns": 10,
        "auto_extract": True,
        "max_extract": 3,
        "summarize_after_turns": 4,
        "max_memories": 500,
    }
    raw.update(overrides)
    return MemorySection.from_mapping(raw)


def _service(tmp_path: Path, llm: LlmClient | None = None, **overrides: object) -> MemoryService:
    store, vector = _vector(tmp_path)
    section = _memory_section(**overrides)
    provider: Callable[[], LlmClient] | None = (lambda: llm) if llm is not None else None
    service = MemoryService(store, vector, lambda: section, llm_provider=provider)
    service.start()
    return service


class TestMarkerFilter:
    def test_first_person_turns_pass(self) -> None:
        assert looks_worth_extracting("我叫张三")

    def test_remember_commands_pass(self) -> None:
        assert looks_worth_extracting("记住我下周要出差")

    def test_english_markers_pass(self) -> None:
        assert looks_worth_extracting("my name is Alice")

    def test_small_talk_is_filtered_out(self) -> None:
        """The filter is what keeps extraction from costing a model call per turn."""
        assert not looks_worth_extracting("现在几点")
        assert not looks_worth_extracting("讲个笑话")

    def test_filter_is_over_inclusive_by_design(self) -> None:
        """A false positive costs one cheap request; a false negative silently
        loses a memory the user expected the assistant to keep."""
        assert looks_worth_extracting("我今天问了什么")


class TestMemoryExtractor:
    def test_returns_nothing_for_a_filtered_turn(self) -> None:
        llm = ScriptedLlm("[]")
        assert MemoryExtractor(llm).extract("现在几点") == []
        assert llm.calls == []

    def test_parses_a_well_formed_array(self) -> None:
        llm = ScriptedLlm('[{"content": "用户的名字是张三", "kind": "fact", "importance": 0.9}]')
        memories = MemoryExtractor(llm).extract("我叫张三")
        assert len(memories) == 1
        assert memories[0].content == "用户的名字是张三"
        assert memories[0].kind is MemoryKind.FACT
        assert memories[0].importance == pytest.approx(0.9)

    def test_tolerates_a_chatty_preamble(self) -> None:
        """Models add prose around JSON; the reply is searched for the array
        rather than required to *be* the array."""
        llm = ScriptedLlm('好的，这是结果：\n[{"content": "用户住在北京"}]\n以上。')
        memories = MemoryExtractor(llm).extract("我住在北京")
        assert [memory.content for memory in memories] == ["用户住在北京"]

    def test_unknown_kind_falls_back_to_fact(self) -> None:
        llm = ScriptedLlm('[{"content": "x", "kind": "nonsense"}]')
        assert MemoryExtractor(llm).extract("我的 x")[0].kind is MemoryKind.FACT

    def test_out_of_range_importance_is_clamped(self) -> None:
        llm = ScriptedLlm(
            '[{"content": "x", "importance": 42}, {"content": "y", "importance": -3}]'
        )
        memories = MemoryExtractor(llm).extract("我的 x")
        assert [memory.importance for memory in memories] == [1.0, 0.0]

    def test_non_numeric_importance_falls_back(self) -> None:
        llm = ScriptedLlm('[{"content": "x", "importance": "high"}]')
        assert MemoryExtractor(llm).extract("我的 x")[0].importance == pytest.approx(0.5)

    def test_max_items_is_enforced(self) -> None:
        llm = ScriptedLlm(
            '[{"content": "a"}, {"content": "b"}, {"content": "c"}, {"content": "d"}]'
        )
        assert len(MemoryExtractor(llm, max_items=2).extract("我的 a")) == 2

    def test_max_items_zero_skips_the_call(self) -> None:
        llm = ScriptedLlm("[]")
        assert MemoryExtractor(llm, max_items=0).extract("我叫张三") == []
        assert llm.calls == []

    @pytest.mark.parametrize("reply", ["not json at all", "[1, 2, 3]", '{"a": 1}', "[{"])
    def test_malformed_replies_yield_no_memories(self, reply: str) -> None:
        """A parse failure is a missed memory, not a broken conversation."""
        assert MemoryExtractor(ScriptedLlm(reply)).extract("我叫张三") == []

    def test_blank_content_is_dropped(self) -> None:
        llm = ScriptedLlm('[{"content": "   "}, {"content": "用户的名字是李四"}]')
        assert [memory.content for memory in MemoryExtractor(llm).extract("我叫")] == [
            "用户的名字是李四"
        ]

    def test_llm_failure_is_swallowed(self) -> None:
        """This runs on the voice path: an exception would look like the
        assistant going silent."""

        class Exploding:
            provider_name = "boom"
            model = "boom"

            def complete(self, messages: Sequence[ChatMessage], **_: object) -> ChatResponse:
                raise LlmError("no key")

            def stream(self, *_: object, **__: object) -> Iterator[StreamChunk]:
                yield from ()

        assert MemoryExtractor(Exploding()).extract("我叫张三") == []


class TestMemoryRepository:
    def _repo(self, tmp_path: Path) -> MemoryRepository:
        store, _ = _vector(tmp_path)
        store.migrate("memory", MIGRATIONS)
        return MemoryRepository(store)

    def test_upsert_deduplicates(self, tmp_path: Path) -> None:
        """Saying "我叫张三" five times must not create five memories that then
        crowd out everything else in a top-5 recall."""
        repo = self._repo(tmp_path)
        for _ in range(5):
            repo.upsert(
                scope=MemoryScope.USER,
                kind=MemoryKind.FACT,
                content="用户的名字是张三",
                source="user",
                importance=0.5,
                pinned=False,
            )
        assert repo.count(MemoryScope.USER) == 1

    def test_repeating_a_fact_raises_its_importance(self, tmp_path: Path) -> None:
        """Repeating something is evidence it matters; overwriting a 0.9 with a
        fresh default 0.5 would be a regression."""
        repo = self._repo(tmp_path)
        repo.upsert(
            scope=MemoryScope.USER,
            kind=MemoryKind.FACT,
            content="x",
            source="a",
            importance=0.9,
            pinned=False,
        )
        record = repo.upsert(
            scope=MemoryScope.USER,
            kind=MemoryKind.FACT,
            content="x",
            source="b",
            importance=0.2,
            pinned=False,
        )
        assert record.importance == pytest.approx(0.9)
        assert record.source == "b"

    def test_pinned_survives_a_weaker_repeat(self, tmp_path: Path) -> None:
        repo = self._repo(tmp_path)
        repo.upsert(
            scope=MemoryScope.USER,
            kind=MemoryKind.PREFERENCE,
            content="p",
            source="a",
            importance=0.5,
            pinned=True,
        )
        record = repo.upsert(
            scope=MemoryScope.USER,
            kind=MemoryKind.PREFERENCE,
            content="p",
            source="b",
            importance=0.5,
            pinned=False,
        )
        assert record.pinned is True

    def test_same_content_different_kind_is_a_separate_memory(self, tmp_path: Path) -> None:
        repo = self._repo(tmp_path)
        repo.upsert(
            scope=MemoryScope.USER,
            kind=MemoryKind.FACT,
            content="x",
            source="a",
            importance=0.5,
            pinned=False,
        )
        repo.upsert(
            scope=MemoryScope.USER,
            kind=MemoryKind.EPISODE,
            content="x",
            source="a",
            importance=0.5,
            pinned=False,
        )
        assert repo.count(MemoryScope.USER) == 2

    def test_scopes_are_isolated(self, tmp_path: Path) -> None:
        repo = self._repo(tmp_path)
        repo.upsert(
            scope=MemoryScope.USER,
            kind=MemoryKind.FACT,
            content="x",
            source="a",
            importance=0.5,
            pinned=False,
        )
        repo.upsert(
            scope=MemoryScope.SESSION,
            kind=MemoryKind.FACT,
            content="x",
            source="a",
            importance=0.5,
            pinned=False,
        )
        assert repo.count(MemoryScope.USER) == 1
        assert repo.count(MemoryScope.SESSION) == 1
        assert repo.count() == 2

    def test_all_for_filters_by_kind_and_orders_by_importance(self, tmp_path: Path) -> None:
        repo = self._repo(tmp_path)
        repo.upsert(
            scope=MemoryScope.USER,
            kind=MemoryKind.FACT,
            content="low",
            source="a",
            importance=0.1,
            pinned=False,
        )
        repo.upsert(
            scope=MemoryScope.USER,
            kind=MemoryKind.FACT,
            content="high",
            source="a",
            importance=0.9,
            pinned=False,
        )
        repo.upsert(
            scope=MemoryScope.USER,
            kind=MemoryKind.EPISODE,
            content="ep",
            source="a",
            importance=0.99,
            pinned=False,
        )
        facts = repo.all_for(MemoryScope.USER, kinds=[MemoryKind.FACT])
        assert [record.content for record in facts] == ["high", "low"]

    def test_keyword_search_matches_substrings(self, tmp_path: Path) -> None:
        repo = self._repo(tmp_path)
        for content in ("用户的订单号是 A12345", "用户喜欢喝咖啡"):
            repo.upsert(
                scope=MemoryScope.USER,
                kind=MemoryKind.FACT,
                content=content,
                source="a",
                importance=0.5,
                pinned=False,
            )
        hits = repo.keyword_search(MemoryScope.USER, ["A12345"], limit=5)
        assert [record.content for record in hits] == ["用户的订单号是 A12345"]

    def test_keyword_search_with_no_tokens_is_empty(self, tmp_path: Path) -> None:
        repo = self._repo(tmp_path)
        assert repo.keyword_search(MemoryScope.USER, [], limit=5) == []

    def test_touch_increments_the_access_count(self, tmp_path: Path) -> None:
        repo = self._repo(tmp_path)
        record = repo.upsert(
            scope=MemoryScope.USER,
            kind=MemoryKind.FACT,
            content="x",
            source="a",
            importance=0.5,
            pinned=False,
        )
        repo.touch([record.memory_id])
        repo.touch([record.memory_id])
        assert repo.get(record.memory_id).access_count == 2  # type: ignore[union-attr]

    def test_touch_with_no_ids_is_a_no_op(self, tmp_path: Path) -> None:
        repo = self._repo(tmp_path)
        repo.touch([])

    def test_delete_and_delete_scope(self, tmp_path: Path) -> None:
        repo = self._repo(tmp_path)
        record = repo.upsert(
            scope=MemoryScope.USER,
            kind=MemoryKind.FACT,
            content="x",
            source="a",
            importance=0.5,
            pinned=False,
        )
        assert repo.delete(record.memory_id) is True
        assert repo.delete(record.memory_id) is False
        repo.upsert(
            scope=MemoryScope.USER,
            kind=MemoryKind.FACT,
            content="y",
            source="a",
            importance=0.5,
            pinned=False,
        )
        assert repo.delete_scope(MemoryScope.USER) == 1

    def test_prune_keeps_pinned_and_most_used(self, tmp_path: Path) -> None:
        """A memory nobody ever recalled goes before one that keeps being used."""
        repo = self._repo(tmp_path)
        for index in range(5):
            repo.upsert(
                scope=MemoryScope.USER,
                kind=MemoryKind.FACT,
                content=f"m{index}",
                source="a",
                importance=0.5,
                pinned=False,
            )
        keeper = repo.upsert(
            scope=MemoryScope.USER,
            kind=MemoryKind.PREFERENCE,
            content="pinned",
            source="a",
            importance=0.5,
            pinned=True,
        )
        used = repo.find(scope=MemoryScope.USER, kind=MemoryKind.FACT, content="m4")
        assert used is not None
        repo.touch([used.memory_id])
        assert repo.prune(MemoryScope.USER, keep=3) == 3
        remaining = {record.content for record in repo.all_for(MemoryScope.USER)}
        assert "pinned" in remaining
        assert "m4" in remaining
        assert keeper.pinned is True

    def test_prune_is_a_no_op_below_the_cap(self, tmp_path: Path) -> None:
        repo = self._repo(tmp_path)
        repo.upsert(
            scope=MemoryScope.USER,
            kind=MemoryKind.FACT,
            content="x",
            source="a",
            importance=0.5,
            pinned=False,
        )
        assert repo.prune(MemoryScope.USER, keep=10) == 0

    def test_count_by_kind(self, tmp_path: Path) -> None:
        repo = self._repo(tmp_path)
        repo.upsert(
            scope=MemoryScope.USER,
            kind=MemoryKind.FACT,
            content="a",
            source="s",
            importance=0.5,
            pinned=False,
        )
        repo.upsert(
            scope=MemoryScope.USER,
            kind=MemoryKind.EPISODE,
            content="b",
            source="s",
            importance=0.5,
            pinned=False,
        )
        assert repo.count_by_kind() == {"episode": 1, "fact": 1}

    def test_unknown_kind_in_the_database_degrades(self, tmp_path: Path) -> None:
        """A row written by a newer JARVIS must not make recall crash."""
        repo = self._repo(tmp_path)
        record = repo.upsert(
            scope=MemoryScope.USER,
            kind=MemoryKind.FACT,
            content="x",
            source="a",
            importance=0.5,
            pinned=False,
        )
        with repo.store.connection() as connection:
            connection.execute("UPDATE memory_records SET kind = 'future'")
        assert repo.get(record.memory_id).kind is MemoryKind.FACT  # type: ignore[union-attr]


class TestConversationTurns:
    def _repo(self, tmp_path: Path) -> MemoryRepository:
        store, _ = _vector(tmp_path)
        store.migrate("memory", MIGRATIONS)
        return MemoryRepository(store)

    def test_recent_turns_returns_the_newest_not_the_first(self, tmp_path: Path) -> None:
        """``ORDER BY id ASC LIMIT n`` would return the *start* of a long session
        rather than what was just said."""
        repo = self._repo(tmp_path)
        for index in range(10):
            repo.append_turn("s1", "user", f"t{index}")
        turns = repo.recent_turns("s1", limit=3)
        assert [turn.content for turn in turns] == ["t7", "t8", "t9"]

    def test_sessions_are_isolated(self, tmp_path: Path) -> None:
        repo = self._repo(tmp_path)
        repo.append_turn("s1", "user", "a")
        repo.append_turn("s2", "user", "b")
        assert repo.count_turns("s1") == 1
        assert repo.count_turns("s2") == 1

    def test_sessions_summary(self, tmp_path: Path) -> None:
        repo = self._repo(tmp_path)
        repo.append_turn("s1", "user", "a")
        repo.append_turn("s1", "assistant", "b")
        summary = repo.sessions()
        assert summary[0]["session_id"] == "s1"
        assert summary[0]["turns"] == 2

    def test_clear_session(self, tmp_path: Path) -> None:
        repo = self._repo(tmp_path)
        repo.append_turn("s1", "user", "a")
        assert repo.clear_session("s1") == 1
        assert repo.count_turns("s1") == 0

    def test_recent_turns_with_zero_limit(self, tmp_path: Path) -> None:
        repo = self._repo(tmp_path)
        repo.append_turn("s1", "user", "a")
        assert repo.recent_turns("s1", limit=0) == []


class TestMemoryService:
    def test_remember_stores_and_indexes(self, tmp_path: Path) -> None:
        service = _service(tmp_path)
        record = service.remember("用户的名字是张三", kind=MemoryKind.FACT, importance=0.9)
        assert record.memory_id > 0
        assert service.stats()["total"] == 1

    def test_blank_content_is_refused(self, tmp_path: Path) -> None:
        service = _service(tmp_path)
        with pytest.raises(JarvisError, match="不能为空"):
            service.remember("   ")

    def test_operations_before_start_raise(self, tmp_path: Path) -> None:
        store, vector = _vector(tmp_path)
        service = MemoryService(store, vector, _memory_section)
        with pytest.raises(JarvisError, match="未启动"):
            service.remember("x")

    def test_recall_finds_a_semantic_match(self, tmp_path: Path) -> None:
        service = _service(tmp_path)
        service.remember("用户的名字是张三", kind=MemoryKind.FACT)
        service.remember("用户喜欢喝美式咖啡", kind=MemoryKind.PREFERENCE)
        hits = service.recall("用户叫什么名字", top_k=1, min_score=0.0)
        assert hits
        assert "张三" in hits[0].record.content

    def test_recall_finds_an_exact_string_the_embedder_blurs(self, tmp_path: Path) -> None:
        """The keyword path exists precisely for queries like an order number."""
        service = _service(tmp_path)
        service.remember("用户的订单号是 ZX998877", kind=MemoryKind.FACT)
        service.remember("用户喜欢喝美式咖啡", kind=MemoryKind.PREFERENCE)
        hits = service.recall("ZX998877", min_score=0.0)
        assert any("ZX998877" in hit.record.content for hit in hits)
        assert any("keyword" in hit.matched_by for hit in hits)

    def test_pinned_memories_always_come_back(self, tmp_path: Path) -> None:
        """The operator said "always"; ranking must not be able to veto that."""
        service = _service(tmp_path)
        service.remember("用户要求回答尽量简短", kind=MemoryKind.PREFERENCE, pinned=True)
        hits = service.recall("今天天气怎么样", min_score=0.0)
        assert any("简短" in hit.record.content for hit in hits)

    def test_recall_on_empty_memory_is_empty(self, tmp_path: Path) -> None:
        assert _service(tmp_path).recall("anything") == []

    def test_recall_with_blank_query_is_empty(self, tmp_path: Path) -> None:
        service = _service(tmp_path)
        service.remember("x")
        assert service.recall("   ") == []

    def test_min_score_floor_is_applied(self, tmp_path: Path) -> None:
        service = _service(tmp_path)
        service.remember("用户喜欢喝美式咖啡", kind=MemoryKind.PREFERENCE)
        assert service.recall("完全无关的话题", min_score=0.99) == []

    def test_recall_updates_the_access_count(self, tmp_path: Path) -> None:
        service = _service(tmp_path)
        service.remember("用户的名字是张三", kind=MemoryKind.FACT)
        service.recall("张三", min_score=0.0)
        records = service.list_memories()
        assert any(record.access_count > 0 for record in records)

    def test_kind_filter(self, tmp_path: Path) -> None:
        service = _service(tmp_path)
        service.remember("用户喜欢喝美式咖啡", kind=MemoryKind.PREFERENCE)
        hits = service.recall("咖啡", min_score=0.0, kinds=[MemoryKind.EPISODE])
        assert hits == []

    def test_forget_removes_from_both_stores(self, tmp_path: Path) -> None:
        service = _service(tmp_path)
        record = service.remember("用户的名字是张三")
        assert service.forget(record.memory_id) is True
        assert service.recall("张三", min_score=0.0) == []
        assert service.forget(record.memory_id) is False

    def test_forget_scope(self, tmp_path: Path) -> None:
        service = _service(tmp_path)
        service.remember("a")
        service.remember("b")
        assert service.forget_scope(MemoryScope.USER) == 2
        assert service.recall("a", min_score=0.0) == []

    def test_profile_block_lists_preferences_first(self, tmp_path: Path) -> None:
        service = _service(tmp_path)
        service.remember("用户的名字是张三", kind=MemoryKind.FACT)
        service.remember("用户要求回答尽量简短", kind=MemoryKind.PREFERENCE)
        block = service.profile_block()
        assert block.index("[偏好]") < block.index("[事实]")

    def test_profile_block_is_empty_without_memories(self, tmp_path: Path) -> None:
        """Callers concatenate it unconditionally, so "nothing" must be ""."""
        assert _service(tmp_path).profile_block() == ""

    def test_history_replays_turns_as_chat_messages(self, tmp_path: Path) -> None:
        service = _service(tmp_path)
        service.record_turn("s1", "user", "你好")
        service.record_turn("s1", "assistant", "你好，有什么可以帮您")
        history = service.history("s1")
        assert [message.content for message in history] == ["你好", "你好，有什么可以帮您"]

    def test_unknown_role_degrades_to_user(self, tmp_path: Path) -> None:
        service = _service(tmp_path)
        service.record_turn("s1", "wizard", "hi")
        assert service.history("s1")[0].role.value == "user"

    def test_observe_turn_records_without_a_model(self, tmp_path: Path) -> None:
        """No LLM means no extraction, but the turn is still recorded."""
        service = _service(tmp_path)
        assert service.observe_turn("s1", "我叫张三", "好的") == []
        assert len(service.history("s1")) == 2

    def test_observe_turn_extracts_when_a_model_is_available(self, tmp_path: Path) -> None:
        llm = ScriptedLlm('[{"content": "用户的名字是张三", "kind": "fact", "importance": 0.9}]')
        service = _service(tmp_path, llm)
        stored = service.observe_turn("s1", "我叫张三", "记住了")
        assert [record.content for record in stored] == ["用户的名字是张三"]
        assert service.recall("我叫什么", min_score=0.0)

    def test_observe_turn_respects_auto_extract_off(self, tmp_path: Path) -> None:
        llm = ScriptedLlm('[{"content": "用户的名字是张三"}]')
        service = _service(tmp_path, llm, auto_extract=False)
        assert service.observe_turn("s1", "我叫张三") == []
        assert llm.calls == []

    def test_observe_turn_skips_small_talk(self, tmp_path: Path) -> None:
        llm = ScriptedLlm("[]")
        service = _service(tmp_path, llm)
        assert service.observe_turn("s1", "现在几点") == []
        assert llm.calls == []

    def test_summarize_session_needs_enough_turns(self, tmp_path: Path) -> None:
        llm = ScriptedLlm("用户咨询了备份方案")
        service = _service(tmp_path, llm)
        for index in range(3):
            service.record_turn("s1", "user", f"t{index}")
        assert service.summarize_session("s1") is None
        assert llm.calls == []

    def test_summarize_session_stores_a_summary_memory(self, tmp_path: Path) -> None:
        llm = ScriptedLlm("用户咨询了备份方案\n决定用 rsync")
        service = _service(tmp_path, llm, summarize_after_turns=2)
        service.record_turn("s1", "user", "怎么备份")
        service.record_turn("s1", "assistant", "可以用 rsync")
        record = service.summarize_session("s1")
        assert record is not None
        assert record.kind is MemoryKind.SUMMARY

    def test_summarize_session_without_a_model_returns_none(self, tmp_path: Path) -> None:
        service = _service(tmp_path, summarize_after_turns=1)
        service.record_turn("s1", "user", "x")
        assert service.summarize_session("s1") is None

    def test_clear_session_keeps_extracted_memories(self, tmp_path: Path) -> None:
        """A summary is a retrieval aid, not a licence to destroy the record."""
        service = _service(tmp_path)
        service.remember("用户的名字是张三")
        service.record_turn("s1", "user", "hi")
        assert service.clear_session("s1") == 1
        assert service.stats()["total"] == 1

    def test_prune_reindexes(self, tmp_path: Path) -> None:
        service = _service(tmp_path, max_memories=2)
        for index in range(6):
            service.remember(f"用户的事实 {index}", kind=MemoryKind.FACT)
        removed = service.prune(keep=2)
        assert removed == 4
        assert service.stats()["total"] == 2

    def test_stats_reports_by_kind_and_vector(self, tmp_path: Path) -> None:
        service = _service(tmp_path)
        service.remember("a", kind=MemoryKind.FACT)
        stats = service.stats()
        assert stats["running"] is True
        assert stats["by_kind"] == {"fact": 1}
        assert "vector" in stats

    def test_index_failure_does_not_lose_the_memory(self, tmp_path: Path) -> None:
        """A cloud embedder with no key is a normal state on a fresh install;
        the memory is still stored and still findable by keyword."""

        class BrokenVector:
            name = "broken"

            def index(self, collection: str, entries: Sequence[object]) -> int:
                raise JarvisError("no key")

            def search(self, *_: object, **__: object) -> list[object]:
                return []

            def forget(self, *_: object, **__: object) -> int:
                return 0

            def drop(self, *_: object, **__: object) -> int:
                return 0

            def stats(self) -> dict[str, object]:
                return {"running": False}

        store, _ = _vector(tmp_path)
        service = MemoryService(store, BrokenVector(), _memory_section)  # type: ignore[arg-type]
        service.start()
        record = service.remember("用户的名字是张三")
        assert record.memory_id > 0
        assert service.stats()["total"] == 1
