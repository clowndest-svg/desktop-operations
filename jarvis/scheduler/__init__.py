"""Scheduler: time- and event-based task triggering.

Responsibility (delivered in phase 16):
    * Cron-like schedules, one-shot timers, event subscriptions; persists
      schedules and survives restarts.

Allowed dependencies: ``core``, ``config``, ``database``.

The public surface is small on purpose: the service, the two value types it
exchanges, and the migration constants the composition root applies.
"""

from jarvis.scheduler.service import SchedulerService
from jarvis.scheduler.store import MIGRATIONS, NAMESPACE, SchedulerRepository
from jarvis.scheduler.types import JobRun, JobSpec, TriggerKind

__all__ = [
    "MIGRATIONS",
    "NAMESPACE",
    "JobRun",
    "JobSpec",
    "SchedulerRepository",
    "SchedulerService",
    "TriggerKind",
]
