"""``add_knowledge``: the write half of her corpus, and the gates around it.

Before r88 the knowledge base had exactly one tool and it was read-only, so
「去查一下，回头记到你资料里」 was answerable and then had nowhere to go. Long-term
memory holds one sentence *about* a fact, not the page it came from.

These tests are mostly about the edges of a tool that writes files from
model-authored text: where the file may go, what a hostile title does to it, and what
happens when the write half has nowhere to write.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import pytest

from jarvis.config.schema import KnowledgeSection
from jarvis.database import SqliteStore
from jarvis.knowledge import KnowledgeService
from jarvis.knowledge.service import _note_stem
from jarvis.tools.builtins import assistant_tools
from tests.test_recall_tools import _store, _vector


class _Section:
    """A ``KnowledgeSection``-shaped value; the real one is built by the config loader."""

    def __init__(self, enabled: bool = True) -> None:
        self.enabled = enabled
        self.chunk_size = 600
        self.chunk_overlap = 120
        self.top_k = 5
        self.min_score = 0.1
        self.max_file_mb = 20


def _service(
    store: SqliteStore, tmp_path: Path, *, notes: Path | None, enabled: bool = True
) -> KnowledgeService:
    service = KnowledgeService(
        store,
        _vector(store),
        lambda: cast(KnowledgeSection, _Section(enabled)),
        notes_dir=notes,
    )
    service.start()
    return service


def _handler(service: KnowledgeService) -> Any:
    for spec, handler in assistant_tools.build(knowledge=service):
        if spec.name == "add_knowledge":
            return handler
    raise AssertionError("add_knowledge 没被注册")


@pytest.fixture
def pair(tmp_path: Path) -> Iterator[KnowledgeService]:
    store = _store(tmp_path)
    yield _service(store, tmp_path, notes=tmp_path / "笔记")
    store.stop()


class TestWritingANote:
    def test_a_note_comes_back_out_of_a_later_search(self, pair: KnowledgeService) -> None:
        out = _handler(pair)(
            {
                "title": "打印机换墨流程",
                "text": "先关机，打开后盖，按住旧墨盒左侧的卡扣往外拔。",
                "source": "用户口述",
            }
        )
        assert "已存进知识库" in out and "knowledge_search" in out
        found = pair.retrieve("墨盒 卡扣", top_k=3)
        assert found and "后盖" in found[0].text

    def test_the_same_title_updates_instead_of_piling_up(self, pair: KnowledgeService) -> None:
        handler = _handler(pair)
        handler({"title": "端口表", "text": "28181 是自带的"})
        again = handler({"title": "端口表", "text": "28181 自带，28080 是用户自己起的"})
        assert "更新" in again
        notes = pair.notes_dir
        assert notes is not None
        assert len(list(Path(notes).glob("*.md"))) == 1, "同名笔记该换掉，不该越攒越多"

    def test_the_source_line_survives_into_the_stored_page(self, pair: KnowledgeService) -> None:
        _handler(pair)({"title": "Qwen3 上下文", "text": "32768", "source": "https://qwen.ai"})
        hits = pair.retrieve("32768", top_k=2)
        assert hits and "https://qwen.ai" in hits[0].text

    def test_a_note_without_a_body_is_refused_and_nothing_is_written(
        self, pair: KnowledgeService
    ) -> None:
        notes = pair.notes_dir
        out = _handler(pair)({"title": "空的一条", "text": "   \n  "})
        assert "没有要存" in out
        assert notes is not None and list(Path(notes).glob("*.md")) == []

    def test_a_missing_title_is_refused(self, pair: KnowledgeService) -> None:
        assert "标题" in _handler(pair)({"title": "", "text": "内容"})

    def test_it_is_caution_and_carries_no_permission_gate(self, pair: KnowledgeService) -> None:
        """CAUTION = a side effect, nothing irreversible. DANGEROUS would be a lie that
        also breaks it: that path demands a per-call ``confirmed`` the chat loop cannot
        supply, so the tool would be advertised and then refuse forever."""
        for spec, _ in assistant_tools.build(knowledge=pair):
            if spec.name == "add_knowledge":
                assert spec.risk.value == "caution"
                assert spec.permissions == frozenset()


class TestTheFilenameIsNotModelControlled:
    @pytest.mark.parametrize(
        "title",
        [
            "../../Windows/System32/kernel32",
            "C:\\Users\\public\\启动.bat",
            "..\\..\\..\\escape",
            "a/b/c/d/e/f",
            '<>:"|?*nul',
            "   ",
            "",
            "点号结尾...",
            "x" * 400,
        ],
    )
    def test_a_hostile_title_stays_one_filename(self, title: str) -> None:
        stem = _note_stem(title)
        assert "/" not in stem and "\\" not in stem and ":" not in stem
        assert Path(stem).name == stem, "它只能是文件名，不能是一段路径"
        assert len(stem) <= 100

    def test_two_titles_that_clean_alike_still_get_two_files(self) -> None:
        """Otherwise 「记一下 A」 and 「记一下 A?」 silently overwrite each other."""
        assert _note_stem("端口:28181") != _note_stem("端口?28181")

    def test_the_same_title_always_lands_on_the_same_file(self) -> None:
        assert _note_stem("打印机换墨流程") == _note_stem("打印机换墨流程")

    def test_chinese_titles_stay_readable(self) -> None:
        """Stripping non-ASCII would collapse every note this program writes to one name."""
        assert _note_stem("端口对照表").startswith("端口对照表-")


class TestTheGate:
    def test_no_notes_directory_means_no_tool_at_all(self, tmp_path: Path) -> None:
        """She is not offered a write tool that can only ever answer 「没地方写」."""
        store = _store(tmp_path)
        service = _service(store, tmp_path, notes=None)
        try:
            names = {spec.name for spec, _ in assistant_tools.build(knowledge=service)}
            assert "knowledge_search" in names
            assert "add_knowledge" not in names
        finally:
            store.stop()

    def test_a_switched_off_base_offers_neither(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        service = _service(store, tmp_path, notes=tmp_path / "笔记", enabled=False)
        try:
            assert assistant_tools.build(knowledge=service) == []
        finally:
            store.stop()

    def test_ingest_text_refuses_rather_than_choosing_a_path(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        service = _service(store, tmp_path, notes=None)
        try:
            result = service.ingest_text("标题", "内容")
            assert not result.ok and "notes_dir" in result.error
        finally:
            store.stop()
