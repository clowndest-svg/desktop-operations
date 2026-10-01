"""L1 persistence: one SQLite file, modular migrations, shared connection.

Responsibility (delivered from phase 11 onwards):
    * Connection management, schema migrations, repository classes and the
      SQLite-backed cache. No raw SQL outside this package.

Public surface only — the composition root and the capability packages that
persist things (``memory``, ``knowledge``, ``scheduler``, ``workflow``) import
from here, never from the submodules.

Allowed dependencies: ``core``, ``config``.
"""

from jarvis.database.accessors import as_bool, as_float, as_int, as_str
from jarvis.database.connection import (
    VALID_JOURNAL_MODES,
    VALID_SYNCHRONOUS,
    open_connection,
)
from jarvis.database.migrations import Migration, MigrationRunner, validate_migrations
from jarvis.database.repository import Repository
from jarvis.database.store import DEFAULT_DB_FILENAME, SqliteStore
from jarvis.database.types import (
    Row,
    SqlParams,
    SqlScalar,
    format_timestamp,
    parse_timestamp,
    utc_now,
)

__all__ = [
    "DEFAULT_DB_FILENAME",
    "VALID_JOURNAL_MODES",
    "VALID_SYNCHRONOUS",
    "Migration",
    "MigrationRunner",
    "Repository",
    "Row",
    "SqlParams",
    "SqlScalar",
    "SqliteStore",
    "as_bool",
    "as_float",
    "as_int",
    "as_str",
    "format_timestamp",
    "open_connection",
    "parse_timestamp",
    "utc_now",
    "validate_migrations",
]
