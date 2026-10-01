"""Forward-only migration framework.

Each capability package owns its own schema and hands its statements to
:meth:`jarvis.database.store.SqliteStore.migrate` at start-up. Migrations are
keyed by ``(namespace, version)`` rather than by a single global counter, so
``memory`` and ``knowledge`` can each add tables without knowing about — or
being blocked by — the other.

Two rules make this safe to call on every boot:

* A migration is applied **once**; the ledger in ``schema_migrations`` records
  what has run.
* Migrations are **append-only**. Editing a statement that already shipped
  changes nothing on an existing machine, so a correction has to be a new
  version. :func:`validate_migrations` rejects the obvious mistakes (duplicate
  versions, gaps, non-positive numbers) at import-adjacent time rather than
  letting them surface as a half-built table.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass

from jarvis.core.exceptions import DatabaseError

logger = logging.getLogger("jarvis.database.migrations")

LEDGER_TABLE: str = "schema_migrations"
"""Where applied migrations are recorded."""


@dataclass(frozen=True, slots=True)
class Migration:
    """One irreversible step forward in a namespace's schema."""

    namespace: str
    """Owning capability, e.g. ``memory``. Matches the table prefix."""

    version: int
    """Monotonic version, starting at 1 and increasing by one."""

    statements: tuple[str, ...]
    """DDL/DML executed in order inside a single transaction."""

    @property
    def label(self) -> str:
        """``memory@3`` — used in log lines and error details."""
        return f"{self.namespace}@{self.version}"


def validate_migrations(namespace: str, migrations: Sequence[Migration]) -> None:
    """Check a namespace's migration list is well-formed.

    Raises:
        DatabaseError: on a wrong namespace, a non-positive version, a
            duplicate version, or a gap in the sequence.
    """
    versions = sorted(migration.version for migration in migrations)
    for migration in migrations:
        if migration.namespace != namespace:
            raise DatabaseError(
                f"迁移命名空间不匹配：{migration.label} 出现在 {namespace} 列表中",
                details={"expected": namespace, "actual": migration.namespace},
            )
        if migration.version < 1:
            raise DatabaseError(
                f"迁移版本号必须 >= 1：{migration.label}",
                details={"version": migration.version},
            )
        if not migration.statements:
            raise DatabaseError(f"迁移没有任何语句：{migration.label}")
    if len(set(versions)) != len(versions):
        raise DatabaseError(f"{namespace} 存在重复的迁移版本号", details={"versions": versions})
    if versions and versions != list(range(1, versions[-1] + 1)):
        raise DatabaseError(
            f"{namespace} 迁移版本号不连续（必须从 1 开始逐个递增）",
            details={"versions": versions},
        )


class MigrationRunner:
    """Applies pending migrations for one namespace against one connection."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def ensure_ledger(self) -> None:
        """Create the bookkeeping table if this is a fresh database."""
        self._connection.execute(f"""
            CREATE TABLE IF NOT EXISTS {LEDGER_TABLE} (
                namespace  TEXT    NOT NULL,
                version    INTEGER NOT NULL,
                applied_at TEXT    NOT NULL DEFAULT (datetime('now')),
                PRIMARY KEY (namespace, version)
            )
            """)

    def applied(self, namespace: str) -> set[int]:
        """Versions of ``namespace`` already recorded as applied."""
        self.ensure_ledger()
        rows = self._connection.execute(
            f"SELECT version FROM {LEDGER_TABLE} WHERE namespace = ?", (namespace,)
        ).fetchall()
        return {int(row["version"]) for row in rows}

    def apply(self, namespace: str, migrations: Sequence[Migration]) -> tuple[int, ...]:
        """Run every pending migration, oldest first.

        Returns:
            The versions that were actually applied, in order (empty when the
            schema is already current — the normal case on every boot after the
            first).

        Raises:
            DatabaseError: if validation fails or any statement errors. The
                failing migration's transaction is rolled back, so the database
                is left at the last good version rather than half-migrated.
        """
        validate_migrations(namespace, migrations)
        done = self.applied(namespace)
        pending = sorted(
            (migration for migration in migrations if migration.version not in done),
            key=lambda migration: migration.version,
        )
        if not pending:
            return ()

        applied: list[int] = []
        for migration in pending:
            try:
                self._connection.execute("BEGIN")
                for statement in migration.statements:
                    self._connection.execute(statement)
                self._connection.execute(
                    f"INSERT INTO {LEDGER_TABLE} (namespace, version) VALUES (?, ?)",
                    (namespace, migration.version),
                )
                self._connection.execute("COMMIT")
            except sqlite3.Error as exc:
                self._connection.execute("ROLLBACK")
                raise DatabaseError(
                    f"迁移失败：{migration.label}",
                    details={
                        "namespace": namespace,
                        "version": migration.version,
                        "reason": str(exc),
                    },
                ) from exc
            logger.info("applied migration %s", migration.label)
            applied.append(migration.version)
        return tuple(applied)
