"""SQLite persistence for workflow execution history.

One table. Definitions live in YAML (the user's file is the source of truth),
so the database only has to remember *what happened*: which workflow ran, when,
whether it succeeded and what each step returned.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping, Sequence

from jarvis.database import (
    Migration,
    Repository,
    SqliteStore,
    SqlScalar,
    format_timestamp,
    utc_now,
)
from jarvis.workflow.types import StepResult, WorkflowRun

logger = logging.getLogger("jarvis.workflow.store")

NAMESPACE: str = "workflow"

MIGRATIONS: tuple[Migration, ...] = (
    Migration(
        namespace=NAMESPACE,
        version=1,
        statements=(
            """
            CREATE TABLE IF NOT EXISTS workflow_runs (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                workflow    TEXT    NOT NULL,
                started_at  TEXT    NOT NULL,
                finished_at TEXT    NOT NULL,
                ok          INTEGER NOT NULL,
                steps       TEXT    NOT NULL DEFAULT '[]',
                error       TEXT    NOT NULL DEFAULT ''
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_workflow_runs_name
                ON workflow_runs (workflow, id DESC)
            """,
        ),
    ),
)

_COLUMNS = "id, workflow, started_at, finished_at, ok, steps, error"


def _as_int(value: object) -> int:
    """Best-effort integer for a value read back from SQLite or JSON."""
    if isinstance(value, (int, float)):
        return int(value)
    return 0


def _decode_steps(raw: object) -> tuple[StepResult, ...]:
    """Rebuild step results from JSON, dropping entries that no longer parse.

    History is diagnostic: showing nine of ten steps beats showing a stack trace
    because the tenth row was written by an older schema.
    """
    if not isinstance(raw, str) or not raw:
        return ()
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("workflow run has unparsable steps payload")
        return ()
    if not isinstance(parsed, list):
        return ()
    results: list[StepResult] = []
    for item in parsed:
        if not isinstance(item, Mapping):
            continue
        results.append(
            StepResult(
                name=str(item.get("name", "")),
                skipped=bool(item.get("skipped", False)),
                ok=bool(item.get("ok", False)),
                output=str(item.get("output", "")),
                error=str(item.get("error", "")),
                elapsed_ms=_as_int(item.get("elapsed_ms", 0)),
            )
        )
    return tuple(results)


def _to_run(row: Mapping[str, object]) -> WorkflowRun:
    """Translate a row into a :class:`WorkflowRun`."""
    return WorkflowRun(
        run_id=_as_int(row["id"]),
        workflow=str(row["workflow"]),
        started_at=str(row["started_at"]),
        finished_at=str(row["finished_at"]),
        ok=bool(row["ok"]),
        steps=_decode_steps(row["steps"]),
        error=str(row["error"]),
    )


class WorkflowRepository(Repository):
    """Append-only history for workflow runs."""

    def __init__(self, store: SqliteStore) -> None:
        super().__init__(store)

    def record_run(
        self,
        *,
        workflow: str,
        started_at: str,
        finished_at: str,
        ok: bool,
        steps: Sequence[StepResult],
        error: str,
    ) -> int:
        """Append one run and return its row id."""
        payload = json.dumps([step.to_dict() for step in steps], ensure_ascii=False, sort_keys=True)
        return self.insert(
            """
            INSERT INTO workflow_runs
                (workflow, started_at, finished_at, ok, steps, error)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (workflow, started_at, finished_at, int(ok), payload, error),
        )

    def history(self, workflow: str | None = None, *, limit: int = 20) -> list[WorkflowRun]:
        """Most recent runs first, optionally scoped to one workflow."""
        if limit <= 0:
            return []
        sql = f"SELECT {_COLUMNS} FROM workflow_runs"
        params: list[SqlScalar] = []
        if workflow is not None:
            sql += " WHERE workflow = ?"
            params.append(workflow)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        return [_to_run(row) for row in self.query(sql, params)]

    def count_runs(self, *, ok: bool | None = None) -> int:
        """Total runs, or only the successful/failed ones."""
        if ok is None:
            value = self.scalar("SELECT COUNT(*) FROM workflow_runs")
        else:
            value = self.scalar("SELECT COUNT(*) FROM workflow_runs WHERE ok = ?", (int(ok),))
        return int(value) if isinstance(value, (int, float)) else 0

    def prune_runs(self, keep: int) -> int:
        """Keep only the newest ``keep`` runs; return how many rows were dropped.

        Same reasoning as the scheduler's copy: this table only ever grew, and a
        cron workflow running every few minutes makes it grow for as long as the app
        is left open. The newest rows are the ones the panel shows.
        """
        if keep <= 0:
            return 0
        return self.execute(
            """
            DELETE FROM workflow_runs
            WHERE id NOT IN (SELECT id FROM workflow_runs ORDER BY id DESC LIMIT ?)
            """,
            (keep,),
        )

    def last_run_at(self, workflow: str) -> str:
        """Timestamp of a workflow's most recent run, or ``""`` if never run."""
        value = self.scalar(
            "SELECT finished_at FROM workflow_runs WHERE workflow = ? ORDER BY id DESC LIMIT 1",
            (workflow,),
        )
        return str(value) if value is not None else ""

    def now(self) -> str:
        """Canonical "now" timestamp (kept here so callers share one clock)."""
        return format_timestamp(utc_now())


__all__ = ["MIGRATIONS", "NAMESPACE", "WorkflowRepository"]
