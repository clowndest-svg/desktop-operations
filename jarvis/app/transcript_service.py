"""Conversations that survive the window closing.

Until this module existed, a conversation lived in one list inside
:class:`~jarvis.app.chat_service.ChatService`: 清空 emptied it and quitting the app
took it with them, which is exactly the two complaints this file answers.

Two tables, deliberately boring:

* ``chat_sessions`` -- one row per conversation, with the title the first question
  gave it;
* ``chat_messages`` -- the turns, in order, with the wall-clock time they happened.

Nothing clever about retention: sessions live until a person deletes one. A
desktop assistant's history is small (text), and "where did last Tuesday's answer
go" is a worse failure than a few megabytes of SQLite.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import uuid
from typing import Any

from jarvis.database import Migration, SqliteStore, format_timestamp, utc_now

logger = logging.getLogger("jarvis.app.transcript_service")

NAMESPACE = "chat"

TITLE_CHARS = 24
"""How much of the first question becomes the session title. Enough to recognise,
short enough to fit the strip the HUD draws."""

MESSAGES_KEPT_IN_MEMORY = 10
"""How many past exchanges a switched-to session replays to the model. Same number
the in-memory chat always used; the rest stay on disk and are still readable."""

MIGRATIONS: tuple[Migration, ...] = (
    Migration(
        namespace=NAMESPACE,
        version=1,
        statements=(
            """
            CREATE TABLE IF NOT EXISTS chat_sessions (
                id         TEXT PRIMARY KEY,
                title      TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS chat_messages (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL REFERENCES chat_sessions(id) ON DELETE CASCADE,
                role       TEXT NOT NULL,
                content    TEXT NOT NULL,
                at         TEXT NOT NULL
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_chat_messages_session
                ON chat_messages (session_id, id)
            """,
        ),
    ),
    Migration(
        namespace=NAMESPACE,
        version=2,
        statements=(
            # Attachments ride along as JSON: a name, a kind, a size, and for images
            # a downscaled thumbnail the page already shrank to a few tens of KB.
            # A column rather than a table because an attachment has no meaning
            # apart from the turn it was handed in with.
            """
            ALTER TABLE chat_messages ADD COLUMN attachments TEXT NOT NULL DEFAULT '[]'
            """,
        ),
    ),
)


def _turn(row: sqlite3.Row) -> dict[str, Any]:
    """One stored turn, with its attachments parsed back out.

    A row written before the attachments column existed, or a row whose JSON is
    damaged, yields an empty list rather than an exception: a transcript that
    refuses to open because of one bad cell would throw away everything else in
    the conversation.
    """
    try:
        raw = row["attachments"]
    except (IndexError, KeyError):
        # A row read through a connection opened before migration 2 ran.
        raw = "[]"
    try:
        attachments = json.loads(raw or "[]")
    except ValueError:
        logger.warning("turn %s carries unparsable attachments; showing none", row["id"])
        attachments = []
    if not isinstance(attachments, list):
        attachments = []
    return {
        "role": row["role"],
        "content": row["content"],
        "at": row["at"],
        "attachments": attachments,
    }


class TranscriptService:
    """Reads and writes conversations. Never raises into the caller."""

    name = "transcript"

    def __init__(self, store: SqliteStore) -> None:
        self._store = store
        self._started = False

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        if self._started:
            return
        self._store.migrate(NAMESPACE, MIGRATIONS)
        self._started = True
        logger.info("transcript store ready")

    def stop(self) -> None:
        self._started = False

    # -- sessions ----------------------------------------------------------

    def new_session(self, title: str = "") -> str:
        session_id = uuid.uuid4().hex
        stamp = format_timestamp(utc_now())
        try:
            with self._store.transaction() as conn:
                conn.execute(
                    "INSERT INTO chat_sessions (id, title, created_at, updated_at)"
                    " VALUES (?, ?, ?, ?)",
                    (session_id, title, stamp, stamp),
                )
        except sqlite3.Error:
            logger.exception("could not open a conversation")
            return ""
        return session_id

    def list_sessions(self, limit: int = 50) -> list[dict[str, Any]]:
        try:
            with self._store.connection() as conn:
                rows = conn.execute(
                    """
                    SELECT s.id, s.title, s.updated_at, COUNT(m.id) AS turns
                      FROM chat_sessions s
                      LEFT JOIN chat_messages m ON m.session_id = s.id
                     GROUP BY s.id, s.title, s.updated_at
                     ORDER BY s.updated_at DESC
                     LIMIT ?
                    """,
                    (max(1, limit),),
                ).fetchall()
        except sqlite3.Error:
            logger.exception("could not list conversations")
            return []
        return [
            {
                "id": row["id"],
                "title": row["title"] or "（没有标题）",
                "updated_at": row["updated_at"],
                "turns": int(row["turns"] or 0) // 2,
            }
            for row in rows
        ]

    def rename(self, session_id: str, title: str) -> bool:
        try:
            with self._store.transaction() as conn:
                cursor = conn.execute(
                    "UPDATE chat_sessions SET title = ?, updated_at = updated_at WHERE id = ?",
                    (title.strip()[: TITLE_CHARS * 2], session_id),
                )
                return cursor.rowcount > 0
        except sqlite3.Error:
            logger.exception("could not rename conversation %s", session_id)
            return False

    def delete(self, session_id: str) -> bool:
        try:
            with self._store.transaction() as conn:
                cursor = conn.execute("DELETE FROM chat_sessions WHERE id = ?", (session_id,))
                return cursor.rowcount > 0
        except sqlite3.Error:
            logger.exception("could not delete conversation %s", session_id)
            return False

    # -- turns -------------------------------------------------------------

    def append(self, session_id: str, role: str, content: str, attachments: str = "[]") -> bool:
        """One turn. Also touches the session's ``updated_at`` so the list re-sorts.

        ``attachments`` arrives as already-serialised JSON from the caller, which is
        the side that knows what an attachment is; this layer only promises to give
        it back exactly as it came.
        """
        if not session_id or not content.strip():
            return False
        stamp = format_timestamp(utc_now())
        try:
            with self._store.transaction() as conn:
                conn.execute(
                    "INSERT INTO chat_messages (session_id, role, content, at, attachments)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (session_id, role, content, stamp, attachments),
                )
                conn.execute(
                    "UPDATE chat_sessions SET updated_at = ? WHERE id = ?", (stamp, session_id)
                )
                # A session that never got a title takes the first question's.
                conn.execute(
                    "UPDATE chat_sessions SET title = ? WHERE id = ? AND title = ''",
                    (content.strip().replace("\n", " ")[:TITLE_CHARS], session_id),
                )
        except sqlite3.Error:
            logger.exception("could not record a turn in %s", session_id)
            return False
        return True

    def messages(
        self, session_id: str, limit: int = MESSAGES_KEPT_IN_MEMORY * 2
    ) -> list[dict[str, Any]]:
        """The last ``limit`` turns, oldest first, for replaying into the model."""
        try:
            with self._store.connection() as conn:
                rows = conn.execute(
                    """
                    SELECT role, content, at, attachments FROM (
                        SELECT id, role, content, at, attachments
                          FROM chat_messages
                         WHERE session_id = ?
                         ORDER BY id DESC
                         LIMIT ?
                    ) ORDER BY id
                    """,
                    (session_id, max(0, limit)),
                ).fetchall()
        except sqlite3.Error:
            logger.exception("could not read conversation %s", session_id)
            return []
        return [_turn(row) for row in rows]

    def whole_session(self, session_id: str, limit: int = 200) -> list[dict[str, Any]]:
        """Every turn the HUD wants to draw, oldest first, capped for the page."""
        try:
            with self._store.connection() as conn:
                rows = conn.execute(
                    """
                    SELECT role, content, at, attachments FROM (
                        SELECT id, role, content, at, attachments
                          FROM chat_messages
                         WHERE session_id = ?
                         ORDER BY id DESC
                         LIMIT ?
                    ) ORDER BY id
                    """,
                    (session_id, max(1, limit)),
                ).fetchall()
        except sqlite3.Error:
            logger.exception("could not read conversation %s", session_id)
            return []
        return [_turn(row) for row in rows]


__all__ = ["MESSAGES_KEPT_IN_MEMORY", "MIGRATIONS", "NAMESPACE", "TranscriptService"]
