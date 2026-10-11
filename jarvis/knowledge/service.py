"""``KnowledgeService`` — ingest, retrieve, answer.

Retrieval is hybrid for the same reason memory's is: a personal corpus is asked
exact questions (an error code, a model number, a name) at least as often as
paraphrased ones, and a hashing embedder blurs exact strings. Semantic hits and
keyword hits are blended into one score, so neither path can veto the other.

Ingestion is **explicit and one-shot**. There is no background crawl of the
disk: a knowledge base that silently indexes whatever it can reach is a privacy
incident with a progress bar.
"""

from __future__ import annotations

import hashlib
import logging
import re
from collections.abc import Callable, Sequence
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Final

from jarvis.core.exceptions import JarvisError, KnowledgeError
from jarvis.core.text import keyword_tokens, match_ratio
from jarvis.database import Row, as_int, as_str
from jarvis.knowledge.chunker import chunk_text
from jarvis.knowledge.loader import SUPPORTED_SUFFIXES, load_document
from jarvis.knowledge.store import MIGRATIONS, NAMESPACE, KnowledgeRepository
from jarvis.knowledge.types import (
    Answer,
    Chunk,
    ChunkingOptions,
    Document,
    IngestResult,
    RetrievedChunk,
)
from jarvis.llm.types import ChatMessage
from jarvis.prompt import render_prompt

if TYPE_CHECKING:  # pragma: no cover - typing only
    from jarvis.config.schema import KnowledgeSection
    from jarvis.database import SqliteStore
    from jarvis.llm.client import LlmClient
    from jarvis.vector import VectorService

logger = logging.getLogger("jarvis.knowledge.service")

COLLECTION: Final[str] = "knowledge"
"""Vector collection holding every chunk. One collection, because a question
should be able to match any document the user ingested."""

KEYWORD_WEIGHT: Final[float] = 0.5
"""Contribution of a full keyword match, mirroring ``memory``'s weighting so the
two retrieval paths behave the same way when a user compares them."""

_NOTE_UNSAFE = re.compile(r"[<>:\"/\\|?*\x00-\x1f]+")
"""Everything Windows refuses in a filename -- both path separators included, because
a title is model-authored text and 「C:/Users/…」 has to stay one filename."""

_NOTE_TAIL = re.compile(r"[ .]+$")
"""Trailing dots and spaces, which Windows silently drops and then confuses everybody."""


def _note_stem(title: str) -> str:
    """A filename for one note: readable, and not something the model can aim.

    Chinese titles are kept as they are -- stripping non-ASCII would collapse every
    note this program is asked to write onto the same empty name. What goes is the
    illegal set. A short hash of the title rides along so two different titles that
    clean down to the same characters still become two files, while the *same* title
    keeps landing on the same file so 「更新那条笔记」 replaces rather than piles up.
    """
    safe = _NOTE_TAIL.sub("", _NOTE_UNSAFE.sub(" ", " ".join(title.split())).strip())
    safe = safe[:80] or "笔记"
    return f"{safe}-{hashlib.sha256(title.encode('utf-8')).hexdigest()[:8]}"


class KnowledgeService:
    """The RAG knowledge base: ingest documents, retrieve evidence, answer."""

    name = "knowledge"

    def __init__(
        self,
        store: SqliteStore,
        vector: VectorService,
        settings_provider: Callable[[], KnowledgeSection],
        *,
        llm_provider: Callable[[], LlmClient] | None = None,
        notes_dir: Path | None = None,
    ) -> None:
        """Create the service.

        Args:
            store: The shared database (already started).
            vector: The vector service (already started).
            settings_provider: Returns the validated ``knowledge`` config.
            llm_provider: Yields a chat client for :meth:`answer`. ``None``
                leaves ingestion and retrieval fully working — a text-only
                install still gets search, just not summarised answers.
            notes_dir: Where :meth:`ingest_text` may write. ``None`` means this
                install has nowhere to put a note of her own, and the method
                refuses with that reason rather than writing somewhere unannounced.
        """
        self._store = store
        self._vector = vector
        self._settings_provider = settings_provider
        self._llm_provider = llm_provider
        self._notes_dir = notes_dir
        self._repo = KnowledgeRepository(store)
        self._started = False

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        """Apply the schema (idempotent, no network, no model load)."""
        if self._started:
            return
        self._store.migrate(NAMESPACE, MIGRATIONS)
        self._started = True
        self.repair_index()
        logger.info(
            "knowledge service ready (%d document(s), %d chunk(s))",
            self._repo.count_documents(),
            self._repo.count_chunks(),
        )

    def repair_index(self) -> dict[str, int]:
        """Drop vector records that no longer have a chunk row, and report the counts.

        Runs at start-up because the orphans are already in existing users'
        databases: every document that was ever re-ingested shorter, and every
        document forgotten before this existed, left vectors behind that answer
        searches with text nobody can see in the list any more. Cleaning them
        deletes no user material -- documents and chunks are the record, the
        vectors are derived from them.
        """
        if not self._started:
            return {"checked": 0, "removed": 0}
        ids = self._vector.record_ids(COLLECTION)
        live = self._repo.existing_chunk_ids(ids)
        dead = [record for record in ids if record not in live]
        if dead:
            self._vector.forget(COLLECTION, dead)
            logger.warning("知识库里有 %d 条向量已经没有对应切片（旧版本残留），已清掉", len(dead))
        return {"checked": len(ids), "removed": len(dead)}

    def stop(self) -> None:
        self._started = False

    @property
    def running(self) -> bool:
        return self._started

    @property
    def enabled(self) -> bool:
        """Whether the config allows ingestion and retrieval."""
        return self._settings_provider().enabled

    @property
    def notes_dir(self) -> Path | None:
        """Where her own notes may be written, or ``None`` when this install has nowhere.

        Read by the tool assembler: ``add_knowledge`` pointed at no directory can only
        ever answer 「这台机器没给她留记笔记的地方」, which is the advertise-a-dead-end
        mistake this project has already made twice with shell tools.
        """
        return self._notes_dir

    # -- ingestion ---------------------------------------------------------

    def ingest(self, path: Path | str, *, force: bool = False) -> IngestResult:
        """Read one file into the knowledge base.

        Never raises: the caller is usually a loop over a folder, and one
        unreadable file must not abort the batch. The failure comes back as
        ``IngestResult.error``.

        Args:
            path: File to ingest.
            force: Re-embed even when the content hash is unchanged.
        """
        if not self._started:
            return self._failed(path, "知识库未启动")
        if not self.enabled:
            return self._failed(path, "知识库未启用（knowledge.enabled=false）")
        settings = self._settings_provider()
        try:
            document = load_document(Path(path), max_file_mb=settings.max_file_mb)
        except KnowledgeError as exc:
            logger.warning("cannot ingest %s: %s", path, exc)
            return self._failed(path, str(exc))

        digest = hashlib.sha256(document.text.encode("utf-8")).hexdigest()
        previous = self._repo.find_document(document.doc_id)
        replaced = previous is not None
        if previous is not None and not force and as_str(previous["content_hash"]) == digest:
            return IngestResult(
                doc_id=document.doc_id,
                source=document.source,
                title=document.title,
                chunks=as_int(previous["chunk_count"]),
                replaced=False,
                skipped_reason="内容未变化，已跳过",
            )

        options = ChunkingOptions(
            size=settings.chunk_size,
            overlap=settings.chunk_overlap,
            metadata={"doc_id": document.doc_id, "source": document.source},
        )
        chunks = chunk_text(document.text, options)
        if not chunks:
            return IngestResult(
                doc_id=document.doc_id,
                source=document.source,
                title=document.title,
                chunks=0,
                replaced=False,
                skipped_reason="文件里没有可索引的文字",
            )

        self._repo.upsert_document(document, chunk_count=len(chunks), content_hash=digest)
        self._repo.replace_chunks(document.doc_id, chunks)
        self._index_chunks(document, chunks)
        logger.info(
            "ingested %s (%d chunk(s), replaced=%s)",
            document.source,
            len(chunks),
            replaced,
        )
        return IngestResult(
            doc_id=document.doc_id,
            source=document.source,
            title=document.title,
            chunks=len(chunks),
            replaced=replaced,
        )

    def ingest_text(self, title: str, text: str, *, source: str = "") -> IngestResult:
        """Save one note she wrote herself and index it, so the corpus can grow by talking.

        Why this exists: ``knowledge_search`` was the only knowledge tool and it is
        read-only. Asked 「去查一下，回头记到你的资料里」 she could answer the question and
        then had nowhere to put the answer — long-term memory holds one sentence *about*
        a fact, not the page it came from. This is the write half, and it goes through
        the same :meth:`ingest` a person triggers from the 加入知识库 button, so there is
        one storage shape and one retrieval quality rather than a second-class one.

        The file lands in ``notes_dir`` and nowhere else. Not under ``file_roots``:
        a corpus she can rewrite by saving twice over is a corpus nobody can trust,
        and 「记一下」 must not become a way to overwrite an imported manual.
        """
        if not self._started:
            return self._failed("<笔记>", "知识库未启动")
        if not self.enabled:
            return self._failed("<笔记>", "知识库未启用（knowledge.enabled=false）")
        if self._notes_dir is None:
            return self._failed("<笔记>", "这台机器没给她留记笔记的地方（notes_dir 未注入）")
        clean_title = " ".join(title.split())[:120] or "未命名"
        body = text.strip()
        if not body:
            return self._failed(clean_title, "没有要存进知识库的内容")
        path = self._notes_dir / f"{_note_stem(clean_title)}.md"
        origin = source.strip()[:300] or "小夜自己记的"
        page = "\n".join(
            [
                f"# {clean_title}",
                "",
                f"来源：{origin}",
                f"记下时间：{datetime.now().strftime('%Y-%m-%d %H:%M')}",
                "",
                body,
            ]
        )
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(page + "\n", encoding="utf-8")
        except OSError as exc:
            logger.warning("could not save note %s: %s", path, exc)
            return self._failed(clean_title, f"笔记没能存下来（{type(exc).__name__}: {exc}）")
        return self.ingest(path, force=True)

    def ingest_directory(
        self,
        directory: Path | str,
        *,
        pattern: str = "*",
        recursive: bool = True,
        force: bool = False,
    ) -> list[IngestResult]:
        """Ingest every supported file under ``directory``.

        Unsupported extensions are counted and reported once, not once per file:
        a folder with 5000 ``.tmp`` files should not produce 5000 failure rows.
        """
        root = Path(directory).expanduser()
        if not root.is_dir():
            return [self._failed(root, f"不是目录：{root}")]
        candidates = sorted(root.rglob(pattern) if recursive else root.glob(pattern))
        results: list[IngestResult] = []
        unsupported = 0
        for candidate in candidates:
            if not candidate.is_file():
                continue
            if candidate.suffix.lower() not in SUPPORTED_SUFFIXES:
                unsupported += 1
                continue
            results.append(self.ingest(candidate, force=force))
        if unsupported:
            logger.info("skipped %d unsupported file(s) under %s", unsupported, root)
        return results

    # -- retrieval ---------------------------------------------------------

    def retrieve(
        self,
        query: str,
        *,
        top_k: int = 0,
        min_score: float = -1.0,
    ) -> list[RetrievedChunk]:
        """Find the chunks most likely to answer ``query``.

        Returns an empty list when nothing clears the floor. An empty result is
        the honest answer, and it is what makes :meth:`answer` able to say
        "资料里没有提到" instead of inventing something.
        """
        if not self._started or not self.enabled or not query.strip():
            return []
        settings = self._settings_provider()
        limit = top_k if top_k > 0 else settings.top_k
        floor = min_score if min_score >= 0 else settings.min_score
        if limit <= 0:
            return []

        scores: dict[str, float] = {}
        chunks: dict[str, RetrievedChunk] = {}

        for hit in self._vector.search(COLLECTION, query, top_k=limit * 3):
            metadata = hit.metadata
            chunks[hit.record_id] = RetrievedChunk(
                chunk_id=hit.record_id,
                doc_id=str(metadata.get("doc_id", "")),
                source=str(metadata.get("source", "")),
                title=str(metadata.get("title", "")),
                ordinal=as_int(metadata.get("ordinal")),
                text=hit.text,
                score=hit.score,
            )
            scores[hit.record_id] = hit.score

        tokens = keyword_tokens(query)
        for row in self._repo.keyword_search(tokens, limit=limit * 3):
            chunk_id = str(row["chunk_id"])
            text = str(row["text"])
            score = KEYWORD_WEIGHT * match_ratio(text, tokens)
            scores[chunk_id] = max(scores.get(chunk_id, 0.0), score)
            chunks.setdefault(
                chunk_id,
                RetrievedChunk(
                    chunk_id=chunk_id,
                    doc_id=str(row["doc_id"]),
                    source=str(row["source"]),
                    title=str(row["title"]),
                    ordinal=as_int(row["ordinal"]),
                    text=text,
                    score=score,
                ),
            )

        # Defence in depth, and the only thing that protects a database written by
        # an older build: a hit whose chunk row is gone is deleted text, however
        # well it matches.
        live = self._repo.existing_chunk_ids(list(chunks))
        dead = [key for key in list(chunks) if key not in live]
        for key in dead:
            chunks.pop(key, None)
            scores.pop(key, None)
        if dead:
            logger.warning("检索剔除了 %d 条已经没有切片的旧向量命中", len(dead))

        ranked = sorted(chunks.values(), key=lambda chunk: scores[chunk.chunk_id], reverse=True)
        return [
            RetrievedChunk(
                chunk_id=chunk.chunk_id,
                doc_id=chunk.doc_id,
                source=chunk.source,
                title=chunk.title,
                ordinal=chunk.ordinal,
                text=chunk.text,
                score=scores[chunk.chunk_id],
            )
            for chunk in ranked
            if scores[chunk.chunk_id] >= floor
        ][:limit]

    def context_block(self, query: str, *, top_k: int = 0) -> str:
        """Render retrieved evidence as a prompt fragment.

        Returns an empty string when nothing was found, so a caller can
        concatenate it unconditionally.
        """
        hits = self.retrieve(query, top_k=top_k)
        if not hits:
            return ""
        lines = ["以下是与问题相关的资料片段（编号供引用）："]
        for index, hit in enumerate(hits, start=1):
            lines.append(f"[{index}] 来源：{hit.citation()}\n{hit.text}")
        return "\n\n".join(lines)

    def answer(self, question: str, *, top_k: int = 0) -> Answer:
        """Answer ``question`` from the corpus, with citations.

        Never raises. When nothing is retrieved the reply says so and carries no
        sources — which is the whole point of separating "the document said it"
        from "the model thinks it".
        """
        if not self._started:
            return Answer(question, "", error="知识库未启动")
        if not self.enabled:
            return Answer(question, "", error="知识库未启用")
        hits = self.retrieve(question, top_k=top_k)
        if not hits:
            return Answer(question, "知识库里没有找到相关资料。")
        if self._llm_provider is None:
            return Answer(question, self._fallback_answer(hits), sources=tuple(hits))

        context = "\n\n".join(
            f"[{index}] 来源：{hit.citation()}\n{hit.text}"
            for index, hit in enumerate(hits, start=1)
        )
        try:
            client = self._llm_provider()
            reply = client.complete(
                [
                    ChatMessage.system(render_prompt("knowledge_answer")),
                    ChatMessage.user(f"资料：\n{context}\n\n问题：{question}"),
                ]
            ).content
        except JarvisError as exc:
            logger.warning("knowledge answer failed, falling back to excerpts: %s", exc)
            return Answer(question, self._fallback_answer(hits), sources=tuple(hits))
        except Exception:  # pragma: no cover - a client bug must not lose the hits
            logger.exception("knowledge answer failed unexpectedly")
            return Answer(question, self._fallback_answer(hits), sources=tuple(hits))

        answer = reply.strip() or self._fallback_answer(hits)
        return Answer(question, answer, sources=tuple(hits))

    @staticmethod
    def _fallback_answer(hits: Sequence[RetrievedChunk]) -> str:
        """What to show when no model is available: the evidence itself.

        Better than an apology — the retrieved passages usually contain the
        answer, and the user can read them.
        """
        lines = ["（未配置大模型，直接给出检索到的原文片段）"]
        for index, hit in enumerate(hits, start=1):
            lines.append(f"[{index}] {hit.citation()}\n{hit.text}")
        return "\n\n".join(lines)

    # -- management --------------------------------------------------------

    def forget(self, doc_id: str) -> bool:
        """Remove a document, its chunks, and its vectors."""
        if not self._started:
            return False
        vector_ids = self._vector.record_ids(COLLECTION, metadata={"doc_id": doc_id})
        chunks = self._repo.count_chunks(doc_id)
        removed = self._repo.delete_document(doc_id) > 0
        if vector_ids:
            self._vector.forget(COLLECTION, vector_ids)
        logger.info("forgot %s: %d chunk(s), %d vector record(s)", doc_id, chunks, len(vector_ids))
        return removed

    def forget_all(self) -> int:
        """Empty the knowledge base (documents, chunks and index)."""
        if not self._started:
            return 0
        removed = self._repo.delete_all()
        self._vector.drop(COLLECTION)
        logger.warning("knowledge base cleared (%d document(s))", removed)
        return removed

    def documents(self) -> list[Row]:
        """Ingested documents, newest first."""
        if not self._started:
            return []
        return self._repo.documents()

    def chunks_of(self, doc_id: str) -> list[Row]:
        """Every chunk of one document, in order."""
        if not self._started:
            return []
        return self._repo.chunks_of(doc_id)

    def stats(self) -> dict[str, object]:
        """Read-only snapshot for the HUD and the health check."""
        return {
            "running": self._started,
            "enabled": self.enabled,
            "documents": self._repo.count_documents() if self._started else 0,
            "chunks": self._repo.count_chunks() if self._started else 0,
            "vector_records": self._vector.count(COLLECTION),
        }

    # -- internals ---------------------------------------------------------

    def _index_chunks(self, document: Document, chunks: Sequence[Chunk]) -> None:
        """Embed and index the chunks, tolerating an unavailable embedder.

        A failed index must not fail the ingest: the chunks are already durable,
        so a later re-ingest (or an index rebuild) can pick them up. The user
        gets a warning rather than a rollback that loses the text.
        """
        from jarvis.vector import VectorEntry

        if not chunks:
            return
        # Drop the previous version's vectors first: the chunk ids are stable,
        # but a shorter new version would otherwise leave orphans behind that
        # still match queries and cite text that no longer exists.
        # Drop *everything this document ever indexed*, not just the chunks about to
        # be written: a shorter new version has no chunk #4 to name the old chunk #4,
        # and that orphan kept answering queries with deleted text.
        stale = self._vector.record_ids(COLLECTION, metadata={"doc_id": document.doc_id})
        if stale:
            self._vector.forget(COLLECTION, stale)
        try:
            self._vector.index(
                COLLECTION,
                [
                    VectorEntry(
                        record_id=chunk.chunk_id,
                        text=chunk.text,
                        metadata={
                            "doc_id": document.doc_id,
                            "source": document.source,
                            "title": document.title,
                            "ordinal": chunk.ordinal,
                        },
                    )
                    for chunk in chunks
                ],
            )
        except JarvisError as exc:
            logger.warning(
                "chunks for %s were stored but not indexed: %s (retrieval will fall back "
                "to keyword matching until the index is rebuilt)",
                document.source,
                exc,
            )

    @staticmethod
    def _failed(path: Path | str, reason: str) -> IngestResult:
        return IngestResult(
            doc_id="",
            source=str(path),
            title=Path(path).name if str(path) else "",
            chunks=0,
            replaced=False,
            error=reason,
        )


__all__ = ["COLLECTION", "KEYWORD_WEIGHT", "KnowledgeService"]
