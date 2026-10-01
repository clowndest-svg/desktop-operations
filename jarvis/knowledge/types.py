"""Knowledge-base vocabulary: documents, chunks, and what retrieval returns.

A *document* is what the user handed over; a *chunk* is what actually gets
embedded. Keeping them as separate types is what makes "which file said that"
answerable — a search hit that can only say "somewhere in your notes" is not
usable as a citation, and the user has no way to check whether the assistant
made it up.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class Document:
    """One ingested source file."""

    doc_id: str
    """Stable id derived from the resolved path, so re-ingesting replaces."""

    source: str
    """Absolute path the text came from."""

    title: str
    """Display name, normally the file name."""

    media_type: str
    """MIME-ish tag, e.g. ``text/markdown``, ``application/pdf``."""

    text: str
    """Full extracted text. Kept in memory only; the database stores chunks."""

    size_bytes: int
    chunk_count: int = 0

    def to_dict(self) -> dict[str, object]:
        """Render without ``text``: this shape is sent to the UI, and shipping
        a whole manual over the JS bridge to draw a list row is a waste."""
        return {
            "doc_id": self.doc_id,
            "source": self.source,
            "title": self.title,
            "media_type": self.media_type,
            "size_bytes": self.size_bytes,
            "chunk_count": self.chunk_count,
        }


@dataclass(frozen=True, slots=True)
class Chunk:
    """A retrievable slice of a document."""

    chunk_id: str
    """``<doc_id>#<ordinal>`` — stable across re-ingests of the same content."""

    doc_id: str
    ordinal: int
    """Position within the document, from 0."""

    text: str
    char_start: int
    """Offset of the chunk in the document's extracted text, for highlighting."""

    char_end: int

    def to_dict(self) -> dict[str, object]:
        return {
            "chunk_id": self.chunk_id,
            "doc_id": self.doc_id,
            "ordinal": self.ordinal,
            "char_start": self.char_start,
            "char_end": self.char_end,
        }


@dataclass(frozen=True, slots=True)
class RetrievedChunk:
    """A chunk that matched a query, with enough context to cite it."""

    chunk_id: str
    doc_id: str
    source: str
    title: str
    ordinal: int
    text: str
    score: float

    def to_dict(self) -> dict[str, object]:
        return {
            "chunk_id": self.chunk_id,
            "doc_id": self.doc_id,
            "source": self.source,
            "title": self.title,
            "ordinal": self.ordinal,
            "text": self.text,
            "score": round(self.score, 4),
        }

    def citation(self) -> str:
        """One-line provenance, e.g. ``手册.pdf 第 3 段``."""
        return f"{self.title} 第 {self.ordinal + 1} 段"


@dataclass(frozen=True, slots=True)
class IngestResult:
    """What one ingest call did."""

    doc_id: str
    source: str
    title: str
    chunks: int
    replaced: bool
    """``True`` when an earlier version of the same source was replaced."""

    skipped_reason: str = ""
    """Non-empty when nothing was ingested, with the reason to show the user."""

    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error and not self.skipped_reason

    def to_dict(self) -> dict[str, object]:
        return {
            "doc_id": self.doc_id,
            "source": self.source,
            "title": self.title,
            "chunks": self.chunks,
            "replaced": self.replaced,
            "skipped_reason": self.skipped_reason,
            "error": self.error,
        }


@dataclass(frozen=True, slots=True)
class Answer:
    """A RAG answer plus the chunks it was built from."""

    question: str
    answer: str
    sources: tuple[RetrievedChunk, ...] = ()
    error: str = ""

    @property
    def grounded(self) -> bool:
        """Whether any evidence was found. An answer with no sources is the
        model's own knowledge, and saying so is the difference between a
        citation and a guess."""
        return bool(self.sources)

    def to_dict(self) -> dict[str, object]:
        return {
            "question": self.question,
            "answer": self.answer,
            "sources": [source.to_dict() for source in self.sources],
            "grounded": self.grounded,
            "error": self.error,
        }


@dataclass(frozen=True, slots=True)
class ChunkingOptions:
    """How text is cut up, resolved from configuration."""

    size: int
    overlap: int
    metadata: Mapping[str, object] = field(default_factory=dict)
