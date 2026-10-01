"""Knowledge base: RAG over user documents and notes.

Responsibility (delivered in phase 11):
    * Document ingestion, chunking, embedding, retrieval-augmented
      generation pipelines.

How it works: :func:`~jarvis.knowledge.loader.load_document` extracts text
(UTF-8 with a GB18030 fallback, so a Notepad-written ``.txt`` from a Chinese
Windows install is readable), :mod:`~jarvis.knowledge.chunker` cuts it on
sentence boundaries with a character overlap, the chunks are stored durably in
SQLite *and* embedded into the vector index, and retrieval blends the two — so
an exact error code and a paraphrased question both find their passage.

Ingestion is explicit. There is no background crawl: a knowledge base that
silently indexes whatever it can reach is a privacy incident with a progress
bar.

Allowed dependencies: ``core``, ``config``, ``database``, ``vector``,
``llm``.
"""

from jarvis.knowledge.chunker import chunk_text, split_units
from jarvis.knowledge.loader import (
    DOCX_SUFFIXES,
    PDF_SUFFIXES,
    SUPPORTED_SUFFIXES,
    TEXT_SUFFIXES,
    decode_text,
    document_id,
    load_document,
    media_type_for,
)
from jarvis.knowledge.service import COLLECTION, KEYWORD_WEIGHT, KnowledgeService
from jarvis.knowledge.store import MIGRATIONS, NAMESPACE, KnowledgeRepository
from jarvis.knowledge.types import (
    Answer,
    Chunk,
    ChunkingOptions,
    Document,
    IngestResult,
    RetrievedChunk,
)

__all__ = [
    "COLLECTION",
    "DOCX_SUFFIXES",
    "KEYWORD_WEIGHT",
    "MIGRATIONS",
    "NAMESPACE",
    "PDF_SUFFIXES",
    "SUPPORTED_SUFFIXES",
    "TEXT_SUFFIXES",
    "Answer",
    "Chunk",
    "ChunkingOptions",
    "Document",
    "IngestResult",
    "KnowledgeRepository",
    "KnowledgeService",
    "RetrievedChunk",
    "chunk_text",
    "decode_text",
    "document_id",
    "load_document",
    "media_type_for",
    "split_units",
]
