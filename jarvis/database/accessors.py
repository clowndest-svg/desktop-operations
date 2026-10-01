"""Typed accessors for values that came out of a database.

A row from SQLite is a mapping whose values are ``int | float | str | bytes |
None`` — the column's declared type is not visible to the type checker, and
``MAX(x)`` over an empty group really is ``NULL``. Writing ``int(row["count"])``
therefore fails under ``mypy --strict``, and the usual workarounds are all bad:
a ``# type: ignore`` at every call site hides real mistakes too, and a bare
``cast`` is a promise the database has not made.

These four functions are the honest version: they say what they do with a NULL
or an unexpected type, in one place, and the caller stops caring.
"""

from __future__ import annotations

__all__ = ["as_bool", "as_float", "as_int", "as_str"]


def as_int(value: object, default: int = 0) -> int:
    """Coerce a column value to ``int``.

    ``bool`` is accepted and converted (SQLite stores booleans as 0/1, and
    ``isinstance(True, int)`` is true anyway). A NULL or an unconvertible value
    yields ``default`` rather than raising: a missing ``MAX()`` is a normal
    outcome, not a corrupt database.
    """
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str | bytes):
        try:
            return int(value)
        except ValueError:
            return default
    return default


def as_float(value: object, default: float = 0.0) -> float:
    """Coerce a column value to ``float``, with the same tolerance as
    :func:`as_int`."""
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str | bytes):
        try:
            return float(value)
        except ValueError:
            return default
    return default


def as_str(value: object, default: str = "") -> str:
    """Coerce a column value to ``str``.

    ``bytes`` is decoded as UTF-8 with replacement rather than refused: a BLOB
    that reached a text column is a bug somewhere else, and turning it into
    mojibake is more debuggable than a crash in a log line.
    """
    if isinstance(value, str):
        return value
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if value is None:
        return default
    return str(value)


def as_bool(value: object, default: bool = False) -> bool:
    """Coerce a column value to ``bool``.

    Only ``0``/``1``, the strings ``"0"``/``"1"``/``"true"``/``"false"`` and
    Python truthiness for the rest — SQLite has no boolean type, so a value
    written as ``int(pinned)`` must read back as a boolean.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, int | float):
        return value != 0
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes"}:
            return True
        if lowered in {"0", "false", "no", ""}:
            return False
        return default
    if value is None:
        return default
    return bool(value)
