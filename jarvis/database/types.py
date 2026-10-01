"""Shared type vocabulary for the persistence layer.

Kept deliberately small: SQLite only understands a handful of Python types, and
naming them here stops every repository from re-inventing its own alias (and
from quietly passing a ``datetime`` straight into a driver that will store it as
whatever ``str()`` happens to produce).
"""

from __future__ import annotations

import datetime
from collections.abc import Mapping, Sequence
from typing import Final

type SqlScalar = int | float | str | bytes | None
"""Everything SQLite can store natively."""

type SqlParams = Sequence[SqlScalar] | Mapping[str, SqlScalar]
"""Bound parameters, positional (``?``) or named (``:name``)."""

type Row = Mapping[str, SqlScalar]
"""One result row, keyed by column name."""

TIMESTAMP_FORMAT: Final[str] = "%Y-%m-%dT%H:%M:%S.%f%z"
"""Canonical on-disk timestamp format (ISO 8601, sortable as text).

Stored as text rather than a Unix float on purpose: a database file is
something an operator may open by hand when something went wrong, and
``2026-09-30T10:20:12`` is readable while ``1785000000.0`` is not.
"""


def format_timestamp(value: datetime.datetime) -> str:
    """Render a datetime for storage, normalising to UTC."""
    if value.tzinfo is None:
        value = value.astimezone()
    return value.astimezone(datetime.UTC).strftime(TIMESTAMP_FORMAT)


def parse_timestamp(text: str) -> datetime.datetime:
    """Parse a stored timestamp back into an aware datetime.

    Raises:
        ValueError: if ``text`` is not in :data:`TIMESTAMP_FORMAT`.
    """
    return datetime.datetime.strptime(text, TIMESTAMP_FORMAT)


def utc_now() -> datetime.datetime:
    """Current time as an aware UTC datetime (single source of "now")."""
    return datetime.datetime.now(datetime.UTC)
