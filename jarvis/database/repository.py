"""Repository base class: the only sanctioned way to touch SQL.

Repositories translate between Python values and rows; they do not open
connections, retry, or decide transactions. All of that belongs to
:class:`jarvis.database.store.SqliteStore`, which owns the single connection and
the lock that serialises access to it.

Subclasses implement *queries*; the helpers here keep the boilerplate (fetch
mode, error translation, ``lastrowid`` vs ``rowcount``) in one place.
"""

from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING

from jarvis.core.exceptions import DatabaseError
from jarvis.database.types import Row, SqlParams

if TYPE_CHECKING:  # pragma: no cover - typing only
    from jarvis.database.store import SqliteStore


class Repository:
    """Base class for every SQLite-backed store in the project."""

    def __init__(self, store: SqliteStore) -> None:
        self._store = store

    @property
    def store(self) -> SqliteStore:
        """The owning store (subclasses use it to open transactions)."""
        return self._store

    # -- reads -------------------------------------------------------------

    def query(self, sql: str, params: SqlParams = ()) -> list[Row]:
        """Run a SELECT and return every row as a mapping.

        Raises:
            DatabaseError: if the statement fails.
        """
        try:
            with self._store.connection() as connection:
                cursor = connection.execute(sql, params)
                return [dict(row) for row in cursor.fetchall()]
        except sqlite3.Error as exc:
            raise self._wrap("查询", sql, exc) from exc

    def query_one(self, sql: str, params: SqlParams = ()) -> Row | None:
        """Run a SELECT expected to yield at most one row."""
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def scalar(self, sql: str, params: SqlParams = ()) -> object:
        """Run a SELECT that yields a single value (``COUNT(*)`` and friends)."""
        try:
            with self._store.connection() as connection:
                row = connection.execute(sql, params).fetchone()
        except sqlite3.Error as exc:
            raise self._wrap("查询", sql, exc) from exc
        if row is None:
            return None
        return row[0]

    # -- writes ------------------------------------------------------------

    def execute(self, sql: str, params: SqlParams = ()) -> int:
        """Run a write statement; returns the number of affected rows."""
        try:
            with self._store.connection() as connection:
                cursor = connection.execute(sql, params)
                return int(cursor.rowcount)
        except sqlite3.Error as exc:
            raise self._wrap("写入", sql, exc) from exc

    def insert(self, sql: str, params: SqlParams = ()) -> int:
        """Run an INSERT; returns the new ``rowid``.

        Raises:
            DatabaseError: if the insert produced no rowid (which would mean the
                statement was not an INSERT).
        """
        try:
            with self._store.connection() as connection:
                cursor = connection.execute(sql, params)
                rowid = cursor.lastrowid
        except sqlite3.Error as exc:
            raise self._wrap("写入", sql, exc) from exc
        if rowid is None:
            raise DatabaseError("插入语句没有返回 rowid", details={"sql": sql[:200]})
        return int(rowid)

    def execute_many(self, sql: str, rows: list[SqlParams]) -> int:
        """Run the same statement for many parameter sets in one transaction."""
        if not rows:
            return 0
        try:
            with self._store.transaction() as connection:
                before = connection.total_changes
                connection.executemany(sql, rows)
                return int(connection.total_changes - before)
        except sqlite3.Error as exc:
            raise self._wrap("批量写入", sql, exc) from exc

    # -- helpers -----------------------------------------------------------

    @staticmethod
    def _wrap(action: str, sql: str, exc: sqlite3.Error) -> DatabaseError:
        return DatabaseError(
            f"数据库{action}失败：{exc}",
            details={"sql": " ".join(sql.split())[:200], "reason": str(exc)},
        )
