"""Reminders, the announcer, and the four tools that expose them.

The parser gets the most attention here because it is the only part that can be
quietly wrong: a reminder set for the wrong hour still "succeeds", and nobody finds
out at 09:00 -- the person who was supposed to be told does. Every assertion is
against a pinned ``now`` for that reason.
"""

from __future__ import annotations

import datetime
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, cast

import pytest

from jarvis.app.announcer import Announcer
from jarvis.app.reminder_service import HELP, ReminderService, chinese_number, parse_when
from jarvis.core.exceptions import JarvisError
from jarvis.scheduler.types import JobSpec, TriggerKind
from jarvis.tools.builtins import assistant_tools

NOON = datetime.datetime(2026, 10, 2, 12, 0, 0)


def recorder(into: list[str]) -> Callable[[str], bool]:
    """A speaker that records what it was asked to say and accepts every request."""

    def speak(text: str) -> bool:
        into.append(text)
        return True

    return speak


def spec(job_id: str, text: str = "喝水", *, enabled: bool = True, **extra: Any) -> JobSpec:
    return JobSpec(
        job_id=job_id,
        name=text,
        action="speak",
        arguments={"text": f"提醒：{text}", "title": "提醒"},
        trigger=TriggerKind.DATE,
        expression="2026-10-02T12:10",
        enabled=enabled,
        **extra,
    )


class FakeScheduler:
    def __init__(self, jobs: list[JobSpec] | None = None) -> None:
        self.jobs: list[JobSpec] = list(jobs or [])
        self.added: list[JobSpec] = []
        self.removed: list[str] = []
        self.toggles: list[tuple[str, bool]] = []

    def add_job(self, spec: JobSpec) -> JobSpec:
        self.added.append(spec)
        self.jobs.append(spec)
        return spec

    def remove_job(self, job_id: str) -> bool:
        self.removed.append(job_id)
        before = len(self.jobs)
        self.jobs = [job for job in self.jobs if job.job_id != job_id]
        return len(self.jobs) < before

    def set_enabled(self, job_id: str, enabled: bool) -> bool:
        self.toggles.append((job_id, enabled))
        return any(job.job_id == job_id for job in self.jobs)

    def list_jobs(self) -> list[JobSpec]:
        return list(self.jobs)

    def next_run_time(self, job_id: str) -> str:
        return "2026-10-02 12:10:00"


def service(jobs: list[JobSpec] | None = None) -> tuple[ReminderService, FakeScheduler]:
    fake = FakeScheduler(jobs)
    return ReminderService(lambda: fake, clock=lambda: NOON), fake


class TestChineseNumbers:
    @pytest.mark.parametrize(
        ("token", "expected"),
        [
            ("10", 10.0),
            ("十", 10.0),
            ("两", 2.0),
            ("半", 0.5),
            ("二十", 20.0),
            ("二十三", 23.0),
            ("", None),
            ("花", None),
        ],
    )
    def test_the_tokens_the_parser_needs(self, token: str, expected: float | None) -> None:
        assert chinese_number(token) == expected


class TestParseWhen:
    def test_relative_minutes(self) -> None:
        assert parse_when("十分钟后", now=NOON) == NOON + datetime.timedelta(minutes=10)

    def test_relative_half_an_hour(self) -> None:
        assert parse_when("半小时以后", now=NOON) == NOON + datetime.timedelta(minutes=30)

    def test_relative_hours_and_days(self) -> None:
        assert parse_when("3 小时后", now=NOON) == NOON + datetime.timedelta(hours=3)
        assert parse_when("两天后", now=NOON) == NOON + datetime.timedelta(days=2)

    def test_named_day_with_a_clock_time(self) -> None:
        assert parse_when("明天 9 点半", now=NOON) == datetime.datetime(2026, 10, 3, 9, 30)
        assert parse_when("今天 18:05", now=NOON) == datetime.datetime(2026, 10, 2, 18, 5)
        assert parse_when("后天早上 8 点", now=NOON) == datetime.datetime(2026, 10, 4, 8, 0)

    def test_a_bare_time_that_already_passed_means_tomorrow(self) -> None:
        """Saying "9 点半" at noon is about tomorrow's 9:30, not five hours ago."""
        assert parse_when("9 点半", now=NOON) == datetime.datetime(2026, 10, 3, 9, 30)
        assert parse_when("19:40", now=NOON) == datetime.datetime(2026, 10, 2, 19, 40)

    def test_iso(self) -> None:
        assert parse_when("2026-10-03 08:00", now=NOON) == datetime.datetime(2026, 10, 3, 8, 0)

    @pytest.mark.parametrize("phrase", ["", "   ", "等一会儿", "下班的时候", "2026-13-45 08:00"])
    def test_what_it_does_not_understand_it_says_so(self, phrase: str) -> None:
        """Refusing is the safe answer. Guessing a moment would set a reminder nobody
        can tell was wrong until it went off at the wrong time -- or never did."""
        assert parse_when(phrase, now=NOON) is None


class TestReminderService:
    def test_add_writes_a_one_shot_speak_job(self) -> None:
        reminders, fake = service()
        made = reminders.add("喝水", "十分钟后")
        assert made.text == "喝水"
        # The row is the display view: the ISO "T" is swapped for a space so the
        # panel does not have to know which shape the scheduler stores.
        assert made.when == "2026-10-02 12:10"
        job = fake.added[0]
        assert job.trigger is TriggerKind.DATE
        assert job.action == "speak"
        assert job.arguments["text"] == "提醒：喝水"
        assert job.job_id.startswith("reminder:")

    def test_an_empty_message_is_refused(self) -> None:
        reminders, _ = service()
        with pytest.raises(JarvisError):
            reminders.add("   ", "十分钟后")

    def test_an_ununderstood_time_carries_the_list_of_shapes(self) -> None:
        reminders, _ = service()
        with pytest.raises(JarvisError) as raised:
            reminders.add("喝水", "回头")
        assert "听不懂" in str(raised.value)

    def test_a_moment_in_the_past_is_not_silently_accepted(self) -> None:
        reminders, fake = service()
        with pytest.raises(JarvisError):
            reminders.add("喝水", "", when=NOON - datetime.timedelta(minutes=1))
        assert fake.added == []

    def test_the_text_is_clipped_not_rejected(self) -> None:
        reminders, fake = service()
        reminders.add("长" * 500, "十分钟后")
        assert len(str(fake.added[0].arguments["text"])) < 260

    def test_only_reminders_are_listed(self) -> None:
        reminders, _ = service([spec("reminder:a1"), spec("workflow:早报", "早报")])
        assert [row.job_id for row in reminders.all_reminders()] == ["reminder:a1"]

    def test_waiting_rows_come_first_and_soonest(self) -> None:
        rows = [spec("reminder:b", "晚"), spec("reminder:a", "早", enabled=False)]
        reminders, _ = service(rows)
        listing = reminders.all_reminders()
        assert [row.text for row in listing] == ["晚", "早"]
        assert reminders.upcoming(3) == [listing[0]]

    def test_cancel_by_keyword_reports_what_it_removed(self) -> None:
        reminders, fake = service([spec("reminder:a1", "喝水")])
        assert reminders.cancel("喝水").startswith("喝水")
        assert fake.removed == ["reminder:a1"]

    def test_cancel_with_a_short_id_works(self) -> None:
        reminders, fake = service([spec("reminder:a1", "喝水")])
        reminders.cancel("a1")
        assert fake.removed == ["reminder:a1"]

    def test_an_ambiguous_keyword_refuses_instead_of_choosing(self) -> None:
        """Guessing which of two reminders to destroy is not a cancellation."""
        reminders, fake = service([spec("reminder:a1", "喝水"), spec("reminder:a2", "记得喝水")])
        with pytest.raises(JarvisError) as raised:
            reminders.cancel("喝水")
        assert "2 条" in str(raised.value)
        assert fake.removed == []

    def test_cancel_something_absent_says_so(self) -> None:
        reminders, _ = service([spec("reminder:a1", "喝水")])
        with pytest.raises(JarvisError):
            reminders.cancel("散步")

    def test_stats_count_waiting_and_done(self) -> None:
        reminders, _ = service([spec("reminder:a1"), spec("reminder:a2", "开会", enabled=False)])
        stats = reminders.stats()
        assert stats["total"] == 2 and stats["waiting"] == 1 and stats["done"] == 1

    def test_a_broken_scheduler_does_not_raise_into_the_panel(self) -> None:
        class Broken:
            def list_jobs(self) -> list[JobSpec]:
                raise RuntimeError("库被占了")

        reminders = ReminderService(lambda: cast(Any, Broken()), clock=lambda: NOON)
        assert reminders.all_reminders() == []
        assert reminders.stats()["total"] == 0


class TestAnnouncer:
    def test_nothing_bound_says_nothing_happened(self) -> None:
        assert "没有可用的播报通道" in Announcer().announce("该喝水了")

    def test_speaking_is_reported_per_channel(self) -> None:
        said: list[str] = []
        popped: list[str] = []

        def speak(text: str) -> bool:
            said.append(text)
            return True

        announcer = Announcer(speak=speak, notify=lambda text, title="x": popped.append(text))
        line = announcer.announce("该喝水了")
        assert said == ["该喝水了"] and popped == ["该喝水了"]
        assert "已开口" in line and "已弹托盘" in line

    def test_a_speaker_that_refuses_is_not_reported_as_delivered(self) -> None:
        announcer = Announcer(speak=lambda text: False)
        assert "没出声" in announcer.announce("该喝水了")

    def test_a_notifier_that_only_takes_the_message_still_gets_it(self) -> None:
        """A one-argument callable is a perfectly good notifier; a signature is not a
        reason to lose the balloon."""
        seen: list[str] = []
        Announcer(notify=seen.append).announce("该喝水了")
        assert seen == ["该喝水了"]

    def test_a_notifier_that_throws_does_not_take_the_thread_down(self) -> None:
        def explode(_text: str) -> None:
            raise RuntimeError("托盘没了")

        announcer = Announcer(speak=lambda text: True, notify=explode)
        assert "已开口" in announcer.announce("该喝水了")

    def test_channels_are_what_is_bound_not_what_was_tried(self) -> None:
        assert Announcer().channels == ()
        assert Announcer(speak=lambda text: True).channels == ("voice",)

    def test_blank_text_is_not_announced(self) -> None:
        said: list[str] = []
        assert "没有可播报" in Announcer(speak=recorder(said)).announce("   ")
        assert said == []


class TestTools:
    def test_nothing_injected_advertises_nothing(self) -> None:
        """A tool the registry cannot reach must not be offered to the model."""
        assert assistant_tools.build() == []
        assert [spec_.name for spec_, _ in assistant_tools.build(speaker=Announcer())] == ["speak"]

    def test_all_four_when_both_backings_exist(self) -> None:
        reminders, _ = service()
        names = {
            spec_.name
            for spec_, _ in assistant_tools.build(speaker=Announcer(), reminders=reminders)
        }
        assert names == {"speak", "add_reminder", "list_reminders", "cancel_reminder"}

    def test_the_reminder_tools_read_as_the_service_answers(self) -> None:
        reminders, _ = service([spec("reminder:a1", "喝水")])
        tools = {
            spec_.name: handler
            for spec_, handler in assistant_tools.build(
                speaker=Announcer(speak=lambda text: True), reminders=reminders
            )
        }
        assert "喝水" in tools["list_reminders"]({})
        assert "已设置提醒" in tools["add_reminder"]({"text": "散步", "when": "半小时后"})
        assert "取消失败" in tools["cancel_reminder"]({"key": "不存在的事"})
        assert "已取消" in tools["cancel_reminder"]({"key": "喝水"})

    def test_speak_without_text_does_not_make_noise(self) -> None:
        said: list[str] = []
        tools = {
            spec_.name: handler
            for spec_, handler in assistant_tools.build(speaker=Announcer(speak=recorder(said)))
        }
        assert "没有要念的内容" in tools["speak"]({})
        assert said == []

    def test_every_new_tool_is_declared_safe(self) -> None:
        """These write a row and make a sound. If one ever gains a machine-touching
        parameter, this assertion is where the argument has to happen."""
        reminders, _ = service()
        for spec_, _ in assistant_tools.build(speaker=Announcer(), reminders=reminders):
            assert spec_.risk.value == "safe", spec_.name

    def test_the_help_string_names_the_shapes_that_work(self) -> None:
        for shape in ("分钟后", "明天", "19:40"):
            assert shape in HELP


class TestAReminderActuallyFires:
    """The whole chain, end to end: scheduler → tool registry → announcer.

    Each link has its own tests above; this one exists because the links are joined
    by *names* -- the job stores the action string ``speak``, and the registry is the
    only thing that turns it into a call. A rename on either side would leave every
    unit test green and every reminder silent.
    """

    def test_a_fired_reminder_is_announced_and_retired(self) -> None:
        from jarvis.config.schema import SchedulerSection, ToolsSection
        from jarvis.database import SqliteStore
        from jarvis.scheduler.service import SchedulerService
        from jarvis.tools.registry import ToolRegistry

        said: list[str] = []

        def accept(text: str) -> bool:
            said.append(text)
            return True

        announcer = Announcer(speak=accept)
        section = ToolsSection(
            enabled=True,
            confirm_dangerous=False,
            allow_write=False,
            allow_shell=False,
            file_roots=(),
            max_result_chars=10_000,
        )
        registry = ToolRegistry(lambda: section)
        for tool_spec, handler in assistant_tools.build(speaker=announcer):
            registry.register(tool_spec, handler)

        def run(action: str, arguments: Mapping[str, object]) -> str:
            """The real registry, not a hand-wired dict.

            ``_tool_action_runner`` calls ``tools.invoke``, which validates the stored
            arguments against the advertised schema before calling anything. A dict of
            handlers skips that step -- and did, on the first machine test, where the
            reminder's ``title`` argument was rejected by a ``speak`` tool that had
            never declared it. The job fired, the tool refused, the person heard
            nothing: exactly the silence this class exists to prevent.
            """
            result = registry.invoke(action, dict(arguments))
            if not result.ok:
                raise AssertionError(f"动作 {action} 失败：{result.error}")
            return result.output

        store = SqliteStore(Path(":memory:"), journal_mode="MEMORY")
        store.start()
        try:
            scheduler = SchedulerService(
                store,
                lambda: SchedulerSection.from_mapping(
                    {
                        "enabled": True,
                        "timezone": "Asia/Shanghai",
                        "max_concurrent": 2,
                        "misfire_grace_seconds": 60,
                    }
                ),
                run,
            )
            scheduler.start()
            reminders = ReminderService(lambda: scheduler, clock=lambda: NOON)
            made = reminders.add("喝水", "十分钟后")

            # Fire it the way APScheduler would, rather than waiting ten minutes.
            scheduler._run_job(made.job_id)
            assert said == ["提醒：喝水"]
            history = scheduler.history(made.job_id, limit=1)
            assert history and history[0].ok and "已开口" in history[0].detail
            assert reminders.all_reminders()[0].enabled is False
        finally:
            store.stop()
