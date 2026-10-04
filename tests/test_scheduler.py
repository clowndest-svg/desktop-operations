"""Tests for the scheduler (jarvis.scheduler).

Every test injects a fake APScheduler so nothing waits on a wall clock: the
value of these tests is the *policy* (validation, persistence, restart
recovery, failure isolation), not APScheduler's own trigger arithmetic.
"""

from __future__ import annotations

import datetime
from collections.abc import Callable, Mapping
from pathlib import Path

import pytest

from jarvis.config.schema import SchedulerSection
from jarvis.core.exceptions import SchedulerError
from jarvis.database import SqliteStore
from jarvis.scheduler import JobSpec, SchedulerService, TriggerKind
from jarvis.scheduler.store import MIGRATIONS, NAMESPACE, SchedulerRepository


def _section(**overrides: object) -> SchedulerSection:
    raw: dict[str, object] = {
        "enabled": True,
        "timezone": "Asia/Shanghai",
        "max_concurrent": 2,
        "misfire_grace_seconds": 60,
    }
    raw.update(overrides)
    return SchedulerSection.from_mapping(raw)


def _store() -> SqliteStore:
    """A throwaway in-memory store (WAL is unsupported for ``:memory:``)."""
    store = SqliteStore(Path(":memory:"), journal_mode="MEMORY")
    store.start()
    return store


class FakeJob:
    """Stand-in for an APScheduler job, with a next fire time."""

    def __init__(self, job_id: str) -> None:
        self.id = job_id
        self.next_run_time: datetime.datetime | None = datetime.datetime(
            2030, 1, 1, tzinfo=datetime.UTC
        )


class FakeScheduler:
    """Records registrations instead of starting timers."""

    def __init__(self) -> None:
        self.jobs: dict[str, FakeJob] = {}
        self.funcs: dict[str, Callable[..., object]] = {}
        self.triggers: dict[str, object] = {}
        self.add_calls: list[dict[str, object]] = []
        self.started = False
        self.shutdown_calls = 0

    def start(self, *, paused: bool = False) -> None:
        self.started = True

    def shutdown(self, *, wait: bool = True) -> None:
        self.started = False
        self.shutdown_calls += 1

    @property
    def running(self) -> bool:
        return self.started

    def add_job(self, func: Callable[..., object], trigger: object, **kwargs: object) -> object:
        job_id = str(kwargs["id"])
        self.jobs[job_id] = FakeJob(job_id)
        self.funcs[job_id] = func
        self.triggers[job_id] = trigger
        self.add_calls.append(dict(kwargs))
        return self.jobs[job_id]

    def remove_job(self, job_id: str) -> None:
        if job_id not in self.jobs:
            raise KeyError(job_id)
        del self.jobs[job_id]

    def get_job(self, job_id: str) -> object | None:
        return self.jobs.get(job_id)


class RecordingRunner:
    """An ``action_runner`` that records calls and can be told to fail."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.failures: set[str] = set()

    def __call__(self, action: str, arguments: Mapping[str, object]) -> str:
        self.calls.append((action, dict(arguments)))
        if action in self.failures:
            raise RuntimeError(f"{action} 爆炸了")
        return f"{action} 完成"


def _build(
    store: SqliteStore,
    runner: RecordingRunner | None = None,
    **overrides: object,
) -> tuple[SchedulerService, RecordingRunner, FakeScheduler]:
    """A started-able service with a fake scheduler and recording runner."""
    runner = runner or RecordingRunner()
    fake = FakeScheduler()
    service = SchedulerService(store, lambda: _section(**overrides), runner, scheduler=fake)
    return service, runner, fake


def _spec(
    job_id: str = "job-1",
    *,
    trigger: TriggerKind = TriggerKind.CRON,
    expression: str = "0 9 * * *",
    enabled: bool = True,
    action: str = "system_report",
    arguments: Mapping[str, object] | None = None,
) -> JobSpec:
    return JobSpec(
        job_id=job_id,
        name="早报",
        action=action,
        arguments=arguments or {},
        trigger=trigger,
        expression=expression,
        enabled=enabled,
    )


class TestExpressionValidation:
    def test_valid_cron_is_accepted(self) -> None:
        service, _, fake = _build(_store())
        service.start()
        stored = service.add_job(_spec())
        assert stored.expression == "0 9 * * *"
        assert "job-1" in fake.jobs

    def test_invalid_cron_is_rejected_at_add_time(self) -> None:
        """A typo must surface to whoever wrote it, not at 09:00 tomorrow."""
        service, _, fake = _build(_store())
        service.start()
        with pytest.raises(SchedulerError):
            service.add_job(_spec(expression="not a cron"))
        assert "job-1" not in fake.jobs

    def test_cron_with_wrong_field_count_is_rejected(self) -> None:
        service, _, _ = _build(_store())
        service.start()
        with pytest.raises(SchedulerError):
            service.add_job(_spec(expression="0 9 * *"))

    @pytest.mark.parametrize("expression", ["0", "abc", "-5", "1.5", ""])
    def test_interval_must_be_a_positive_integer(self, expression: str) -> None:
        service, _, _ = _build(_store())
        service.start()
        with pytest.raises(SchedulerError):
            service.add_job(_spec(trigger=TriggerKind.INTERVAL, expression=expression))

    def test_interval_job_is_accepted(self) -> None:
        service, _, fake = _build(_store())
        service.start()
        service.add_job(_spec(trigger=TriggerKind.INTERVAL, expression="60"))
        assert "job-1" in fake.jobs


class TestJobManagement:
    def test_add_job_persists_and_registers(self) -> None:
        store = _store()
        service, _, fake = _build(store)
        service.start()
        service.add_job(_spec(arguments={"路径": "C:/tmp", "n": 3}))
        assert [job.job_id for job in service.list_jobs()] == ["job-1"]
        assert "job-1" in fake.jobs

    def test_arguments_survive_a_database_round_trip(self) -> None:
        """Chinese keys and numeric values must come back exactly as written."""
        store = _store()
        service, _, _ = _build(store)
        service.start()
        service.add_job(_spec(arguments={"路径": "C:/tmp", "n": 3}))
        reloaded = service.get_job("job-1")
        assert reloaded is not None
        assert reloaded.arguments == {"路径": "C:/tmp", "n": 3}

    def test_get_job_returns_none_for_unknown(self) -> None:
        service, _, _ = _build(_store())
        service.start()
        assert service.get_job("nope") is None

    def test_remove_job_deletes_and_unregisters(self) -> None:
        service, _, fake = _build(_store())
        service.start()
        service.add_job(_spec())
        assert service.remove_job("job-1") is True
        assert service.get_job("job-1") is None
        assert "job-1" not in fake.jobs

    def test_remove_unknown_job_returns_false(self) -> None:
        service, _, _ = _build(_store())
        service.start()
        assert service.remove_job("nope") is False

    def test_disabling_a_job_keeps_its_definition(self) -> None:
        """Disable must be reversible: the row stays, only the trigger goes."""
        service, _, fake = _build(_store())
        service.start()
        service.add_job(_spec())
        assert service.set_enabled("job-1", False) is True
        assert "job-1" not in fake.jobs
        stored = service.get_job("job-1")
        assert stored is not None and stored.enabled is False

    def test_reenabling_a_job_registers_it_again(self) -> None:
        service, _, fake = _build(_store())
        service.start()
        service.add_job(_spec())
        service.set_enabled("job-1", False)
        service.set_enabled("job-1", True)
        assert "job-1" in fake.jobs

    def test_set_enabled_unknown_job_returns_false(self) -> None:
        service, _, _ = _build(_store())
        service.start()
        assert service.set_enabled("nope", True) is False


class TestLifecycle:
    def test_start_is_idempotent(self) -> None:
        service, _, fake = _build(_store())
        service.start()
        service.start()
        assert fake.shutdown_calls == 0
        assert service.running is True

    def test_start_registers_only_enabled_jobs(self) -> None:
        store = _store()
        service, _, fake = _build(store)
        service.start()
        service.add_job(_spec("on", enabled=True))
        service.add_job(_spec("off", enabled=False))
        fake.jobs.clear()
        service.stop()
        service.start()
        assert set(fake.jobs) == {"on"}

    def test_restart_rereads_jobs_from_the_database(self) -> None:
        """The whole point of persisting: a new process still knows the 09:00 job."""
        store = _store()
        first, _, _ = _build(store)
        first.start()
        first.add_job(_spec())
        first.stop()

        second, _, second_fake = _build(store)
        second.start()
        assert "job-1" in second_fake.jobs

    def test_disabled_config_skips_the_scheduler_but_still_starts(self) -> None:
        store = _store()
        service = SchedulerService(store, lambda: _section(enabled=False), RecordingRunner())
        service.start()
        assert service.running is True
        assert service.stats()["enabled_jobs"] == 0

    def test_stop_is_safe_to_call_twice(self) -> None:
        service, _, fake = _build(_store())
        service.start()
        service.stop()
        service.stop()
        assert fake.shutdown_calls == 1
        assert service.running is False

    def test_trigger_parameters_come_from_config(self) -> None:
        """misfire grace and concurrency are config, never hard-coded."""
        service, _, fake = _build(_store(), misfire_grace_seconds=120, max_concurrent=7)
        service.start()
        service.add_job(_spec())
        call = fake.add_calls[-1]
        assert call["misfire_grace_time"] == 120
        assert call["max_instances"] == 7


class TestExecution:
    def test_run_now_records_success(self) -> None:
        service, runner, _ = _build(_store())
        service.start()
        service.add_job(_spec())
        run = service.run_now("job-1")
        assert run.ok is True
        assert run.detail == "system_report 完成"
        assert run.error == ""
        assert runner.calls == [("system_report", {})]

    def test_run_now_unknown_job_raises(self) -> None:
        service, _, _ = _build(_store())
        service.start()
        with pytest.raises(SchedulerError):
            service.run_now("nope")

    def test_action_failure_is_recorded_not_raised(self) -> None:
        """An exploding action must become a history row, not an exception."""
        service, runner, _ = _build(_store())
        service.start()
        service.add_job(_spec())
        runner.failures.add("system_report")
        run = service.run_now("job-1")
        assert run.ok is False
        assert "RuntimeError" in run.error
        assert service.history("job-1")[0].ok is False

    def test_failure_does_not_stop_later_runs(self) -> None:
        """The scheduler thread must survive a failing job."""
        service, runner, _ = _build(_store())
        service.start()
        service.add_job(_spec())
        runner.failures.add("system_report")
        service.run_now("job-1")
        runner.failures.clear()
        assert service.run_now("job-1").ok is True

    def test_registered_callback_executes_and_records(self) -> None:
        """Simulates a real trigger firing the callback APScheduler was given."""
        service, runner, fake = _build(_store())
        service.start()
        service.add_job(_spec())
        fake.funcs["job-1"]("job-1")
        assert runner.calls == [("system_report", {})]
        assert service.history("job-1")[0].ok is True

    def test_history_is_newest_first_and_limited(self) -> None:
        service, _, _ = _build(_store())
        service.start()
        service.add_job(_spec())
        for _ in range(3):
            service.run_now("job-1")
        recent = service.history("job-1", limit=2)
        assert len(recent) == 2
        assert recent[0].run_id > recent[1].run_id

    def test_history_can_be_scoped_to_one_job(self) -> None:
        service, _, _ = _build(_store())
        service.start()
        service.add_job(_spec("a"))
        service.add_job(_spec("b"))
        service.run_now("a")
        assert [run.job_id for run in service.history("b")] == []

    def test_next_run_time_returns_iso_string(self) -> None:
        service, _, _ = _build(_store())
        service.start()
        service.add_job(_spec())
        assert service.next_run_time("job-1").startswith("2030-01-01")

    def test_next_run_time_is_empty_for_unknown_job(self) -> None:
        service, _, _ = _build(_store())
        service.start()
        assert service.next_run_time("nope") == ""

    def test_stats_summarises_jobs_and_runs(self) -> None:
        service, runner, _ = _build(_store())
        service.start()
        service.add_job(_spec("a"))
        service.add_job(_spec("b", enabled=False))
        service.run_now("a")
        runner.failures.add("system_report")
        service.run_now("a")
        stats = service.stats()
        assert stats["jobs"] == 2
        assert stats["enabled_jobs"] == 1
        assert stats["runs"] == 2
        assert stats["failures"] == 1

    def test_read_methods_never_raise_when_the_store_is_closed(self) -> None:
        """The UI polls these on a timer; a dead database must not crash the HUD."""
        store = _store()
        service, _, _ = _build(store)
        service.start()
        service.add_job(_spec())
        store.stop()
        assert service.list_jobs() == []
        assert service.history() == []
        assert service.stats()["jobs"] == 0


class TestOneShotDateTrigger:
    """``DATE`` exists because a reminder is a moment, not a recurrence.

    The retirement rules matter more than the parsing: a one-shot that stays enabled
    after firing tells the operator it is still waiting, and one that gets disabled
    after *failing* means the reminder never came and the list says it already did.
    """

    def test_a_wall_clock_moment_is_accepted_and_registered(self) -> None:
        service, _, fake = _build(_store())
        service.start()
        stored = service.add_job(_spec(trigger=TriggerKind.DATE, expression="2030-01-01T09:30"))
        assert stored.trigger is TriggerKind.DATE
        assert type(fake.triggers["job-1"]).__name__ == "DateTrigger"

    @pytest.mark.parametrize("expression", ["tomorrow", "9 点半", "2030-13-01T09:30", ""])
    def test_a_moment_that_is_not_one_is_refused_at_add_time(self, expression: str) -> None:
        service, _, fake = _build(_store())
        service.start()
        with pytest.raises(SchedulerError):
            service.add_job(_spec(trigger=TriggerKind.DATE, expression=expression))
        assert "job-1" not in fake.jobs

    def test_a_space_instead_of_the_iso_t_is_fine(self) -> None:
        """People type 「2030-01-01 09:30」; rejecting it would be pedantry with a cost."""
        service, _, fake = _build(_store())
        service.start()
        service.add_job(_spec(trigger=TriggerKind.DATE, expression="2030-01-01 09:30"))
        assert "job-1" in fake.jobs

    def test_firing_retires_the_one_shot(self) -> None:
        service, _, fake = _build(_store())
        service.start()
        service.add_job(_spec(trigger=TriggerKind.DATE, expression="2030-01-01T09:30"))
        service._run_job("job-1")
        assert service.get_job("job-1") is not None
        assert service.get_job("job-1").enabled is False  # type: ignore[union-attr]
        assert "job-1" not in fake.jobs

    def test_a_failed_one_shot_stays_enabled(self) -> None:
        """Better it tries again than your reminder quietly never comes."""
        runner = RecordingRunner()
        runner.failures.add("system_report")
        service, _, _ = _build(_store(), runner)
        service.start()
        service.add_job(_spec(trigger=TriggerKind.DATE, expression="2030-01-01T09:30"))
        service._run_job("job-1")
        assert service.get_job("job-1").enabled is True  # type: ignore[union-attr]

    def test_running_by_hand_does_not_retire_it(self) -> None:
        """ "试一下" is not the reminder firing; the row must survive the test."""
        service, _, _ = _build(_store())
        service.start()
        service.add_job(_spec(trigger=TriggerKind.DATE, expression="2030-01-01T09:30"))
        service.run_now("job-1")
        assert service.get_job("job-1").enabled is True  # type: ignore[union-attr]

    def test_a_cron_job_is_left_alone_after_firing(self) -> None:
        service, _, _ = _build(_store())
        service.start()
        service.add_job(_spec())
        service._run_job("job-1")
        assert service.get_job("job-1").enabled is True  # type: ignore[union-attr]


def _record(repo: SchedulerRepository, index: int) -> None:
    """One run record, with row order and timestamp order deliberately agreeing."""
    repo.record_run(
        job_id="reminder:喝水",
        started_at=f"2026-10-03T09:0{index}:00",
        finished_at=f"2026-10-03T09:0{index}:01",
        ok=True,
        detail="已开口，已弹托盘",
        error="",
    )


class TestHistoryRetention:
    """The run history is capped, because this process is meant to run for weeks.

    The table was append-only in the strongest sense: nothing ever deleted from it.
    A reminder firing every day on a machine that is left on turns "history" into a
    leak, and every ``COUNT(*)`` the panel does has to pay for the whole of it.
    """

    def test_prune_keeps_the_newest_rows(self) -> None:
        store = _store()
        store.migrate(NAMESPACE, MIGRATIONS)
        repo = SchedulerRepository(store)
        for index in range(10):
            _record(repo, index)
        assert repo.prune_runs(4) == 6
        assert repo.count_runs() == 4
        # The survivors are the newest: that is what the panel shows and what the
        # failure count is about.
        assert [run.started_at for run in repo.history(limit=10)] == [
            "2026-10-03T09:09:00",
            "2026-10-03T09:08:00",
            "2026-10-03T09:07:00",
            "2026-10-03T09:06:00",
        ]

    def test_prune_reports_zero_when_there_is_nothing_to_drop(self) -> None:
        store = _store()
        store.migrate(NAMESPACE, MIGRATIONS)
        repo = SchedulerRepository(store)
        _record(repo, 0)
        assert repo.prune_runs(10) == 0
        assert repo.count_runs() == 1

    def test_a_non_positive_keep_deletes_nothing(self) -> None:
        """A misconfigured limit must not be read as "delete everything"."""
        store = _store()
        store.migrate(NAMESPACE, MIGRATIONS)
        repo = SchedulerRepository(store)
        _record(repo, 0)
        assert repo.prune_runs(0) == 0
        assert repo.count_runs() == 1

    def test_start_applies_the_limit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Wired into the lifecycle, not merely available as a method."""
        monkeypatch.setattr("jarvis.scheduler.service.RUN_HISTORY_LIMIT", 2)
        store = _store()
        store.migrate(NAMESPACE, MIGRATIONS)
        repo = SchedulerRepository(store)
        for index in range(5):
            _record(repo, index)
        service, _, _ = _build(store, RecordingRunner())
        service.start()
        assert repo.count_runs() == 2
