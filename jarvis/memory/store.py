"""SQLite persistence for long-term memory.

Two tables, because the two things being stored have different lifetimes:

``memory_records``
    Durable facts, preferences, episodes and summaries. Deduplicated on
    ``(scope, kind, content)`` — saying "我叫张三" five times must not create five
    memories that then crowd out everything else in a top-5 recall.

``memory_turns``
    Raw conversation, kept so a session can be replayed and later compressed.
    Deliberately *not* what recall reads: raw turns are long, redundant and
    mostly small talk, which is exactly what a summary is for.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence

from jarvis.database import (
    Migration,
    Repository,
    Row,
    SqliteStore,
    as_bool,
    as_float,
    as_int,
    as_str,
    utc_now,
)
from jarvis.database.types import SqlScalar
from jarvis.memory.types import ConversationTurn, MemoryKind, MemoryRecord, MemoryScope

logger = logging.getLogger("jarvis.memory.store")

NAMESPACE: str = "memory"

MIGRATIONS: tuple[Migration, ...] = (
    Migration(
        namespace=NAMESPACE,
        version=1,
        statements=(
            """
            CREATE TABLE IF NOT EXISTS memory_records (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                scope        TEXT    NOT NULL,
                kind         TEXT    NOT NULL,
                content      TEXT    NOT NULL,
                source       TEXT    NOT NULL DEFAULT '',
                importance   REAL    NOT NULL DEFAULT 0.5,
                pinned       INTEGER NOT NULL DEFAULT 0,
                created_at   TEXT    NOT NULL,
                updated_at   TEXT    NOT NULL,
                accessed_at  TEXT    NOT NULL,
                access_count INTEGER NOT NULL DEFAULT 0,
                UNIQUE (scope, kind, content)
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_memory_records_scope_kind
                ON memory_records (scope, kind)
            """,
            """
            CREATE TABLE IF NOT EXISTS memory_turns (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT    NOT NULL,
                role       TEXT    NOT NULL,
                content    TEXT    NOT NULL,
                created_at TEXT    NOT NULL
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_memory_turns_session
                ON memory_turns (session_id, id)
            """,
        ),
    ),
)

_COLUMNS = (
    "id, scope, kind, content, source, importance, pinned, "
    "created_at, updated_at, accessed_at, access_count"
)


def _to_record(row: Row) -> MemoryRecord:
    """Translate a row into the domain type, tolerating an unknown enum value.

    A kind written by a newer version of JARVIS (or hand-edited) must not make
    the whole recall fail: the record degrades to ``FACT`` and the anomaly is
    visible in the log rather than in a stack trace.
    """
    raw_kind = as_str(row["kind"])
    try:
        kind = MemoryKind(raw_kind)
    except ValueError:
        logger.warning("unknown memory kind %r; treating as fact", raw_kind)
        kind = MemoryKind.FACT
    raw_scope = as_str(row["scope"])
    try:
        scope = MemoryScope(raw_scope)
    except ValueError:
        scope = MemoryScope.USER
    return MemoryRecord(
        memory_id=as_int(row["id"]),
        kind=kind,
        scope=scope,
        content=as_str(row["content"]),
        source=as_str(row["source"]),
        importance=as_float(row["importance"]),
        pinned=as_bool(row["pinned"]),
        created_at=as_str(row["created_at"]),
        updated_at=as_str(row["updated_at"]),
        accessed_at=as_str(row["accessed_at"]),
        access_count=as_int(row["access_count"]),
    )


class MemoryRepository(Repository):
    """CRUD for memories and conversation turns."""

    def __init__(self, store: SqliteStore) -> None:
        super().__init__(store)

    # -- memories ----------------------------------------------------------

    def upsert(
        self,
        *,
        scope: MemoryScope,
        kind: MemoryKind,
        content: str,
        source: str,
        importance: float,
        pinned: bool,
    ) -> MemoryRecord:
        """Insert a memory, or refresh the one that already says the same thing.

        The conflict path raises ``importance`` to the higher of the two rather
        than overwriting: repeating a fact is evidence it matters, and losing a
        previously-set 0.9 to a fresh default 0.5 would be a regression.
        """
        now = utc_now().strftime("%Y-%m-%dT%H:%M:%S.%f%z")
        self.execute(
            """
            INSERT INTO memory_records
                (scope, kind, content, source, importance, pinned,
                 created_at, updated_at, accessed_at, access_count)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
            ON CONFLICT (scope, kind, content) DO UPDATE SET
                importance = MAX(memory_records.importance, excluded.importance),
                pinned     = MAX(memory_records.pinned, excluded.pinned),
                source     = excluded.source,
                updated_at = excluded.updated_at
            """,
            (scope.value, kind.value, content, source, importance, int(pinned), now, now, now),
        )
        record = self.find(scope=scope, kind=kind, content=content)
        if record is None:  # pragma: no cover - the upsert just wrote it
            raise AssertionError("upsert did not produce a row")
        return record

    def find(self, *, scope: MemoryScope, kind: MemoryKind, content: str) -> MemoryRecord | None:
        """Look up the exact ``(scope, kind, content)`` triple."""
        row = self.query_one(
            f"SELECT {_COLUMNS} FROM memory_records WHERE scope = ? AND kind = ? AND content = ?",
            (scope.value, kind.value, content),
        )
        return _to_record(row) if row else None

    def get(self, memory_id: int) -> MemoryRecord | None:
        """Fetch one memory by id."""
        row = self.query_one(f"SELECT {_COLUMNS} FROM memory_records WHERE id = ?", (memory_id,))
        return _to_record(row) if row else None

    def all_for(
        self,
        scope: MemoryScope,
        *,
        kinds: Sequence[MemoryKind] | None = None,
        limit: int = 0,
    ) -> list[MemoryRecord]:
        """List memories in a scope, most important first.

        Args:
            scope: Bucket to read.
            kinds: Optional filter; ``None`` means every kind.
            limit: Maximum rows; ``0`` means no limit.
        """
        sql = f"SELECT {_COLUMNS} FROM memory_records WHERE scope = ?"
        params: list[SqlScalar] = [scope.value]
        if kinds:
            placeholders = ", ".join("?" for _ in kinds)
            sql += f" AND kind IN ({placeholders})"
            params.extend(kind.value for kind in kinds)
        sql += " ORDER BY pinned DESC, importance DESC, updated_at DESC"
        if limit > 0:
            sql += " LIMIT ?"
            params.append(limit)
        return [_to_record(row) for row in self.query(sql, params)]

    def keyword_search(
        self,
        scope: MemoryScope,
        tokens: Sequence[str],
        *,
        limit: int,
    ) -> list[MemoryRecord]:
        """Substring search over memory content.

        The fallback that keeps recall working when the embedder is unavailable
        or when the query is an exact string the hashing embedder blurs away
        (a name, an order number). ``LIKE`` cannot use an index here, but the
        table is a personal memory — hundreds of rows, not millions.
        """
        if not tokens or limit <= 0:
            return []
        clauses = " OR ".join("content LIKE ?" for _ in tokens)
        params: list[SqlScalar] = [scope.value]
        params.extend(f"%{token}%" for token in tokens)
        rows = self.query(
            f"SELECT {_COLUMNS} FROM memory_records WHERE scope = ? AND ({clauses}) "
            "ORDER BY pinned DESC, importance DESC LIMIT ?",
            [*params, limit],
        )
        return [_to_record(row) for row in rows]

    def pinned(self, scope: MemoryScope) -> list[MemoryRecord]:
        """Every pinned memory in a scope (always injected, never ranked away)."""
        rows = self.query(
            f"SELECT {_COLUMNS} FROM memory_records WHERE scope = ? AND pinned = 1 "
            "ORDER BY updated_at DESC",
            (scope.value,),
        )
        return [_to_record(row) for row in rows]

    def touch(self, memory_ids: Sequence[int]) -> None:
        """Record that these memories were just used.

        Access count feeds the recall ranking, so a memory that keeps proving
        useful drifts upward without anybody deciding it should.
        """
        if not memory_ids:
            return
        now = utc_now().strftime("%Y-%m-%dT%H:%M:%S.%f%z")
        placeholders = ", ".join("?" for _ in memory_ids)
        self.execute(
            f"UPDATE memory_records SET accessed_at = ?, access_count = access_count + 1 "
            f"WHERE id IN ({placeholders})",
            [now, *memory_ids],
        )

    def delete(self, memory_id: int) -> bool:
        """Delete one memory. Returns whether a row disappeared."""
        return self.execute("DELETE FROM memory_records WHERE id = ?", (memory_id,)) > 0

    def delete_scope(self, scope: MemoryScope) -> int:
        """Delete every memory in a scope."""
        return self.execute("DELETE FROM memory_records WHERE scope = ?", (scope.value,))

    def prune(self, scope: MemoryScope, *, keep: int) -> int:
        """Drop the least valuable memories once a scope exceeds ``keep``.

        Pinned rows are never candidates; ties break on access count first, so
        a memory nobody ever recalled goes before one that keeps being used.
        """
        if keep <= 0:
            return 0
        total = self.count(scope)
        if total <= keep:
            return 0
        return self.execute(
            """
            DELETE FROM memory_records
             WHERE pinned = 0
               AND scope = ?
               AND id IN (
                   SELECT id FROM memory_records
                    WHERE scope = ? AND pinned = 0
                    ORDER BY access_count ASC, importance ASC, updated_at ASC
                    LIMIT ?
               )
            """,
            (scope.value, scope.value, total - keep),
        )

    def count(self, scope: MemoryScope | None = None) -> int:
        """Number of memories overall or in one scope."""
        if scope is None:
            value = self.scalar("SELECT COUNT(*) FROM memory_records")
        else:
            value = self.scalar(
                "SELECT COUNT(*) FROM memory_records WHERE scope = ?", (scope.value,)
            )
        return as_int(value)

    def count_by_kind(self) -> dict[str, int]:
        """Memory counts keyed by kind, for the HUD's storage panel."""
        rows = self.query(
            "SELECT kind, COUNT(*) AS total FROM memory_records GROUP BY kind ORDER BY kind"
        )
        return {as_str(row["kind"]): as_int(row["total"]) for row in rows}

    # -- turns -------------------------------------------------------------

    def append_turn(self, session_id: str, role: str, content: str) -> ConversationTurn:
        """Persist one conversation turn."""
        now = utc_now().strftime("%Y-%m-%dT%H:%M:%S.%f%z")
        turn_id = self.insert(
            "INSERT INTO memory_turns (session_id, role, content, created_at) VALUES (?, ?, ?, ?)",
            (session_id, role, content, now),
        )
        return ConversationTurn(
            turn_id=turn_id, session_id=session_id, role=role, content=content, created_at=now
        )

    def recent_turns(self, session_id: str, *, limit: int) -> list[ConversationTurn]:
        """The last ``limit`` turns of a session, oldest first.

        Read newest-first in SQL and reversed in Python: ``ORDER BY id DESC
        LIMIT n`` uses the index, while ``ORDER BY id ASC LIMIT n`` would return
        the *first* n turns of the session rather than the most recent ones.
        """
        if limit <= 0:
            return []
        rows = self.query(
            "SELECT id, session_id, role, content, created_at FROM memory_turns "
            "WHERE session_id = ? ORDER BY id DESC LIMIT ?",
            (session_id, limit),
        )
        turns = [
            ConversationTurn(
                turn_id=as_int(row["id"]),
                session_id=as_str(row["session_id"]),
                role=as_str(row["role"]),
                content=as_str(row["content"]),
                created_at=as_str(row["created_at"]),
            )
            for row in rows
        ]
        turns.reverse()
        return turns

    def count_turns(self, session_id: str) -> int:
        """How many turns a session has accumulated."""
        value = self.scalar("SELECT COUNT(*) FROM memory_turns WHERE session_id = ?", (session_id,))
        return as_int(value)

    def sessions(self, *, limit: int = 20) -> list[dict[str, object]]:
        """Recent sessions with their turn counts, newest first."""
        rows = self.query(
            """
            SELECT session_id,
                   COUNT(*)   AS turns,
                   MIN(created_at) AS started_at,
                   MAX(created_at) AS last_at
              FROM memory_turns
             GROUP BY session_id
             ORDER BY last_at DESC
             LIMIT ?
            """,
            (limit,),
        )
        return [
            {
                "session_id": as_str(row["session_id"]),
                "turns": as_int(row["turns"]),
                "started_at": as_str(row["started_at"]),
                "last_at": as_str(row["last_at"]),
            }
            for row in rows
        ]

    def clear_session(self, session_id: str) -> int:
        """Delete every turn of one session."""
        return self.execute("DELETE FROM memory_turns WHERE session_id = ?", (session_id,))


__all__ = ["MIGRATIONS", "NAMESPACE", "MemoryRepository"]
