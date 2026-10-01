"""SQLite persistence for scheduled jobs and their execution history.

Two tables, because they have different lifetimes and growth rates:

``scheduler_jobs``
    One row per job. Read back on every ``start()`` so a restart does not lose
    the 09:00 briefing — APScheduler's in-memory job store cannot do that, and
    ``SQLAlchemyJobStore`` would drag in SQLAlchemy for one table.

``scheduler_runs``
    Append-only history. Kept separate from the job row so a hot job (every
    second) cannot bloat the definition it is supposed to be running.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping

from jarvis.database import (
    Migration,
    Repository,
    SqliteStore,
    SqlScalar,
    format_timestamp,
    utc_now,
)
from jarvis.scheduler.types import JobRun, JobSpec, TriggerKind

logger = logging.getLogger("jarvis.scheduler.store")

NAMESPACE: str = "scheduler"

MIGRATIONS: tuple[Migration, ...] = (
    Migration(
        namespace=NAMESPACE,
        version=1,
        statements=(
            """
            CREATE TABLE IF NOT EXISTS scheduler_jobs (
                job_id      TEXT    PRIMARY KEY,
                name        TEXT    NOT NULL,
                action      TEXT    NOT NULL,
                arguments   TEXT    NOT NULL DEFAULT '{}',
                trigger     TEXT    NOT NULL,
                expression  TEXT    NOT NULL,
                enabled     INTEGER NOT NULL DEFAULT 1,
                created_at  TEXT    NOT NULL,
                updated_at  TEXT    NOT NULL
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_scheduler_jobs_enabled
                ON scheduler_jobs (enabled)
            """,
            """
            CREATE TABLE IF NOT EXISTS scheduler_runs (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id      TEXT    NOT NULL,
                started_at  TEXT    NOT NULL,
                finished_at TEXT    NOT NULL,
                ok          INTEGER NOT NULL,
                detail      TEXT    NOT NULL DEFAULT '',
                error       TEXT    NOT NULL DEFAULT ''
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_scheduler_runs_job
                ON scheduler_runs (job_id, id DESC)
            """,
        ),
    ),
)

_COLUMNS = "job_id, name, action, arguments, trigger, expression, enabled, created_at"


def _decode_arguments(raw: object) -> dict[str, object]:
    """Parse the stored JSON back into a mapping, tolerating hand-edited rows.

    A corrupt ``arguments`` blob must not stop the job list from rendering: the
    job degrades to "no arguments" and the anomaly is logged instead of raised.
    """
    if not isinstance(raw, str) or not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("scheduler job has unparsable arguments %r; using {}", raw[:80])
        return {}
    if not isinstance(parsed, dict):
        return {}
    return {str(key): value for key, value in parsed.items()}


def _to_spec(row: Mapping[str, object]) -> JobSpec:
    """Translate a row into a :class:`JobSpec`, defaulting unknown triggers."""
    raw_trigger = str(row["trigger"])
    try:
        trigger = TriggerKind(raw_trigger)
    except ValueError:
        logger.warning("unknown trigger kind %r; treating as interval", raw_trigger)
        trigger = TriggerKind.INTERVAL
    return JobSpec(
        job_id=str(row["job_id"]),
        name=str(row["name"]),
        action=str(row["action"]),
        arguments=_decode_arguments(row["arguments"]),
        trigger=trigger,
        expression=str(row["expression"]),
        enabled=bool(row["enabled"]),
        created_at=str(row["created_at"]),
    )


class SchedulerRepository(Repository):
    """CRUD for job definitions and append-only run history."""

    def __init__(self, store: SqliteStore) -> None:
        super().__init__(store)

    # -- jobs --------------------------------------------------------------

    def upsert_job(self, spec: JobSpec) -> JobSpec:
        """Insert or replace a job definition, returning what was stored.

        Replace rather than merge: a caller handing over a whole ``JobSpec``
        means it, and a half-updated row (new cron, old name) is worse than a
        clean overwrite.
        """
        now = format_timestamp(utc_now())
        created_at = spec.created_at or now
        self.execute(
            """
            INSERT INTO scheduler_jobs
                (job_id, name, action, arguments, trigger, expression, enabled,
                 created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (job_id) DO UPDATE SET
                name       = excluded.name,
                action     = excluded.action,
                arguments  = excluded.arguments,
                trigger    = excluded.trigger,
                expression = excluded.expression,
                enabled    = excluded.enabled,
                updated_at = excluded.updated_at
            """,
            (
                spec.job_id,
                spec.name,
                spec.action,
                json.dumps(dict(spec.arguments), ensure_ascii=False, sort_keys=True),
                spec.trigger.value,
                spec.expression,
                int(spec.enabled),
                created_at,
                now,
            ),
        )
        return JobSpec(
            job_id=spec.job_id,
            name=spec.name,
            action=spec.action,
            arguments=dict(spec.arguments),
            trigger=spec.trigger,
            expression=spec.expression,
            enabled=spec.enabled,
            created_at=created_at,
        )

    def get_job(self, job_id: str) -> JobSpec | None:
        """Fetch one job by id."""
        row = self.query_one(f"SELECT {_COLUMNS} FROM scheduler_jobs WHERE job_id = ?", (job_id,))
        return _to_spec(row) if row else None

    def list_jobs(self, *, enabled_only: bool = False) -> list[JobSpec]:
        """Every job, oldest first; optionally only the enabled ones."""
        sql = f"SELECT {_COLUMNS} FROM scheduler_jobs"
        if enabled_only:
            sql += " WHERE enabled = 1"
        sql += " ORDER BY created_at ASC, job_id ASC"
        return [_to_spec(row) for row in self.query(sql)]

    def delete_job(self, job_id: str) -> bool:
        """Delete a job definition. Returns whether a row disappeared."""
        return self.execute("DELETE FROM scheduler_jobs WHERE job_id = ?", (job_id,)) > 0

    def set_enabled(self, job_id: str, enabled: bool) -> bool:
        """Flip a job's enabled flag. Returns whether the job existed."""
        return (
            self.execute(
                "UPDATE scheduler_jobs SET enabled = ?, updated_at = ? WHERE job_id = ?",
                (int(enabled), format_timestamp(utc_now()), job_id),
            )
            > 0
        )

    # -- runs --------------------------------------------------------------

    def record_run(
        self,
        *,
        job_id: str,
        started_at: str,
        finished_at: str,
        ok: bool,
        detail: str,
        error: str,
    ) -> int:
        """Append one execution record and return its row id."""
        return self.insert(
            """
            INSERT INTO scheduler_runs
                (job_id, started_at, finished_at, ok, detail, error)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (job_id, started_at, finished_at, int(ok), detail, error),
        )

    def history(self, job_id: str | None = None, *, limit: int = 20) -> list[JobRun]:
        """Most recent runs first, optionally scoped to one job."""
        if limit <= 0:
            return []
        sql = "SELECT id, job_id, started_at, finished_at, ok, detail, error " "FROM scheduler_runs"
        params: list[SqlScalar] = []
        if job_id is not None:
            sql += " WHERE job_id = ?"
            params.append(job_id)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        return [
            JobRun(
                run_id=int(row["id"]),  # type: ignore[arg-type]
                job_id=str(row["job_id"]),
                started_at=str(row["started_at"]),
                finished_at=str(row["finished_at"]),
                ok=bool(row["ok"]),
                detail=str(row["detail"]),
                error=str(row["error"]),
            )
            for row in self.query(sql, params)
        ]

    def count_runs(self, *, ok: bool | None = None) -> int:
        """Total runs, or only the successful/failed ones."""
        if ok is None:
            value = self.scalar("SELECT COUNT(*) FROM scheduler_runs")
        else:
            value = self.scalar("SELECT COUNT(*) FROM scheduler_runs WHERE ok = ?", (int(ok),))
        return int(value) if isinstance(value, (int, float)) else 0


__all__ = ["MIGRATIONS", "NAMESPACE", "SchedulerRepository"]
