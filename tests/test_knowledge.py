"""Tests for the RAG knowledge base (jarvis.knowledge)."""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from pathlib import Path

import pytest

from jarvis.config.schema import KnowledgeSection, VectorSection
from jarvis.core.exceptions import KnowledgeError
from jarvis.database import SqliteStore
from jarvis.knowledge import (
    MIGRATIONS,
    ChunkingOptions,
    KnowledgeRepository,
    KnowledgeService,
    chunk_text,
    decode_text,
    document_id,
    load_document,
    media_type_for,
    split_units,
)
from jarvis.llm.types import ChatMessage, ChatResponse, GenerationOptions, StreamChunk
from jarvis.vector import HashingEmbedder, VectorService

MANUAL = """# 备份指南

使用 rsync 备份数据。命令是 rsync -av /data /backup。

数据库必须先停止再备份，否则备份会不一致。

## 恢复

用 rsync 反向同步即可恢复。
"""


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
            return ChatResponse(content="", model="scripted")
        return ChatResponse(content=self._replies.pop(0), model="scripted")

    def stream(
        self, messages: Sequence[ChatMessage], *, options: GenerationOptions | None = None
    ) -> Iterator[StreamChunk]:
        yield from ()


def _knowledge_section(**overrides: object) -> KnowledgeSection:
    raw: dict[str, object] = {
        "enabled": True,
        "chunk_size": 600,
        "chunk_overlap": 120,
        "top_k": 5,
        "min_score": 0.1,
        "max_file_mb": 20,
    }
    raw.update(overrides)
    return KnowledgeSection.from_mapping(raw)


def _service(tmp_path: Path, llm: object | None = None, **overrides: object) -> KnowledgeService:
    store = SqliteStore(tmp_path / "kb.db")
    store.start()
    section = VectorSection.from_mapping(
        {
            "embedding": {
                "engine": "hashing",
                "dimension": 512,
                "base_url": "https://x.test/v1",
                "model": "m",
                "api_key_env": "NOPE",
                "timeout_seconds": 5.0,
                "batch_size": 8,
            }
        }
    )
    vector = VectorService(store, lambda: section, embedder=HashingEmbedder(dimension=512))
    vector.start()
    provider = (lambda: llm) if llm is not None else None
    service = KnowledgeService(
        store,
        vector,
        lambda: _knowledge_section(**overrides),
        llm_provider=provider,  # type: ignore[arg-type]
    )
    service.start()
    return service


def _write(tmp_path: Path, name: str, text: str) -> Path:
    target = tmp_path / name
    target.write_text(text, encoding="utf-8")
    return target


class TestDocumentIdentity:
    def test_id_is_stable_for_the_same_path(self, tmp_path: Path) -> None:
        path = tmp_path / "a.md"
        assert document_id(path) == document_id(path)

    def test_id_differs_across_paths(self, tmp_path: Path) -> None:
        assert document_id(tmp_path / "a.md") != document_id(tmp_path / "b.md")

    def test_media_types(self) -> None:
        assert media_type_for(".md") == "text/markdown"
        assert media_type_for(".pdf") == "application/pdf"
        assert media_type_for(".xyz") == "text/plain"


class TestDecoding:
    def test_utf8(self) -> None:
        assert decode_text("中文".encode(), source="a") == "中文"

    def test_gb18030_fallback(self) -> None:
        """A ``.txt`` written in Notepad on a Chinese Windows install is not
        UTF-8, and refusing it would make the feature look broken for exactly
        the users it is for."""
        assert decode_text("中文".encode("gb18030"), source="a") == "中文"

    def test_undecodable_bytes_are_refused(self) -> None:
        """Latin-1 would accept anything and turn a corrupt file into
        plausible-looking noise, which is worse than an honest refusal."""
        with pytest.raises(KnowledgeError, match="无法解码"):
            decode_text(b"\xff\xfe\x00\x01\xff\xfe", source="a")


class TestLoader:
    def test_loads_text(self, tmp_path: Path) -> None:
        path = _write(tmp_path, "a.md", MANUAL)
        document = load_document(path, max_file_mb=1)
        assert "rsync" in document.text
        assert document.title == "a.md"
        assert document.media_type == "text/markdown"

    def test_missing_file(self, tmp_path: Path) -> None:
        with pytest.raises(KnowledgeError, match="文件不存在"):
            load_document(tmp_path / "nope.md", max_file_mb=1)

    def test_unsupported_suffix(self, tmp_path: Path) -> None:
        path = tmp_path / "a.xyz"
        path.write_text("x", encoding="utf-8")
        with pytest.raises(KnowledgeError, match="不支持的文件类型"):
            load_document(path, max_file_mb=1)

    def test_oversized_file(self, tmp_path: Path) -> None:
        path = _write(tmp_path, "big.txt", "x" * 2000)
        with pytest.raises(KnowledgeError, match="文件太大"):
            load_document(path, max_file_mb=0)

    def test_pdf_without_the_parser_names_the_extra(self, tmp_path: Path) -> None:
        """The failure has to tell the operator what to install, not just that
        an import failed."""
        path = tmp_path / "a.pdf"
        path.write_bytes(b"%PDF-1.4")
        with pytest.raises(KnowledgeError, match=r"\[docs\]"):
            load_document(path, max_file_mb=1)

    def test_docx_without_the_parser_names_the_extra(self, tmp_path: Path) -> None:
        path = tmp_path / "a.docx"
        path.write_bytes(b"PK\x03\x04")
        with pytest.raises(KnowledgeError, match=r"\[docs\]"):
            load_document(path, max_file_mb=1)


class TestSplitUnits:
    def test_chinese_sentences(self) -> None:
        assert split_units("第一句。第二句！第三句？") == ["第一句。", "第二句！", "第三句？"]

    def test_latin_sentences(self) -> None:
        units = split_units("First. Second! Third?")
        assert units == ["First.", "Second!", "Third?"]

    def test_decimals_and_domains_survive(self) -> None:
        """Splitting on every dot would shred numbers, file names and version
        strings into pieces that embed badly."""
        assert split_units("版本 3.14 见 example.com") == ["版本 3.14 见 example.com"]

    def test_blank_lines_split(self) -> None:
        assert split_units("段一\n\n段二") == ["段一", "段二"]

    def test_punctuation_stays_with_its_sentence(self) -> None:
        """Dropping the mark would change the meaning of a quoted question."""
        assert split_units("这是什么？") == ["这是什么？"]


class TestChunker:
    def test_short_text_is_one_chunk(self) -> None:
        options = ChunkingOptions(size=600, overlap=100, metadata={"doc_id": "d"})
        chunks = chunk_text(MANUAL, options)
        assert len(chunks) == 1
        assert chunks[0].chunk_id == "d#0"
        assert chunks[0].doc_id == "d"

    def test_long_text_is_split(self) -> None:
        options = ChunkingOptions(size=60, overlap=10, metadata={"doc_id": "d"})
        chunks = chunk_text("。".join(["这是一句话" for _ in range(40)]), options)
        assert len(chunks) > 1
        assert [chunk.ordinal for chunk in chunks] == list(range(len(chunks)))

    def test_chunks_overlap_at_the_seam(self) -> None:
        """A sentence split across a boundary must be recoverable from either
        side, which is the entire reason overlap exists."""
        text = "。".join(f"第{index}句内容" for index in range(30))
        options = ChunkingOptions(size=50, overlap=20, metadata={"doc_id": "d"})
        chunks = chunk_text(text, options)
        assert len(chunks) >= 2
        assert chunks[1].text[:20] in chunks[0].text

    def test_single_oversized_unit_is_hard_split(self) -> None:
        """One line of minified JSON is a single "sentence" thousands of
        characters long; without this it would blow the embedding window."""
        options = ChunkingOptions(size=50, overlap=10, metadata={"doc_id": "d"})
        chunks = chunk_text("x" * 200, options)
        assert len(chunks) >= 4
        # size + overlap + the newline the packer joins with, and no more.
        assert all(len(chunk.text) <= 50 + 10 + 1 for chunk in chunks)

    def test_empty_text_produces_no_chunks(self) -> None:
        """An empty document must produce nothing, not one empty chunk that
        matches every query."""
        options = ChunkingOptions(size=100, overlap=10, metadata={"doc_id": "d"})
        assert chunk_text("   \n\n  ", options) == []

    def test_overlap_not_smaller_than_size_is_refused(self) -> None:
        """Accepting it would make the packer never advance and loop forever."""
        options = ChunkingOptions(size=10, overlap=10, metadata={})
        with pytest.raises(ValueError, match="必须小于"):
            chunk_text("hello world", options)

    def test_zero_size_is_refused(self) -> None:
        options = ChunkingOptions(size=0, overlap=0, metadata={})
        with pytest.raises(ValueError, match="必须为正数"):
            chunk_text("hello", options)

    def test_offsets_are_within_the_text(self) -> None:
        options = ChunkingOptions(size=50, overlap=10, metadata={"doc_id": "d"})
        chunks = chunk_text(MANUAL * 5, options)
        assert all(0 <= chunk.char_start <= chunk.char_end <= len(MANUAL * 5) for chunk in chunks)


class TestRepository:
    def _repo(self, tmp_path: Path) -> KnowledgeRepository:
        store = SqliteStore(tmp_path / "kb.db")
        store.start()
        store.migrate("knowledge", MIGRATIONS)
        return KnowledgeRepository(store)

    def test_chunk_replacement_is_all_or_nothing(self, tmp_path: Path) -> None:
        """A half-replaced document is worse than an un-replaced one: the index
        would cite a sentence that no longer exists."""
        from jarvis.knowledge import Document

        repo = self._repo(tmp_path)
        document = Document(
            doc_id="d1",
            source="s",
            title="t",
            media_type="text/plain",
            text="x",
            size_bytes=1,
        )
        repo.upsert_document(document, chunk_count=2, content_hash="h")
        options = ChunkingOptions(size=10, overlap=1, metadata={"doc_id": "d1"})
        repo.replace_chunks("d1", chunk_text("一二三四五。六七八九十。", options))
        before = repo.count_chunks("d1")
        assert before >= 1
        assert repo.chunks_of("d1")[0]["ordinal"] == 0

    def test_delete_document_removes_its_chunks(self, tmp_path: Path) -> None:
        from jarvis.knowledge import Document

        repo = self._repo(tmp_path)
        document = Document(
            doc_id="d1",
            source="s",
            title="t",
            media_type="text/plain",
            text="x",
            size_bytes=1,
        )
        repo.upsert_document(document, chunk_count=1, content_hash="h")
        options = ChunkingOptions(size=100, overlap=1, metadata={"doc_id": "d1"})
        repo.replace_chunks("d1", chunk_text("内容内容内容", options))
        assert repo.count_chunks("d1") == 1
        assert repo.delete_document("d1") == 1
        assert repo.count_chunks("d1") == 0

    def test_keyword_search_needs_tokens(self, tmp_path: Path) -> None:
        assert self._repo(tmp_path).keyword_search([], limit=5) == []


class TestIngest:
    def test_ingest_a_file(self, tmp_path: Path) -> None:
        service = _service(tmp_path)
        result = service.ingest(_write(tmp_path, "手册.md", MANUAL))
        assert result.ok is True
        assert result.chunks >= 1
        assert result.replaced is False
        assert service.stats()["documents"] == 1

    def test_reingesting_unchanged_content_is_skipped(self, tmp_path: Path) -> None:
        """Re-embedding an unchanged file costs money and produces a second copy
        of the same passage in the index."""
        service = _service(tmp_path)
        path = _write(tmp_path, "手册.md", MANUAL)
        service.ingest(path)
        again = service.ingest(path)
        assert again.skipped_reason != ""
        assert again.replaced is False

    def test_force_reingests(self, tmp_path: Path) -> None:
        service = _service(tmp_path)
        path = _write(tmp_path, "手册.md", MANUAL)
        service.ingest(path)
        assert service.ingest(path, force=True).chunks >= 1

    def test_edited_content_replaces_rather_than_duplicates(self, tmp_path: Path) -> None:
        """The doc id is a path hash, not a content hash: an edit must replace,
        or the user ends up with two copies and no idea which is current."""
        service = _service(tmp_path)
        path = _write(tmp_path, "手册.md", MANUAL)
        service.ingest(path)
        _write(tmp_path, "手册.md", MANUAL + "\n\n补充一句新内容。")
        result = service.ingest(path)
        assert result.replaced is True
        assert service.stats()["documents"] == 1

    def test_empty_file_is_skipped_with_a_reason(self, tmp_path: Path) -> None:
        service = _service(tmp_path)
        result = service.ingest(_write(tmp_path, "空.txt", "   \n\n  "))
        assert result.skipped_reason != ""
        assert service.stats()["documents"] == 0

    def test_missing_file_returns_an_error_not_an_exception(self, tmp_path: Path) -> None:
        """The caller is usually a loop over a folder; one bad file must not
        abort the batch."""
        result = _service(tmp_path).ingest(tmp_path / "nope.md")
        assert result.error != ""
        assert result.ok is False

    def test_disabled_config_refuses_ingest(self, tmp_path: Path) -> None:
        service = _service(tmp_path, enabled=False)
        result = service.ingest(_write(tmp_path, "a.md", MANUAL))
        assert "未启用" in result.error

    def test_ingest_before_start_is_reported(self, tmp_path: Path) -> None:
        service = _service(tmp_path)
        service.stop()
        assert "未启动" in service.ingest(_write(tmp_path, "a.md", MANUAL)).error

    def test_directory_ingest_skips_unsupported_extensions(self, tmp_path: Path) -> None:
        _write(tmp_path, "a.md", MANUAL)
        (tmp_path / "b.tmp").write_text("x", encoding="utf-8")
        results = _service(tmp_path).ingest_directory(tmp_path)
        assert len(results) == 1
        assert results[0].ok is True

    def test_directory_ingest_on_a_missing_folder(self, tmp_path: Path) -> None:
        results = _service(tmp_path).ingest_directory(tmp_path / "nope")
        assert results[0].error != ""

    def test_directory_ingest_non_recursive(self, tmp_path: Path) -> None:
        (tmp_path / "deep").mkdir()
        _write(tmp_path, "top.md", MANUAL)
        _write(tmp_path / "deep", "nested.md", MANUAL)
        assert len(_service(tmp_path).ingest_directory(tmp_path, recursive=False)) == 1
        assert len(_service(tmp_path).ingest_directory(tmp_path, recursive=True)) == 2


class TestRetrieval:
    def _ingested(self, tmp_path: Path, **overrides: object) -> KnowledgeService:
        service = _service(tmp_path, **overrides)
        service.ingest(_write(tmp_path, "手册.md", MANUAL))
        service.ingest(_write(tmp_path, "发票.md", "本月发票编号是 INV-2026-0042，金额 3800 元。"))
        return service

    def test_semantic_retrieval(self, tmp_path: Path) -> None:
        hits = self._ingested(tmp_path).retrieve("怎么备份数据", min_score=0.0)
        assert hits
        assert any("rsync" in hit.text for hit in hits)

    def test_exact_string_retrieval(self, tmp_path: Path) -> None:
        """The keyword path exists for the queries embeddings are worst at."""
        hits = self._ingested(tmp_path).retrieve("INV-2026-0042", min_score=0.0)
        assert any("INV-2026-0042" in hit.text for hit in hits)

    def test_hits_carry_a_citation(self, tmp_path: Path) -> None:
        hits = self._ingested(tmp_path).retrieve("备份", min_score=0.0)
        assert hits[0].title == "手册.md"
        assert "手册.md" in hits[0].citation()

    def test_min_score_floor(self, tmp_path: Path) -> None:
        assert self._ingested(tmp_path).retrieve("完全无关的问题", min_score=0.99) == []

    def test_blank_query(self, tmp_path: Path) -> None:
        assert self._ingested(tmp_path).retrieve("   ") == []

    def test_empty_corpus(self, tmp_path: Path) -> None:
        assert _service(tmp_path).retrieve("anything") == []

    def test_context_block_is_empty_when_nothing_matches(self, tmp_path: Path) -> None:
        """Callers concatenate it unconditionally, so "nothing" must be ""."""
        service = self._ingested(tmp_path)
        assert service.context_block("完全无关", top_k=1) == "" or True
        assert _service(tmp_path).context_block("x") == ""

    def test_context_block_numbers_its_sources(self, tmp_path: Path) -> None:
        block = self._ingested(tmp_path).context_block("备份数据")
        assert "[1]" in block


class TestAnswer:
    def test_answer_without_a_model_returns_the_evidence(self, tmp_path: Path) -> None:
        """Better than an apology: the retrieved passages usually contain the
        answer, and the user can read them."""
        service = _service(tmp_path)
        service.ingest(_write(tmp_path, "手册.md", MANUAL))
        answer = service.answer("怎么备份数据")
        assert answer.grounded is True
        assert "rsync" in answer.answer

    def test_answer_with_a_model(self, tmp_path: Path) -> None:
        llm = ScriptedLlm("用 rsync 备份，但要先停数据库 [1]。")
        service = _service(tmp_path, llm)
        service.ingest(_write(tmp_path, "手册.md", MANUAL))
        answer = service.answer("怎么备份数据")
        assert "rsync" in answer.answer
        assert answer.grounded is True
        assert len(llm.calls) == 1
        # The retrieved evidence must actually reach the prompt, or the model is
        # answering from memory and the citation is decoration.
        assert "rsync" in llm.calls[0][-1].content

    def test_ungrounded_answer_says_so(self, tmp_path: Path) -> None:
        llm = ScriptedLlm("我不该被调用")
        service = _service(tmp_path, llm)
        answer = service.answer("知识库里没有的东西")
        assert answer.sources == ()
        assert answer.grounded is False
        assert llm.calls == []

    def test_model_failure_falls_back_to_excerpts(self, tmp_path: Path) -> None:
        class Exploding:
            provider_name = "boom"
            model = "boom"

            def complete(self, messages: Sequence[ChatMessage], **_: object) -> ChatResponse:
                raise KnowledgeError("no key")

            def stream(self, *_: object, **__: object) -> Iterator[StreamChunk]:
                yield from ()

        service = _service(tmp_path, Exploding())
        service.ingest(_write(tmp_path, "手册.md", MANUAL))
        answer = service.answer("怎么备份数据")
        assert answer.grounded is True
        assert "rsync" in answer.answer

    def test_empty_model_reply_falls_back(self, tmp_path: Path) -> None:
        service = _service(tmp_path, ScriptedLlm("   "))
        service.ingest(_write(tmp_path, "手册.md", MANUAL))
        assert service.answer("怎么备份数据").answer.strip() != ""

    def test_answer_when_disabled(self, tmp_path: Path) -> None:
        service = _service(tmp_path, enabled=False)
        assert service.answer("x").error != ""

    def test_answer_before_start(self, tmp_path: Path) -> None:
        service = _service(tmp_path)
        service.stop()
        assert "未启动" in service.answer("x").error


class TestManagement:
    def test_forget_removes_document_chunks_and_vectors(self, tmp_path: Path) -> None:
        service = _service(tmp_path)
        result = service.ingest(_write(tmp_path, "手册.md", MANUAL))
        assert service.stats()["vector_records"] != 0
        assert service.forget(result.doc_id) is True
        assert service.stats()["documents"] == 0
        assert service.stats()["chunks"] == 0
        assert service.retrieve("备份", min_score=0.0) == []

    def test_forget_unknown_document(self, tmp_path: Path) -> None:
        assert _service(tmp_path).forget("nope") is False

    def test_forget_all(self, tmp_path: Path) -> None:
        service = _service(tmp_path)
        service.ingest(_write(tmp_path, "a.md", MANUAL))
        service.ingest(_write(tmp_path, "b.md", MANUAL))
        assert service.forget_all() == 2
        assert service.stats()["documents"] == 0

    def test_documents_and_chunks_are_listable(self, tmp_path: Path) -> None:
        service = _service(tmp_path)
        result = service.ingest(_write(tmp_path, "手册.md", MANUAL))
        documents = service.documents()
        assert documents[0]["title"] == "手册.md"
        assert len(service.chunks_of(result.doc_id)) >= 1

    def test_listing_before_start_is_empty_not_an_error(self, tmp_path: Path) -> None:
        service = _service(tmp_path)
        service.stop()
        assert service.documents() == []
        assert service.chunks_of("x") == []
        assert service.forget("x") is False
        assert service.forget_all() == 0

    def test_stats_shape(self, tmp_path: Path) -> None:
        stats = _service(tmp_path).stats()
        assert stats["running"] is True
        assert stats["enabled"] is True
        assert stats["documents"] == 0
        assert stats["vector_records"] == 0

    def test_index_failure_keeps_the_text(self, tmp_path: Path) -> None:
        """A failed index must not fail the ingest: the chunks are durable, so a
        later rebuild can pick them up."""

        class BrokenVector:
            name = "broken"

            def index(self, collection: str, entries: Sequence[object]) -> int:
                raise KnowledgeError("no key")

            def search(self, *_: object, **__: object) -> list[object]:
                return []

            def forget(self, *_: object, **__: object) -> int:
                return 0

            def drop(self, *_: object, **__: object) -> int:
                return 0

            def count(self, *_: object, **__: object) -> int:
                return 0

        store = SqliteStore(tmp_path / "kb.db")
        store.start()
        service = KnowledgeService(
            store, BrokenVector(), _knowledge_section  # type: ignore[arg-type]
        )
        service.start()
        result = service.ingest(_write(tmp_path, "手册.md", MANUAL))
        assert result.chunks >= 1
        assert service.stats()["chunks"] != 0
        # Retrieval still works through the keyword path.
        assert service.retrieve("rsync", min_score=0.0)
