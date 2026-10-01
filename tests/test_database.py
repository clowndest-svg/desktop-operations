"""Tests for the persistence layer (jarvis.database)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from jarvis.core.exceptions import DatabaseError
from jarvis.database import (
    Migration,
    Repository,
    SqliteStore,
    format_timestamp,
    parse_timestamp,
    utc_now,
    validate_migrations,
)
from jarvis.database.connection import open_connection


def _store(tmp_path: Path) -> SqliteStore:
    store = SqliteStore(tmp_path / "test.db")
    store.start()
    return store


def _migration(version: int, table: str) -> Migration:
    return Migration(
        namespace="demo",
        version=version,
        statements=(f"CREATE TABLE {table} (id INTEGER PRIMARY KEY, value TEXT)",),
    )


class TestConnection:
    def test_foreign_keys_are_on(self) -> None:
        """SQLite ships with referential integrity OFF; every REFERENCES clause
        would be a comment if the pragma were not applied."""
        connection = open_connection(
            Path(":memory:"), busy_timeout_ms=1000, journal_mode="MEMORY", synchronous="NORMAL"
        )
        try:
            assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        finally:
            connection.close()

    def test_busy_timeout_is_applied(self) -> None:
        connection = open_connection(
            Path(":memory:"), busy_timeout_ms=4321, journal_mode="MEMORY", synchronous="NORMAL"
        )
        try:
            assert connection.execute("PRAGMA busy_timeout").fetchone()[0] == 4321
        finally:
            connection.close()

    def test_unopenable_path_raises_database_error(self, tmp_path: Path) -> None:
        blocker = tmp_path / "blocker"
        blocker.write_text("not a directory", encoding="utf-8")
        with pytest.raises(DatabaseError):
            open_connection(
                blocker / "nested" / "x.db",
                busy_timeout_ms=1000,
                journal_mode="MEMORY",
                synchronous="NORMAL",
            )


class TestMigrationValidation:
    def test_rejects_wrong_namespace(self) -> None:
        with pytest.raises(DatabaseError, match="命名空间"):
            validate_migrations("demo", [Migration("other", 1, ("SELECT 1",))])

    def test_rejects_duplicate_versions(self) -> None:
        with pytest.raises(DatabaseError, match="重复"):
            validate_migrations("demo", [_migration(1, "a"), _migration(1, "b")])

    def test_rejects_a_gap(self) -> None:
        """A missing version means somebody edited a shipped migration instead of
        appending one; catching it here beats discovering a half-built table."""
        with pytest.raises(DatabaseError, match="不连续"):
            validate_migrations("demo", [_migration(1, "a"), _migration(3, "c")])

    def test_rejects_zero_version(self) -> None:
        with pytest.raises(DatabaseError, match=">= 1"):
            validate_migrations("demo", [Migration("demo", 0, ("SELECT 1",))])

    def test_rejects_empty_statement_list(self) -> None:
        with pytest.raises(DatabaseError, match="没有任何语句"):
            validate_migrations("demo", [Migration("demo", 1, ())])


class TestMigrationRunner:
    def test_applies_once_then_is_a_no_op(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        try:
            assert store.migrate("demo", [_migration(1, "a")]) == (1,)
            assert store.migrate("demo", [_migration(1, "a")]) == ()
            assert store.schema_version("demo") == 1
        finally:
            store.stop()

    def test_applies_pending_versions_in_order(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        try:
            store.migrate("demo", [_migration(1, "a")])
            assert store.migrate("demo", [_migration(1, "a"), _migration(2, "b")]) == (2,)
            assert store.schema_version("demo") == 2
        finally:
            store.stop()

    def test_namespaces_are_independent(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        try:
            store.migrate("alpha", [Migration("alpha", 1, ("CREATE TABLE alpha_t (id INTEGER)",))])
            store.migrate("beta", [Migration("beta", 1, ("CREATE TABLE beta_t (id INTEGER)",))])
            assert store.schema_version("alpha") == 1
            assert store.schema_version("beta") == 1
        finally:
            store.stop()

    def test_failure_rolls_back_and_leaves_version_untouched(self, tmp_path: Path) -> None:
        """A half-applied migration is worse than a failed one: the next boot
        would skip it because the ledger said it ran."""
        store = _store(tmp_path)
        try:
            bad = Migration(
                "demo",
                1,
                (
                    "CREATE TABLE good (id INTEGER)",
                    "THIS IS NOT SQL",
                ),
            )
            with pytest.raises(DatabaseError, match="迁移失败"):
                store.migrate("demo", [bad])
            assert store.schema_version("demo") == 0
            with store.connection() as connection:
                tables = {
                    row["name"]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }
            assert "good" not in tables
        finally:
            store.stop()

    def test_ledger_records_the_namespace(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        try:
            store.migrate("demo", [_migration(1, "a")])
            with store.connection() as connection:
                rows = connection.execute(
                    "SELECT namespace, version FROM schema_migrations"
                ).fetchall()
            assert [(row["namespace"], row["version"]) for row in rows] == [("demo", 1)]
        finally:
            store.stop()


class TestLifecycle:
    def test_operations_before_start_raise(self, tmp_path: Path) -> None:
        store = SqliteStore(tmp_path / "x.db")
        with pytest.raises(DatabaseError, match="未启动"), store.connection():
            pass

    def test_start_is_idempotent_and_stop_is_safe(self, tmp_path: Path) -> None:
        store = SqliteStore(tmp_path / "x.db")
        store.start()
        store.start()
        assert store.running
        store.stop()
        store.stop()
        assert not store.running

    def test_creates_parent_directory(self, tmp_path: Path) -> None:
        store = SqliteStore(tmp_path / "deep" / "nested" / "x.db")
        store.start()
        try:
            assert (tmp_path / "deep" / "nested" / "x.db").exists()
        finally:
            store.stop()


class TestRepository:
    def test_insert_query_and_count(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        try:
            store.migrate("demo", [_migration(1, "notes")])
            repo = Repository(store)
            rowid = repo.insert("INSERT INTO notes (value) VALUES (?)", ("你好",))
            assert rowid == 1
            assert repo.query_one("SELECT value FROM notes WHERE id = ?", (rowid,)) == {
                "value": "你好"
            }
            assert repo.scalar("SELECT COUNT(*) FROM notes") == 1
        finally:
            store.stop()

    def test_query_one_returns_none_for_no_rows(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        try:
            store.migrate("demo", [_migration(1, "notes")])
            assert Repository(store).query_one("SELECT * FROM notes") is None
        finally:
            store.stop()

    def test_bad_sql_is_wrapped(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        try:
            with pytest.raises(DatabaseError, match="查询失败"):
                Repository(store).query("SELECT * FROM nope")
        finally:
            store.stop()

    def test_execute_many_is_all_or_nothing(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        try:
            store.migrate("demo", [_migration(1, "notes")])
            repo = Repository(store)
            with pytest.raises(DatabaseError):
                repo.execute_many(
                    "INSERT INTO notes (id, value) VALUES (?, ?)",
                    [(1, "a"), (1, "duplicate id")],
                )
            assert repo.scalar("SELECT COUNT(*) FROM notes") == 0
        finally:
            store.stop()

    def test_transaction_rolls_back_on_exception(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        try:
            store.migrate("demo", [_migration(1, "notes")])
            with pytest.raises(RuntimeError), store.transaction() as connection:
                connection.execute("INSERT INTO notes (value) VALUES ('x')")
                raise RuntimeError("boom")
            assert Repository(store).scalar("SELECT COUNT(*) FROM notes") == 0
        finally:
            store.stop()

    def test_transaction_commits_on_clean_exit(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        try:
            store.migrate("demo", [_migration(1, "notes")])
            with store.transaction() as connection:
                connection.execute("INSERT INTO notes (value) VALUES ('x')")
            assert Repository(store).scalar("SELECT COUNT(*) FROM notes") == 1
        finally:
            store.stop()


class TestTimestamps:
    def test_roundtrip_is_utc_and_aware(self) -> None:
        now = utc_now()
        parsed = parse_timestamp(format_timestamp(now))
        assert parsed.tzinfo is not None
        assert abs((parsed - now).total_seconds()) < 0.001

    def test_naive_datetime_is_treated_as_local(self) -> None:
        naive = utc_now().replace(tzinfo=None)
        assert parse_timestamp(format_timestamp(naive)).tzinfo is not None

    def test_stored_text_sorts_lexicographically(self) -> None:
        """Timestamps are text so an operator can read the file; that only works
        if the format also sorts correctly, which fixed-width ISO 8601 does and
        a variable-width one would not."""
        early = utc_now().replace(microsecond=1)
        late = early.replace(microsecond=2)
        assert format_timestamp(early) < format_timestamp(late)
        assert format_timestamp(early)[:4].isdigit()


class TestJournalMode:
    def test_wal_is_applied_to_a_file_database(self, tmp_path: Path) -> None:
        store = SqliteStore(tmp_path / "wal.db", journal_mode="WAL")
        store.start()
        try:
            with store.connection() as connection:
                assert connection.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        finally:
            store.stop()

    def test_invalid_journal_mode_raises(self, tmp_path: Path) -> None:
        store = SqliteStore(tmp_path / "x.db", journal_mode="NOPE")
        with pytest.raises(DatabaseError):
            store.start()


class TestConcurrency:
    def test_two_threads_can_share_the_connection(self, tmp_path: Path) -> None:
        """``check_same_thread=False`` plus the store's lock is what lets the
        scheduler thread write while the UI thread reads."""
        import threading

        store = _store(tmp_path)
        try:
            store.migrate("demo", [_migration(1, "notes")])
            repo = Repository(store)
            errors: list[Exception] = []

            def worker(index: int) -> None:
                try:
                    for _ in range(20):
                        repo.insert("INSERT INTO notes (value) VALUES (?)", (f"w{index}",))
                except Exception as exc:  # pragma: no cover - failure path
                    errors.append(exc)

            threads = [threading.Thread(target=worker, args=(index,)) for index in range(4)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            assert not errors
            assert repo.scalar("SELECT COUNT(*) FROM notes") == 80
        finally:
            store.stop()


class TestSqliteErrorShape:
    def test_integrity_error_is_translated_not_leaked(self, tmp_path: Path) -> None:
        """Callers catch ``JarvisError``; a raw ``sqlite3`` exception escaping the
        persistence layer would bypass every ``except JarvisError`` above it."""
        store = _store(tmp_path)
        try:
            store.migrate("demo", [_migration(1, "notes")])
            repo = Repository(store)
            repo.insert("INSERT INTO notes (id, value) VALUES (1, 'a')", ())
            with pytest.raises(DatabaseError):
                repo.insert("INSERT INTO notes (id, value) VALUES (1, 'b')", ())
        finally:
            store.stop()

    def test_raw_sqlite_error_never_reaches_callers(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        try:
            with pytest.raises(DatabaseError) as info:
                Repository(store).execute("INSERT INTO missing (x) VALUES (1)")
            assert isinstance(info.value.__cause__, sqlite3.Error)
        finally:
            store.stop()
