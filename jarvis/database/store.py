"""``SqliteStore`` — the one object that owns the database connection.

Why a single shared connection instead of a pool: JARVIS is a desktop assistant
with three writers at most (UI thread, scheduler thread, voice thread) and a
workload measured in single-digit statements per second. A pool would buy
nothing and cost a class of bugs ("database is locked") that a lock plus
``check_same_thread=False`` simply does not have.

The lock is re-entrant, and every public entry point takes it, so a repository
method called from the scheduler cannot interleave with one called from the JS
bridge.
"""

from __future__ import annotations

import contextlib
import logging
import sqlite3
import threading
from collections.abc import Iterator, Sequence
from pathlib import Path

from jarvis.core.exceptions import DatabaseError
from jarvis.database.connection import open_connection
from jarvis.database.migrations import Migration, MigrationRunner
from jarvis.database.repository import Repository

logger = logging.getLogger("jarvis.database.store")

DEFAULT_DB_FILENAME: str = "jarvis.db"
"""File name used under ``<data>/database`` when config does not say otherwise."""


class SqliteStore:
    """Lifecycle component wrapping one SQLite connection and its migrations.

    Registered in the composition root like every other service: ``start()``
    opens the file and is idempotent, ``stop()`` closes it. Nothing touches the
    filesystem at construction time, so building the object before the data
    directory exists is safe.
    """

    name = "database"

    def __init__(
        self,
        path: Path,
        *,
        busy_timeout_ms: int = 5000,
        journal_mode: str = "WAL",
        synchronous: str = "NORMAL",
    ) -> None:
        self._path = path
        self._busy_timeout_ms = busy_timeout_ms
        self._journal_mode = journal_mode
        self._synchronous = synchronous
        self._connection: sqlite3.Connection | None = None
        self._lock = threading.RLock()

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        """Open the connection and apply the base pragmas (idempotent)."""
        with self._lock:
            if self._connection is not None:
                return
            self._connection = open_connection(
                self._path,
                busy_timeout_ms=self._busy_timeout_ms,
                journal_mode=self._journal_mode,
                synchronous=self._synchronous,
            )
        logger.info("database ready at %s", self._path)

    def stop(self) -> None:
        """Close the connection. Safe to call twice."""
        with self._lock:
            if self._connection is None:
                return
            with contextlib.suppress(sqlite3.Error):
                self._connection.close()
            self._connection = None

    @property
    def running(self) -> bool:
        """Whether the connection is open."""
        return self._connection is not None

    @property
    def path(self) -> Path:
        """Location of the database file (``:memory:`` in tests)."""
        return self._path

    # -- access ------------------------------------------------------------

    def _require(self) -> sqlite3.Connection:
        connection = self._connection
        if connection is None:
            raise DatabaseError("数据库未启动：请先调用 start()")
        return connection

    @contextlib.contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        """Yield the connection with the write lock held.

        Raises:
            DatabaseError: if the store has not been started.
        """
        with self._lock:
            yield self._require()

    @contextlib.contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Yield the connection inside an explicit transaction.

        Commits on a clean exit, rolls back on any exception. Used where several
        statements must land together — writing a document *and* its chunks, for
        instance, where a half-ingested file would be worse than a failed one.

        Raises:
            DatabaseError: if the store has not been started.
        """
        with self._lock:
            connection = self._require()
            connection.execute("BEGIN")
            try:
                yield connection
            except BaseException:
                connection.execute("ROLLBACK")
                raise
            connection.execute("COMMIT")

    # -- schema ------------------------------------------------------------

    def migrate(self, namespace: str, migrations: Sequence[Migration]) -> tuple[int, ...]:
        """Bring ``namespace`` up to date; returns the versions applied.

        Idempotent by design: the composition root calls this on every boot and
        an already-current schema returns an empty tuple without touching data.

        Raises:
            DatabaseError: if the store is stopped, the list is malformed, or a
                statement fails.
        """
        with self._lock:
            runner = MigrationRunner(self._require())
            return runner.apply(namespace, migrations)

    def schema_version(self, namespace: str) -> int:
        """Highest applied version for ``namespace`` (0 when untouched)."""
        with self._lock:
            runner = MigrationRunner(self._require())
            applied = runner.applied(namespace)
        return max(applied) if applied else 0

    def repository(self) -> Repository:
        """A plain repository bound to this store (for ad-hoc SQL)."""
        return Repository(self)
