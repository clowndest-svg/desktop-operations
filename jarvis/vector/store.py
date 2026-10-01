"""SQLite-backed vector index.

Why not FAISS (which ``docs/architecture.md`` originally named): FAISS is a
native wheel that has to be built per Python version, it cannot be installed by
``pip install -e .[voice]`` on a fresh machine without a compiler, and the
indexes it produces are opaque files that cannot be inspected when a retrieval
result looks wrong. A ``BLOB`` column in the database JARVIS already opens has
none of those problems, and at the scale this assistant operates on — thousands
of chunks, not millions — a linear scan over ``struct.unpack`` is well under a
millisecond per query.

The protocol-shaped seam is :class:`jarvis.vector.types.Embedder`, so swapping in
FAISS later is a new class implementing the same four methods, not a rewrite.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping, Sequence
from typing import Any, cast

from jarvis.core.exceptions import VectorStoreError
from jarvis.database import Migration, Repository, Row, SqliteStore, SqlParams, utc_now
from jarvis.vector.types import (
    SearchHit,
    Vector,
    VectorEntry,
    decode_vector,
    dot,
    encode_vector,
    l2_normalize,
)

logger = logging.getLogger("jarvis.vector.store")


def _int_of(value: object) -> int:
    """Coerce a column value to ``int``, treating NULL/unexpected as 0.

    ``MAX(...)`` over an empty group returns NULL and ``COUNT(*)`` comes back as
    an int — both are legitimate, and neither should require a ``type: ignore``
    at every call site.
    """
    if isinstance(value, int | float | str | bytes):
        return int(value)
    return 0


NAMESPACE: str = "vector"
"""Migration namespace; also the table prefix."""

MIGRATIONS: tuple[Migration, ...] = (
    Migration(
        namespace=NAMESPACE,
        version=1,
        statements=(
            """
            CREATE TABLE IF NOT EXISTS vector_records (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                collection TEXT    NOT NULL,
                record_id  TEXT    NOT NULL,
                text       TEXT    NOT NULL,
                metadata   TEXT    NOT NULL DEFAULT '{}',
                vector     BLOB    NOT NULL,
                dimension  INTEGER NOT NULL,
                engine     TEXT    NOT NULL DEFAULT '',
                created_at TEXT    NOT NULL,
                UNIQUE (collection, record_id)
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_vector_records_collection
                ON vector_records (collection)
            """,
        ),
    ),
)


def _try_numpy() -> object | None:
    """Return the numpy module when it is already installed, else ``None``.

    numpy arrives with the voice extra (``funasr`` needs it), so on many installs
    the fast path is free. It is never *required*: importing it lazily keeps a
    text-only install from acquiring a native dependency through the back door.
    """
    try:  # pragma: no cover - depends on the environment
        import numpy
    except ImportError:
        return None
    return numpy


class SqliteVectorStore(Repository):
    """Stores embeddings in the shared database and ranks by cosine similarity."""

    def __init__(self, store: SqliteStore) -> None:
        super().__init__(store)
        self._numpy = _try_numpy()

    # -- writes ------------------------------------------------------------

    def upsert(
        self,
        collection: str,
        entries: Sequence[VectorEntry],
        vectors: Sequence[Vector],
        *,
        engine: str = "",
    ) -> int:
        """Insert or replace records, keyed by ``(collection, record_id)``.

        Vectors are normalised on write, so :meth:`search` is a plain dot
        product. Doing it once here rather than per query is the difference
        between O(rows) square roots and zero.

        Raises:
            VectorStoreError: if ``entries`` and ``vectors`` differ in length or
                any vector has an inconsistent width.
        """
        if len(entries) != len(vectors):
            raise VectorStoreError(
                f"条目与向量数量不一致：{len(entries)} vs {len(vectors)}",
                details={"collection": collection},
            )
        if not entries:
            return 0
        widths = {len(vector) for vector in vectors}
        if len(widths) > 1:
            raise VectorStoreError(
                "同一批次内向量维度不一致",
                details={"collection": collection, "widths": sorted(widths)},
            )
        now = utc_now().strftime("%Y-%m-%dT%H:%M:%S.%f%z")
        rows: list[tuple[object, ...]] = []
        for entry, vector in zip(entries, vectors, strict=True):
            rows.append(
                (
                    collection,
                    entry.record_id,
                    entry.text,
                    json.dumps(dict(entry.metadata), ensure_ascii=False),
                    encode_vector(l2_normalize(vector)),
                    len(vector),
                    engine,
                    now,
                )
            )
        written = self.execute_many(
            """
            INSERT INTO vector_records
                (collection, record_id, text, metadata, vector, dimension, engine, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (collection, record_id) DO UPDATE SET
                text       = excluded.text,
                metadata   = excluded.metadata,
                vector     = excluded.vector,
                dimension  = excluded.dimension,
                engine     = excluded.engine,
                created_at = excluded.created_at
            """,
            cast("list[SqlParams]", rows),
        )
        logger.debug("indexed %d record(s) into %r", len(rows), collection)
        return written

    def delete(self, collection: str, record_ids: Sequence[str]) -> int:
        """Remove specific records. Returns how many rows disappeared."""
        if not record_ids:
            return 0
        placeholders = ", ".join("?" for _ in record_ids)
        return self.execute(
            f"DELETE FROM vector_records WHERE collection = ? AND record_id IN ({placeholders})",
            [collection, *record_ids],
        )

    def delete_collection(self, collection: str) -> int:
        """Drop a whole collection (used when a document is re-ingested)."""
        return self.execute("DELETE FROM vector_records WHERE collection = ?", (collection,))

    # -- reads -------------------------------------------------------------

    def search(
        self,
        collection: str,
        query: Vector,
        *,
        top_k: int = 5,
        min_score: float = 0.0,
    ) -> list[SearchHit]:
        """Rank a collection against ``query``; highest similarity first.

        Raises:
            VectorStoreError: if the query width does not match the index, which
                means the configured embedder changed under an existing index.
        """
        if top_k <= 0 or not query:
            return []
        rows = self.query(
            """
            SELECT record_id, text, metadata, vector, dimension
              FROM vector_records
             WHERE collection = ?
            """,
            (collection,),
        )
        if not rows:
            return []
        normalized = l2_normalize(query)
        expected = len(normalized)
        scored = self._score_all(rows, normalized, collection=collection, expected=expected)
        hits = [
            SearchHit(
                record_id=str(row["record_id"]),
                score=score,
                text=str(row["text"]),
                metadata=self._metadata(row["metadata"]),
            )
            for row, score in scored
            if score >= min_score
        ]
        hits.sort(key=lambda hit: hit.score, reverse=True)
        return hits[:top_k]

    def _score_all(
        self,
        rows: Sequence[Row],
        normalized: Vector,
        *,
        collection: str,
        expected: int,
    ) -> list[tuple[Row, float]]:
        """Score every row, validating widths before any comparison happens.

        A width mismatch means the configured embedder changed while an index
        built by the previous one is still on disk. Comparing them would produce
        nonsense rankings, so the whole query is refused with a message that says
        what to do about it.
        """
        pairs: list[tuple[Row, bytes]] = []
        for row in rows:
            blob = row["vector"]
            if not isinstance(blob, bytes):
                continue
            width = _int_of(row["dimension"])
            if width != expected:
                raise VectorStoreError(
                    f"查询向量维度 {expected} 与索引维度 {width} 不一致："
                    "嵌入模型换了，需要重建该集合",
                    details={"collection": collection, "record_id": row["record_id"]},
                )
            pairs.append((row, blob))
        if not pairs:
            return []

        numpy = self._numpy
        if numpy is not None:  # pragma: no cover - depends on the environment
            return self._score_with_numpy(pairs, normalized, numpy)
        return [(row, dot(normalized, decode_vector(blob))) for row, blob in pairs]

    @staticmethod
    def _score_with_numpy(
        pairs: Sequence[tuple[Row, bytes]],
        normalized: Vector,
        numpy: Any,
    ) -> list[tuple[Row, float]]:
        """Matrix-multiply path, used only when numpy is already installed."""
        matrix = numpy.array([numpy.frombuffer(blob, dtype="<f4") for _, blob in pairs])
        scores = matrix @ numpy.array(normalized, dtype="<f4")
        return [(row, float(score)) for (row, _), score in zip(pairs, scores, strict=True)]

    def get(self, collection: str, record_id: str) -> SearchHit | None:
        """Fetch one record without scoring it."""
        row = self.query_one(
            "SELECT record_id, text, metadata FROM vector_records "
            "WHERE collection = ? AND record_id = ?",
            (collection, record_id),
        )
        if row is None:
            return None
        return SearchHit(
            record_id=str(row["record_id"]),
            score=0.0,
            text=str(row["text"]),
            metadata=self._metadata(row["metadata"]),
        )

    def count(self, collection: str | None = None) -> int:
        """Number of indexed records, optionally restricted to one collection."""
        if collection is None:
            value = self.scalar("SELECT COUNT(*) FROM vector_records")
        else:
            value = self.scalar(
                "SELECT COUNT(*) FROM vector_records WHERE collection = ?", (collection,)
            )
        return _int_of(value)

    def collections(self) -> list[dict[str, object]]:
        """Per-collection summary, ordered by name."""
        rows = self.query("""
            SELECT collection,
                   COUNT(*)      AS records,
                   MAX(dimension) AS dimension,
                   MAX(engine)   AS engine,
                   MAX(created_at) AS updated_at
              FROM vector_records
             GROUP BY collection
             ORDER BY collection
            """)
        return [
            {
                "collection": str(row["collection"]),
                "records": _int_of(row["records"]),
                "dimension": _int_of(row["dimension"]),
                "engine": str(row["engine"] or ""),
                "updated_at": str(row["updated_at"] or ""),
            }
            for row in rows
        ]

    def dimension_of(self, collection: str) -> int:
        """Width of the vectors in ``collection`` (0 when empty)."""
        return _int_of(
            self.scalar(
                "SELECT MAX(dimension) FROM vector_records WHERE collection = ?", (collection,)
            )
        )

    # -- helpers -----------------------------------------------------------

    @staticmethod
    def _metadata(raw: object) -> Mapping[str, object]:
        """Decode a metadata column, tolerating hand-edited values."""
        if not isinstance(raw, str) or not raw:
            return {}
        try:
            decoded = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("ignoring malformed vector metadata: %r", raw[:80])
            return {}
        return decoded if isinstance(decoded, dict) else {}


__all__ = ["MIGRATIONS", "NAMESPACE", "SqliteVectorStore"]
