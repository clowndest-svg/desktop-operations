"""SQLite persistence for the knowledge base.

The chunk text is stored **twice**: once here, once inside the vector index.
That is deliberate, and the reason is that the two copies answer different
questions.

* ``kb_chunks`` is the durable record of what was ingested. It survives the
  vector index being dropped, rebuilt, or rebuilt by a *different* embedder —
  which is exactly what happens when an operator switches from the offline
  hashing embedder to a cloud one, because the widths differ and the old index
  has to be thrown away.
* The vector record's copy exists so an index hit is self-describing: search
  results can be inspected without a join, and a stale index still explains
  what it matched.

At personal-corpus scale the duplication costs a few megabytes. Losing the
corpus because the index was rebuilt costs a re-ingest of every file, and the
original files may no longer exist.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence

from jarvis.database import (
    Migration,
    Repository,
    Row,
    as_int,
    as_str,
    utc_now,
)
from jarvis.knowledge.types import Chunk, Document

logger = logging.getLogger("jarvis.knowledge.store")

NAMESPACE: str = "knowledge"

MIGRATIONS: tuple[Migration, ...] = (
    Migration(
        namespace=NAMESPACE,
        version=1,
        statements=(
            """
            CREATE TABLE IF NOT EXISTS kb_documents (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                doc_id       TEXT    NOT NULL UNIQUE,
                source       TEXT    NOT NULL,
                title        TEXT    NOT NULL DEFAULT '',
                media_type   TEXT    NOT NULL DEFAULT 'text/plain',
                size_bytes   INTEGER NOT NULL DEFAULT 0,
                chunk_count  INTEGER NOT NULL DEFAULT 0,
                content_hash TEXT    NOT NULL DEFAULT '',
                ingested_at  TEXT    NOT NULL
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_kb_documents_source
                ON kb_documents (source)
            """,
            """
            CREATE TABLE IF NOT EXISTS kb_chunks (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                chunk_id   TEXT    NOT NULL UNIQUE,
                doc_id     TEXT    NOT NULL,
                ordinal    INTEGER NOT NULL,
                text       TEXT    NOT NULL,
                char_start INTEGER NOT NULL DEFAULT 0,
                char_end   INTEGER NOT NULL DEFAULT 0,
                FOREIGN KEY (doc_id) REFERENCES kb_documents (doc_id) ON DELETE CASCADE
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_kb_chunks_doc
                ON kb_chunks (doc_id, ordinal)
            """,
        ),
    ),
)


class KnowledgeRepository(Repository):
    """CRUD for ingested documents and their chunks."""

    # -- documents ---------------------------------------------------------

    def upsert_document(
        self,
        document: Document,
        *,
        chunk_count: int,
        content_hash: str,
    ) -> bool:
        """Insert or replace a document row; returns whether one already existed.

        The caller uses the return value to report "replaced" to the user, which
        matters: silently re-ingesting is how somebody ends up with two copies of
        an edited file and no idea which one is current.
        """
        existed = self.find_document(document.doc_id) is not None
        now = utc_now().strftime("%Y-%m-%dT%H:%M:%S.%f%z")
        self.execute(
            """
            INSERT INTO kb_documents
                (doc_id, source, title, media_type, size_bytes, chunk_count,
                 content_hash, ingested_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (doc_id) DO UPDATE SET
                source       = excluded.source,
                title        = excluded.title,
                media_type   = excluded.media_type,
                size_bytes   = excluded.size_bytes,
                chunk_count  = excluded.chunk_count,
                content_hash = excluded.content_hash,
                ingested_at  = excluded.ingested_at
            """,
            (
                document.doc_id,
                document.source,
                document.title,
                document.media_type,
                document.size_bytes,
                chunk_count,
                content_hash,
                now,
            ),
        )
        return existed

    def find_document(self, doc_id: str) -> Row | None:
        """Fetch one document row by id."""
        return self.query_one("SELECT * FROM kb_documents WHERE doc_id = ?", (doc_id,))

    def find_document_by_source(self, source: str) -> Row | None:
        """Fetch one document row by its source path."""
        return self.query_one("SELECT * FROM kb_documents WHERE source = ?", (source,))

    def documents(self, *, limit: int = 200) -> list[Row]:
        """Every ingested document, newest first."""
        return self.query(
            "SELECT doc_id, source, title, media_type, size_bytes, chunk_count, ingested_at "
            "FROM kb_documents ORDER BY ingested_at DESC, id DESC LIMIT ?",
            (limit,),
        )

    def delete_document(self, doc_id: str) -> int:
        """Delete a document and its chunks. Returns the document rows removed."""
        # Chunks first: the foreign key cascade would handle it, but doing it
        # explicitly keeps the behaviour correct even if somebody disables
        # foreign keys on an existing database file.
        self.execute("DELETE FROM kb_chunks WHERE doc_id = ?", (doc_id,))
        return self.execute("DELETE FROM kb_documents WHERE doc_id = ?", (doc_id,))

    def delete_all(self) -> int:
        """Delete the whole corpus."""
        self.execute("DELETE FROM kb_chunks")
        return self.execute("DELETE FROM kb_documents")

    def count_documents(self) -> int:
        """Number of ingested documents."""
        return as_int(self.scalar("SELECT COUNT(*) FROM kb_documents"))

    def count_chunks(self, doc_id: str | None = None) -> int:
        """Number of chunks overall or within one document."""
        if doc_id is None:
            value = self.scalar("SELECT COUNT(*) FROM kb_chunks")
        else:
            value = self.scalar("SELECT COUNT(*) FROM kb_chunks WHERE doc_id = ?", (doc_id,))
        return as_int(value)

    def content_hash(self, doc_id: str) -> str:
        """Stored content hash, so an unchanged file can skip re-embedding."""
        row = self.find_document(doc_id)
        return as_str(row["content_hash"]) if row else ""

    # -- chunks ------------------------------------------------------------

    def replace_chunks(self, doc_id: str, chunks: Sequence[Chunk]) -> int:
        """Replace every chunk of ``doc_id`` atomically.

        One transaction, because a half-replaced document is worse than an
        un-replaced one: the index would then contain chunks from two versions
        of the same file, and retrieval would cite a sentence that no longer
        exists.
        """
        with self.store.transaction() as connection:
            connection.execute("DELETE FROM kb_chunks WHERE doc_id = ?", (doc_id,))
            connection.executemany(
                "INSERT INTO kb_chunks (chunk_id, doc_id, ordinal, text, char_start, char_end) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                [
                    (
                        chunk.chunk_id,
                        chunk.doc_id,
                        chunk.ordinal,
                        chunk.text,
                        chunk.char_start,
                        chunk.char_end,
                    )
                    for chunk in chunks
                ],
            )
        return len(chunks)

    def chunk(self, chunk_id: str) -> Row | None:
        """Fetch one chunk row."""
        return self.query_one("SELECT * FROM kb_chunks WHERE chunk_id = ?", (chunk_id,))

    def chunks_of(self, doc_id: str) -> list[Row]:
        """Every chunk of a document, in order."""
        return self.query("SELECT * FROM kb_chunks WHERE doc_id = ? ORDER BY ordinal", (doc_id,))

    def keyword_search(self, tokens: Sequence[str], *, limit: int) -> list[Row]:
        """Substring search over chunk text.

        The companion to vector search, for the queries embeddings are worst at:
        an exact error code, a product number, a name. ``LIKE`` cannot use an
        index, but a personal corpus is thousands of rows.
        """
        if not tokens or limit <= 0:
            return []
        clauses = " OR ".join("c.text LIKE ?" for _ in tokens)
        params: list[str] = [f"%{token}%" for token in tokens]
        return self.query(
            f"""
            SELECT c.chunk_id, c.doc_id, c.ordinal, c.text, c.char_start, c.char_end,
                   d.source, d.title
              FROM kb_chunks c
              JOIN kb_documents d ON d.doc_id = c.doc_id
             WHERE {clauses}
             LIMIT ?
            """,
            [*params, limit],
        )


__all__ = ["MIGRATIONS", "NAMESPACE", "KnowledgeRepository"]
