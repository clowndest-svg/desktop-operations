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

from jarvis.core.exceptions import DatabaseError
from jarvis.database import Migration, SqliteStore, format_timestamp, utc_now

logger = logging.getLogger("jarvis.app.transcript_service")

NAMESPACE = "chat"

TITLE_CHARS = 24
"""How much of the first question becomes the session title. Enough to recognise,
short enough to fit the strip the HUD draws."""

MESSAGES_KEPT_IN_MEMORY = 10
"""How many past exchanges a switched-to session replays to the model. Same number
the in-memory chat always used; the rest stay on disk and are still readable."""

MAX_SEARCH_RESULTS = 20
"""Ceiling on :meth:`TranscriptService.search`. A tool result goes into a prompt, and
an unbounded substring match over somebody's year of conversation would fill it."""

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
    Migration(
        namespace=NAMESPACE,
        version=3,
        statements=(
            # Which model answered, and which turn of the task table it belongs to.
            # Both exist because the panel now holds several conversations and a round
            # table puts several models inside one of them: 「她说了什么」 no longer says
            # *who* said it, and a cost that cannot be attributed to the task that caused
            # it is a bill nobody can explain afterwards.
            """
            ALTER TABLE chat_messages ADD COLUMN model TEXT NOT NULL DEFAULT ''
            """,
            """
            ALTER TABLE chat_messages ADD COLUMN task_id TEXT NOT NULL DEFAULT ''
            """,
        ),
    ),
    Migration(
        namespace=NAMESPACE,
        version=4,
        statements=(
            "ALTER TABLE chat_sessions ADD COLUMN pinned INTEGER NOT NULL DEFAULT 0",
            "CREATE INDEX idx_chat_sessions_listing "
            "ON chat_sessions (pinned DESC, updated_at DESC, id DESC)",
        ),
    ),
)


def _text(row: sqlite3.Row, key: str) -> str:
    """A column that may not exist yet, read without an exception path per row."""
    try:
        return str(row[key] or "")
    except (IndexError, KeyError):
        return ""


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
        "id": int(row["id"]),
        "role": row["role"],
        "content": row["content"],
        "at": row["at"],
        "attachments": attachments,
        # Read defensively like the attachments above: a connection opened before
        # migration 3 ran has no such column, and a transcript that refused to open
        # over that would throw away every turn in the conversation for a label.
        "model": _text(row, "model"),
        "task_id": _text(row, "task_id"),
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

    def fork_before(self, session_id: str, message_id: int, title: str) -> str:
        """Copy the prefix before a user question atomically; retain the original."""
        if not self._started:
            return ""
        key = uuid.uuid4().hex
        stamp = format_timestamp(utc_now())
        try:
            with self._store.transaction() as conn:
                target = conn.execute(
                    "SELECT role FROM chat_messages WHERE session_id=? AND id=?",
                    (session_id, message_id),
                ).fetchone()
                if target is None or target["role"] != "user":
                    return ""
                conn.execute(
                    "INSERT INTO chat_sessions(id,title,created_at,updated_at) VALUES(?,?,?,?)",
                    (key, title[:TITLE_CHARS], stamp, stamp),
                )
                conn.execute(
                    "INSERT INTO chat_messages"
                    "(session_id,role,content,at,attachments,model,task_id) "
                    "SELECT ?,role,content,at,attachments,model,'' FROM chat_messages "
                    "WHERE session_id=? AND id<? ORDER BY id",
                    (key, session_id, message_id),
                )
            return key
        except sqlite3.Error:
            logger.exception("could not branch a question")
            return ""

    def new_session(self, title: str = "", session_id: str = "") -> str:
        """Open a conversation row. Returns its id, or ``""`` when nothing was created.

        ``session_id`` is accepted because the caller may already have one: a tab opened
        in the panel gets an id before it has any turns, and that id has to survive being
        stored -- a conversation whose id changes halfway through leaves the running turn
        writing into a row the tab is not showing.
        """
        session_id = str(session_id or "") or uuid.uuid4().hex
        stamp = format_timestamp(utc_now())
        try:
            with self._store.transaction() as conn:
                conn.execute(
                    "INSERT OR IGNORE INTO chat_sessions (id, title, created_at, updated_at)"
                    " VALUES (?, ?, ?, ?)",
                    (session_id, title, stamp, stamp),
                )
        except sqlite3.Error:
            logger.exception("could not open a conversation")
            return ""
        return session_id

    def list_sessions(self, limit: int = 50) -> list[dict[str, Any]]:
        return list(self.session_page(limit=limit)["sessions"])

    def session_details(self, session_ids: list[str]) -> dict[str, dict[str, Any]]:
        """Snapshots need only the open tabs, even when those tabs are years old."""
        result: dict[str, dict[str, Any]] = {}
        try:
            with self._store.connection() as conn:
                for start in range(0, len(session_ids), 200):
                    keys = session_ids[start : start + 200]
                    placeholders = ",".join("?" for _ in keys)
                    rows = conn.execute(
                        "SELECT s.id, s.title, s.updated_at, "
                        "(SELECT COUNT(*) FROM chat_messages m WHERE m.session_id=s.id) AS total "
                        f"FROM chat_sessions s WHERE s.id IN ({placeholders})",
                        keys,
                    )
                    for row in rows:
                        result[row["id"]] = {
                            "title": row["title"],
                            "updated_at": row["updated_at"],
                            "turns": row["total"] // 2,
                        }
        except (sqlite3.Error, DatabaseError):
            logger.exception("could not read open conversation details")
        return result

    def session_page(
        self, query: str = "", offset: int = 0, limit: int = 50, pinned_only: bool = False
    ) -> dict[str, Any]:
        """Page sessions before counting turns; search all history, including old sessions.

        Literal substring matching keeps %, _ and quotes ordinary user text. The
        covering message index counts only this page rather than grouping all turns.
        One extra session tells the page whether there is more, without a full count.
        """
        term = str(query).strip()[:200]
        limit, offset = max(1, min(int(limit), 200)), max(0, int(offset))
        empty = {"sessions": [], "has_more": False, "next_offset": offset, "error": ""}
        try:
            with self._store.connection() as conn:
                rows = conn.execute(
                    """
                    SELECT id, title, updated_at, pinned FROM chat_sessions s
                    WHERE (? = 0 OR pinned = 1) AND (
                        ? = '' OR instr(lower(title), lower(?)) > 0 OR EXISTS (
                            SELECT 1 FROM chat_messages m WHERE m.session_id = s.id
                            AND instr(lower(m.content), lower(?)) > 0
                        )
                    )
                    ORDER BY pinned DESC, updated_at DESC, id DESC LIMIT ? OFFSET ?
                    """,
                    (int(pinned_only), term, term, term, limit + 1, offset),
                ).fetchall()
                sessions = []
                for row in rows[:limit]:
                    count = conn.execute(
                        "SELECT COUNT(*) FROM chat_messages WHERE session_id = ?", (row["id"],)
                    ).fetchone()[0]
                    excerpt = ""
                    if term:
                        match = conn.execute(
                            "SELECT substr(content, max(1, instr(lower(content), lower(?)) - 45), "
                            "160) FROM chat_messages WHERE session_id = ? "
                            "AND instr(lower(content), lower(?)) > 0 ORDER BY id DESC LIMIT 1",
                            (term, row["id"], term),
                        ).fetchone()
                        excerpt = match[0] if match else ""
                    sessions.append(
                        {
                            "id": row["id"],
                            "title": row["title"] or "（没有标题）",
                            "updated_at": row["updated_at"],
                            "pinned": bool(row["pinned"]),
                            "turns": int(count) // 2,
                            "excerpt": excerpt,
                        }
                    )
        except (sqlite3.Error, DatabaseError):
            logger.exception("could not list conversations")
            return {**empty, "error": "读取对话历史失败，请稍后重试"}
        return {
            "sessions": sessions,
            "has_more": len(rows) > limit,
            "next_offset": offset + len(sessions),
            "error": "",
        }

    def pin(self, session_id: str, pinned: bool) -> bool:
        """Pinning changes list priority, never the time the conversation happened."""
        try:
            with self._store.transaction() as conn:
                return (
                    conn.execute(
                        "UPDATE chat_sessions SET pinned = ? WHERE id = ?",
                        (int(pinned), session_id),
                    ).rowcount
                    > 0
                )
        except (sqlite3.Error, DatabaseError):
            logger.exception("could not pin conversation %s", session_id)
            return False

    def export_snapshot(self, session_id: str) -> dict[str, Any]:
        """Read the title and ALL messages together, without the display/replay cap."""
        try:
            with self._store.connection() as conn:
                row = conn.execute(
                    "SELECT title FROM chat_sessions WHERE id = ?", (session_id,)
                ).fetchone()
                if row is None:
                    return {"error": "没有这个对话", "messages": []}
                turns = conn.execute(
                    "SELECT id, role, content, at, attachments, model, task_id "
                    "FROM chat_messages WHERE session_id = ? ORDER BY id",
                    (session_id,),
                ).fetchall()
                return {"error": "", "title": row["title"], "messages": [_turn(t) for t in turns]}
        except (sqlite3.Error, DatabaseError):
            logger.exception("could not export conversation %s", session_id)
            return {"error": "读取对话失败，未生成导出文件", "messages": []}

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

    def append(
        self,
        session_id: str,
        role: str,
        content: str,
        attachments: str = "[]",
        *,
        model: str = "",
        task_id: str = "",
    ) -> bool:
        """One turn. Also touches the session's ``updated_at`` so the list re-sorts.

        ``attachments`` arrives as already-serialised JSON from the caller, which is
        the side that knows what an attachment is; this layer only promises to give
        it back exactly as it came.

        ``model`` is recorded per turn rather than per session because a round-table
        conversation has several models answering inside one session, and "which model
        said this" is the question the panel is asked to answer.
        """
        if not session_id or not content.strip():
            return False
        stamp = format_timestamp(utc_now())
        try:
            with self._store.transaction() as conn:
                conn.execute(
                    "INSERT INTO chat_messages"
                    " (session_id, role, content, at, attachments, model, task_id)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (session_id, role, content, stamp, attachments, model, task_id),
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
                    SELECT id, role, content, at, attachments, model, task_id FROM (
                        SELECT id, role, content, at, attachments, model, task_id
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

    def search(self, query: str, *, limit: int = 8) -> list[dict[str, Any]]:
        """Find turns across every stored conversation that contain ``query``.

        Substring matching, not ``LIKE``: the term is a plain parameter to ``instr``,
        so a ``%`` or an underscore in what the user typed stays what it is instead of
        turning into a wildcard. Sessions are deleted only by a person, so "where did
        we talk about that" has an answer for as long as the history exists.
        """
        term = query.strip()
        if not term or limit <= 0:
            return []
        try:
            with self._store.connection() as conn:
                rows = conn.execute(
                    """
                    SELECT m.role, m.content, m.at, m.session_id,
                           s.title, substr(m.content, 1, 400) AS excerpt
                      FROM chat_messages AS m
                      LEFT JOIN chat_sessions AS s ON s.id = m.session_id
                     WHERE instr(m.content, ?) > 0
                     ORDER BY m.id DESC
                     LIMIT ?
                    """,
                    (term, min(int(limit), MAX_SEARCH_RESULTS)),
                ).fetchall()
        except sqlite3.Error:
            logger.exception("could not search the conversation history")
            return []
        return [
            {
                "role": row["role"],
                "at": row["at"],
                "session_id": row["session_id"],
                "title": row["title"] or "",
                "excerpt": row["excerpt"],
            }
            for row in rows
        ]

    def whole_session(self, session_id: str, limit: int = 200) -> list[dict[str, Any]]:
        """Every turn the HUD wants to draw, oldest first, capped for the page."""
        try:
            with self._store.connection() as conn:
                rows = conn.execute(
                    """
                    SELECT id, role, content, at, attachments, model, task_id FROM (
                        SELECT id, role, content, at, attachments, model, task_id
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
