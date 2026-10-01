"""The scheduler service: cron/interval jobs on top of APScheduler.

Why APScheduler and not a hand-rolled ``threading.Timer`` loop: cron parsing,
timezone handling and misfire policy are exactly the fiddly parts a scheduler
gets wrong, and they are already solved here. What APScheduler does *not* give
us is durability — its in-memory store forgets everything on exit — so job
definitions live in SQLite and are re-registered on every ``start()``.

The service owns no knowledge of what a job *does*. ``action_runner`` is
injected, so this package can be tested (and layered) without importing
``jarvis.tools``.
"""

from __future__ import annotations

import datetime
import logging
import time
from collections.abc import Callable, Mapping
from typing import Protocol, cast

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from apscheduler.triggers.interval import IntervalTrigger

from jarvis.config.schema import SchedulerSection
from jarvis.core.exceptions import SchedulerError
from jarvis.database import SqliteStore, format_timestamp, utc_now
from jarvis.scheduler.store import MIGRATIONS, NAMESPACE, SchedulerRepository
from jarvis.scheduler.types import JobRun, JobSpec, TriggerKind

logger = logging.getLogger("jarvis.scheduler.service")


class _SchedulerLike(Protocol):
    """The slice of APScheduler's ``BackgroundScheduler`` this service uses.

    Declared so the injected object is typed without importing APScheduler's
    (absent) stubs, and so tests can pass a trivial fake.
    """

    def start(self, *, paused: bool = False) -> None: ...

    def shutdown(self, *, wait: bool = True) -> None: ...

    def add_job(self, func: Callable[..., object], trigger: object, **kwargs: object) -> object: ...

    def remove_job(self, job_id: str) -> None: ...

    def get_job(self, job_id: str) -> object | None: ...


def _validate_expression(kind: TriggerKind, expression: str, timezone: str) -> None:
    """Reject a bad schedule at ``add_job`` time, not at 09:00 tomorrow.

    A cron string is validated by handing it to ``CronTrigger.from_crontab`` —
    the same parser that will later run it — so "valid here" and "valid at
    fire time" cannot drift apart.

    Raises:
        SchedulerError: if the expression is empty or not a valid cron/interval.
    """
    text = expression.strip()
    if not text:
        raise SchedulerError("定时表达式不能为空", details={"trigger": kind.value})
    if kind is TriggerKind.CRON:
        try:
            CronTrigger.from_crontab(text, timezone=timezone)
        except (ValueError, TypeError, KeyError) as exc:
            raise SchedulerError(
                f"cron 表达式不合法（需要 5 个字段：分 时 日 月 周）：{exc}",
                details={"expression": text, "reason": str(exc)},
            ) from exc
        return
    if kind is TriggerKind.DATE:
        # A wall-clock moment. Only the shape is checked here: a date that has
        # already passed is not a typo, it is "due now", and APScheduler's misfire
        # policy is the right place to decide that. Callers that mean "in the
        # future" (reminders) say so themselves, against their own clock.
        try:
            datetime.datetime.fromisoformat(text.replace(" ", "T"))
        except ValueError as exc:
            raise SchedulerError(
                f"一次性时间不合法（要 ISO 形式，例如 2026-10-02 09:30）：{exc}",
                details={"expression": text},
            ) from exc
        return
    if not text.isdigit() or int(text) <= 0:
        raise SchedulerError(
            '间隔表达式必须是正整数秒（例如 "60"）',
            details={"expression": text},
        )


def _build_trigger(kind: TriggerKind, expression: str, timezone: str) -> object:
    """Turn a validated expression into an APScheduler trigger object."""
    text = expression.strip()
    if kind is TriggerKind.CRON:
        return CronTrigger.from_crontab(text, timezone=timezone)
    if kind is TriggerKind.DATE:
        return DateTrigger(run_date=datetime.datetime.fromisoformat(text.replace(" ", "T")))
    return IntervalTrigger(seconds=int(text))


class SchedulerService:
    """Lifecycle component running persisted cron/interval jobs."""

    name = "scheduler"

    def __init__(
        self,
        store: SqliteStore,
        settings_provider: Callable[[], SchedulerSection],
        action_runner: Callable[[str, Mapping[str, object]], str],
        *,
        scheduler: object | None = None,
    ) -> None:
        """Wire the service.

        Args:
            store: Shared SQLite store (injected so tests can use ``:memory:``).
            settings_provider: Read the ``scheduler`` config section lazily, so a
                config reload is picked up without rebuilding the service.
            action_runner: Executes ``(action, arguments)`` and returns a result
                string; raising signals failure. Injected to keep this package
                independent of ``jarvis.tools``.
            scheduler: A pre-built APScheduler-like object. ``None`` builds a
                real ``BackgroundScheduler`` on ``start()``; tests pass a fake.
        """
        self._store = store
        self._settings_provider = settings_provider
        self._action_runner = action_runner
        self._repo = SchedulerRepository(store)
        self._scheduler: _SchedulerLike | None = cast("_SchedulerLike | None", scheduler)
        self._injected = scheduler is not None
        self._started = False

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        """Register persisted jobs and start the background thread (idempotent).

        The reload-from-database step is the whole point: a machine that was off
        at 09:00 must still know about the 09:00 job when it comes back.
        """
        if self._started:
            return
        self._store.migrate(NAMESPACE, MIGRATIONS)
        settings = self._settings_provider()
        if not settings.enabled:
            self._started = True
            logger.info("scheduler disabled by config; manual run_now still works")
            return
        if self._scheduler is None:
            self._scheduler = cast(
                "_SchedulerLike",
                BackgroundScheduler(
                    timezone=settings.timezone,
                    job_defaults={
                        "misfire_grace_time": settings.misfire_grace_seconds,
                        "max_instances": settings.max_concurrent,
                        "coalesce": True,
                    },
                ),
            )
        for spec in self._repo.list_jobs(enabled_only=True):
            self._register(spec, settings)
        self._scheduler.start()
        self._started = True
        logger.info("scheduler started with %d job(s)", len(self._repo.list_jobs()))

    def stop(self) -> None:
        """Shut the background thread down. Never raises, safe to call twice."""
        if not self._started:
            return
        scheduler = self._scheduler
        if scheduler is not None:
            try:
                scheduler.shutdown(wait=False)
            except Exception:  # pragma: no cover - APScheduler edge cases
                logger.exception("scheduler shutdown failed")
        # A real BackgroundScheduler cannot be restarted after shutdown; drop it
        # so a later start() builds a fresh one. An injected fake is kept, which
        # is what lets a test exercise stop()/start() round trips.
        if not self._injected:
            self._scheduler = None
        self._started = False

    @property
    def running(self) -> bool:
        """Whether the component has been started."""
        return self._started

    # -- job management ----------------------------------------------------

    def add_job(self, spec: JobSpec) -> JobSpec:
        """Validate, persist and (when running) register a job.

        Validation happens here rather than at fire time so a typo in a cron
        expression is reported to whoever wrote it, immediately.

        Raises:
            SchedulerError: if ``spec.expression`` is not valid for its trigger.
        """
        settings = self._settings_provider()
        _validate_expression(spec.trigger, spec.expression, settings.timezone)
        stored = self._repo.upsert_job(spec)
        if stored.enabled and self._scheduler is not None:
            self._register(stored, settings)
        return stored

    def remove_job(self, job_id: str) -> bool:
        """Delete a job from the database and from the scheduler.

        Returns whether a persisted job existed; removing a job that was only
        ever registered in memory is still not an error.
        """
        removed = self._repo.delete_job(job_id)
        self._unregister(job_id)
        return removed

    def set_enabled(self, job_id: str, enabled: bool) -> bool:
        """Enable or disable a job, keeping its definition.

        Returns whether the job existed. Disabling unregisters the trigger so it
        stops firing without losing the row it can be re-enabled from.
        """
        if not self._repo.set_enabled(job_id, enabled):
            return False
        spec = self._repo.get_job(job_id)
        if spec is None:  # pragma: no cover - deleted between the two statements
            return False
        if enabled:
            if self._scheduler is not None:
                self._register(spec, self._settings_provider())
        else:
            self._unregister(job_id)
        return True

    def list_jobs(self) -> list[JobSpec]:
        """Every job definition. Never raises: the UI polls this on a timer."""
        try:
            return self._repo.list_jobs()
        except Exception:
            logger.exception("listing scheduled jobs failed")
            return []

    def get_job(self, job_id: str) -> JobSpec | None:
        """Fetch one job definition, or ``None``. Never raises."""
        try:
            return self._repo.get_job(job_id)
        except Exception:
            logger.exception("reading scheduled job %s failed", job_id)
            return None

    # -- execution ---------------------------------------------------------

    def run_now(self, job_id: str) -> JobRun:
        """Execute a job immediately, bypassing its schedule, and record it.

        Raises:
            SchedulerError: if no job with that id exists.
        """
        if self._repo.get_job(job_id) is None:
            raise SchedulerError(f"任务不存在：{job_id}", details={"job_id": job_id})
        return self._execute(job_id)

    def history(self, job_id: str | None = None, *, limit: int = 20) -> list[JobRun]:
        """Recent execution records, newest first. Never raises."""
        try:
            return self._repo.history(job_id, limit=limit)
        except Exception:
            logger.exception("reading scheduler history failed")
            return []

    def next_run_time(self, job_id: str) -> str:
        """The next fire time as an ISO string, or ``""`` when unknown."""
        scheduler = self._scheduler
        if scheduler is None:
            return ""
        try:
            job = scheduler.get_job(job_id)
        except Exception:
            logger.exception("reading next run time for %s failed", job_id)
            return ""
        if job is None:
            return ""
        value = getattr(job, "next_run_time", None)
        if not isinstance(value, datetime.datetime):
            return ""
        return format_timestamp(value)

    def stats(self) -> dict[str, object]:
        """Counters for the HUD's automation panel. Never raises."""
        payload: dict[str, object] = {
            "name": self.name,
            "running": self._started,
            "jobs": 0,
            "enabled_jobs": 0,
            "runs": 0,
            "failures": 0,
        }
        try:
            jobs = self._repo.list_jobs()
            payload["jobs"] = len(jobs)
            payload["enabled_jobs"] = sum(1 for job in jobs if job.enabled)
            payload["runs"] = self._repo.count_runs()
            payload["failures"] = self._repo.count_runs(ok=False)
        except Exception:
            logger.exception("computing scheduler stats failed")
        return payload

    # -- internals ---------------------------------------------------------

    def _register(self, spec: JobSpec, settings: SchedulerSection) -> None:
        """Add or replace a trigger in the live scheduler.

        ``replace_existing`` makes this idempotent, which is what lets ``start()``
        and ``set_enabled()`` share one code path.
        """
        scheduler = self._scheduler
        if scheduler is None:
            return
        try:
            scheduler.add_job(
                self._run_job,
                trigger=_build_trigger(spec.trigger, spec.expression, settings.timezone),
                args=[spec.job_id],
                id=spec.job_id,
                name=spec.name,
                replace_existing=True,
                misfire_grace_time=settings.misfire_grace_seconds,
                max_instances=settings.max_concurrent,
            )
        except Exception:
            logger.exception("registering job %s failed", spec.job_id)

    def _unregister(self, job_id: str) -> None:
        """Remove a trigger if present; absence is not an error."""
        scheduler = self._scheduler
        if scheduler is None:
            return
        try:
            scheduler.remove_job(job_id)
        except Exception:
            logger.debug("job %s was not registered", job_id)

    def _run_job(self, job_id: str) -> None:
        """APScheduler callback: run a job and swallow every failure.

        An exception escaping here would be APScheduler's problem to log and,
        depending on the job store, could take the job out of service. Recording
        it and returning keeps the next tick alive.
        """
        try:
            run = self._execute(job_id)
            self._retire_one_shot(job_id, fired_for_real=run.ok)
        except Exception:  # pragma: no cover - defensive, _execute already guards
            logger.exception("scheduled job %s crashed", job_id)

    def _retire_one_shot(self, job_id: str, *, fired_for_real: bool) -> None:
        """Turn a one-shot job off after it has fired.

        A reminder that already went off must not sit in the list claiming it is
        still waiting, and it must not fire again if the row is re-registered after
        a restart with a clock that has moved past it. Disabling rather than deleting
        keeps the row as the record of what was asked, and it is what
        ``set_enabled(False)`` is for.

        A one-shot whose action *failed* stays enabled: "your reminder never came and
        the list says it already happened" is the worse failure, and the next start
        will try again under the misfire policy. ``run_now`` deliberately does not
        come through here -- testing a job by hand is not it firing.
        """
        if not fired_for_real:
            return
        spec = self._repo.get_job(job_id)
        if spec is None or spec.trigger is not TriggerKind.DATE:
            return
        self._repo.set_enabled(job_id, False)
        self._unregister(job_id)

    def _execute(self, job_id: str) -> JobRun:
        """Run one job and persist the outcome; never propagates action errors."""
        started_at = format_timestamp(utc_now())
        start = time.monotonic()
        spec = self._repo.get_job(job_id)
        ok = True
        detail = ""
        error = ""
        if spec is None:
            ok = False
            error = "任务不存在"
        else:
            try:
                detail = self._action_runner(spec.action, spec.arguments)
            except Exception as exc:
                ok = False
                error = f"{type(exc).__name__}: {exc}"
                logger.warning(
                    "scheduled job %s failed after %.0f ms: %s",
                    job_id,
                    (time.monotonic() - start) * 1000,
                    error,
                )
        finished_at = format_timestamp(utc_now())
        run_id = self._repo.record_run(
            job_id=job_id,
            started_at=started_at,
            finished_at=finished_at,
            ok=ok,
            detail=detail,
            error=error,
        )
        return JobRun(
            run_id=run_id,
            job_id=job_id,
            started_at=started_at,
            finished_at=finished_at,
            ok=ok,
            detail=detail,
            error=error,
        )


__all__ = ["SchedulerService"]
