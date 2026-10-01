"""SQLite connection factory.

Every connection JARVIS opens goes through :func:`open_connection`, so the
pragmas that make SQLite behave under a desktop workload are set in exactly one
place:

* ``journal_mode=WAL`` — the HUD reads telemetry while the scheduler writes job
  history; the default rollback journal makes those two block each other.
* ``foreign_keys=ON`` — SQLite ships with referential integrity *off*, which
  turns every ``REFERENCES`` clause into a comment unless it is switched on.
* ``busy_timeout`` — a second writer waits instead of raising "database is
  locked" the moment two threads touch the file.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from jarvis.core.exceptions import DatabaseError

VALID_JOURNAL_MODES: frozenset[str] = frozenset(
    {"DELETE", "TRUNCATE", "PERSIST", "MEMORY", "WAL", "OFF"}
)
"""Journal modes SQLite accepts (kept as a set so config can validate against it)."""

VALID_SYNCHRONOUS: frozenset[str] = frozenset({"OFF", "NORMAL", "FULL", "EXTRA"})
"""Durability levels; ``NORMAL`` is the sane default under WAL."""


def open_connection(
    path: Path,
    *,
    busy_timeout_ms: int,
    journal_mode: str,
    synchronous: str,
) -> sqlite3.Connection:
    """Open ``path`` and apply the project-wide pragmas.

    Args:
        path: Database file. ``:memory:`` is accepted for tests.
        busy_timeout_ms: How long a blocked writer waits before giving up.
        journal_mode: One of :data:`VALID_JOURNAL_MODES`.
        synchronous: One of :data:`VALID_SYNCHRONOUS`.

    Raises:
        DatabaseError: if the file cannot be opened or a pragma is rejected.
    """
    target = ":memory:" if str(path) == ":memory:" else str(path)
    _validate_pragma("journal_mode", journal_mode, VALID_JOURNAL_MODES)
    _validate_pragma("synchronous", synchronous, VALID_SYNCHRONOUS)
    try:
        if target != ":memory:":
            Path(target).parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False: the store serialises access with its own
        # lock (see SqliteStore). Without this the scheduler thread cannot
        # write while the UI thread reads.
        connection = sqlite3.connect(target, check_same_thread=False, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute(f"PRAGMA busy_timeout={int(busy_timeout_ms)}")
        connection.execute(f"PRAGMA journal_mode={journal_mode}")
        connection.execute(f"PRAGMA synchronous={synchronous}")
        connection.execute("PRAGMA foreign_keys=ON")
    except (sqlite3.Error, OSError) as exc:
        raise DatabaseError(
            f"无法打开数据库：{target}",
            details={"path": target, "reason": str(exc)},
        ) from exc
    return connection


def _validate_pragma(name: str, value: str, allowed: frozenset[str]) -> None:
    """Reject a bad pragma value instead of letting SQLite ignore it.

    ``PRAGMA journal_mode=NOPE`` does not fail — it silently keeps the previous
    mode and returns it. A typo would then look like a working configuration
    right up until two writers collided, so the value is checked here.
    """
    if value not in allowed:
        raise DatabaseError(
            f"无效的 {name}：{value}",
            details={"allowed": sorted(allowed)},
        )
