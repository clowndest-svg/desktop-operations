"""Domain types for the scheduler.

Kept separate from the service so the workflow engine (and the UI bridge) can
name a job without importing APScheduler. ``JobSpec`` is a value object: the
scheduler stores it, hands it to the action runner and never mutates it, which
is why it is frozen.
"""

from __future__ import annotations

import enum
from collections.abc import Mapping
from dataclasses import dataclass


class TriggerKind(enum.StrEnum):
    """How a job decides when to fire.

    ``CRON`` covers "every day at nine", ``INTERVAL`` covers "every N seconds" and
    ``DATE`` is one wall-clock moment -- a reminder.

    ``DATE`` was left out on purpose at first, on the theory that one-shot timers
    belong to the layers above. The reminder feature proved that backwards: a
    one-shot still needs the two things a hand-rolled ``threading.Timer`` gets wrong
    and this service already solves -- the definition is a row that survives a
    restart, and a fire time that passed while the machine was off is handled by a
    misfire policy instead of vanishing. A timer that dies with the process is not a
    reminder; it is a promise with a bug in it.
    """

    CRON = "cron"
    INTERVAL = "interval"
    DATE = "date"


@dataclass(frozen=True, slots=True)
class JobSpec:
    """A persisted, re-runnable description of a scheduled task.

    ``action`` and ``arguments`` are deliberately opaque strings/mappings: the
    scheduler does not know what a tool or workflow is, it only knows how to
    hand the pair to the injected ``action_runner``. That keeps this package
    free of a dependency on ``jarvis.tools``.
    """

    job_id: str
    """Stable identifier chosen by the caller (``workflow:每日早报``)."""

    name: str
    """Human-readable label shown in the UI."""

    action: str
    """Action name passed to the ``action_runner`` callback."""

    arguments: Mapping[str, object]
    """Arguments passed to the ``action_runner`` callback."""

    trigger: TriggerKind
    """Whether ``expression`` is a cron string or an interval in seconds."""

    expression: str
    """Cron expression (5 fields) or a positive integer number of seconds."""

    enabled: bool = True
    """Disabled jobs stay in the database but are not registered."""

    created_at: str = ""
    """Canonical timestamp; filled in by the service when empty."""

    def to_dict(self) -> dict[str, object]:
        """JSON-ready view for the UI bridge (never exposes live objects)."""
        return {
            "job_id": self.job_id,
            "name": self.name,
            "action": self.action,
            "arguments": dict(self.arguments),
            "trigger": self.trigger.value,
            "expression": self.expression,
            "enabled": self.enabled,
            "created_at": self.created_at,
        }


@dataclass(frozen=True, slots=True)
class JobRun:
    """One execution record, success or failure.

    Failures are first-class rows rather than log lines because the question a
    user actually asks is "did my 9am job run, and if not why" — an empty history
    and a stack trace in a file do not answer it.
    """

    run_id: int
    """Row id in ``scheduler_runs`` (monotonic, newest first when listed)."""

    job_id: str
    """The job this run belongs to."""

    started_at: str
    """Canonical timestamp of when the action began."""

    finished_at: str
    """Canonical timestamp of when the action returned or raised."""

    ok: bool
    """Whether the action runner completed without raising."""

    detail: str
    """Result text returned by the action runner (empty on failure)."""

    error: str = ""
    """``TypeName: message`` of the raised exception, empty on success."""

    def to_dict(self) -> dict[str, object]:
        """JSON-ready view for the UI bridge."""
        return {
            "run_id": self.run_id,
            "job_id": self.job_id,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "ok": self.ok,
            "detail": self.detail,
            "error": self.error,
        }


__all__ = ["JobRun", "JobSpec", "TriggerKind"]
