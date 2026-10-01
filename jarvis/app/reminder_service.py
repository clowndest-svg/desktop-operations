"""Reminders: the scheduler, aimed at a person's day.

Why a layer on top of the scheduler instead of more scheduler
-------------------------------------------------------------
:class:`~jarvis.scheduler.service.SchedulerService` stores and fires *jobs*: a name,
an action, a trigger. A reminder is a job plus three things that are not the
scheduler's business -- the Chinese sentence that names when, the text to say when it
comes due, and the rule that a one-shot which has fired should stop pretending it is
waiting. This module owns those three and delegates every minute of clockwork.

Why the time is parsed here and not by the model
-------------------------------------------------
Handing "十分钟后" to the model would cost an API call per reminder, would be wrong
in ways nobody can see (a model has no clock unless you give it one), and would fail
silently at 3 a.m. when the provider is unreachable. A pure function with tests
either produces a moment or says it did not understand -- and "听不懂这个时间" is a
better answer than a reminder set for the wrong hour.

The supported shapes are deliberately narrow: relative spans (「十分钟后」), named
days with a clock time (「明天 9 点半」), a bare clock time (「19:40」, rolling to
tomorrow once it has passed), and ISO. Anything else is refused with the list.
"""

from __future__ import annotations

import datetime
import logging
import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from jarvis.core.exceptions import JarvisError
from jarvis.scheduler.types import JobSpec, TriggerKind

logger = logging.getLogger("jarvis.app.reminder_service")

JOB_PREFIX = "reminder:"
"""Namespaces the job id so the list, the cancel path and a future cron feature can
tell a reminder from a workflow trigger without a schema change."""

SPEAK_ACTION = "speak"
"""The tool a reminder fires. Going through the tool registry rather than calling the
voice service directly is what keeps ``scheduler`` free of an import of ``app``."""

MAX_TEXT_CHARS = 200
"""A reminder is a sentence, not a document; the row and the balloon both assume it
is short."""

_RELATIVE = re.compile(
    r"(?P<amount>\d+(?:\.\d+)?|[一二两三四五六七八九十半]+)\s*"
    r"(?P<unit>秒钟?|分钟?|个?小时|钟头|小时间|天|日|周|星期)"
    r"(?:之?后|以后| later|内)?"
)
_CLOCK = re.compile(r"(?P<hour>[01]?\d|2[0-3])\s*[点:：]\s*(?P<minute>半|一刻|[0-5]?\d)?")
_DAY = re.compile(r"(?P<day>大后天|后天|明天|今天|今日|明天早上|明早)")
_ISO = re.compile(r"(?P<date>\d{4}-\d{1,2}-\d{1,2})[ T](?P<time>\d{1,2}:\d{2})(?::\d{2})?")

_UNIT_MINUTES = {
    "秒": 1 / 60,
    "分": 1,
    "小时": 60,
    "钟头": 60,
    "天": 1440,
    "日": 1440,
    "周": 10080,
    "星期": 10080,
}
_DIGITS = {
    "一": 1,
    "二": 2,
    "两": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
    "十": 10,
}

HELP = "听不懂这个时间。可以说「十分钟后」「明天 9 点半」「19:40」或「2026-10-03 08:00」。"


def chinese_number(token: str) -> float | None:
    """``十`` → 10, ``两`` → 2, ``半`` → 0.5, ``20`` → 20. ``None`` when it is not one."""
    text = token.strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        pass
    if text == "半":
        return 0.5
    if "十" in text:
        tens, _, ones = text.partition("十")
        head = _DIGITS.get(tens, 1) if tens else 1
        tail = _DIGITS.get(ones, 0) if ones else 0
        if tail and ones not in _DIGITS:
            return None
        return float(head * 10 + tail)
    if len(text) == 1 and text in _DIGITS:
        return float(_DIGITS[text])
    return None


def _unit_minutes(unit: str) -> float | None:
    cleaned = unit.replace("个", "").replace("钟头", "小时").rstrip("子")
    for key, minutes in _UNIT_MINUTES.items():
        if cleaned.startswith(key):
            return minutes
    return None


def parse_when(text: str, *, now: datetime.datetime | None = None) -> datetime.datetime | None:
    """Turn a Chinese time phrase into a moment, or ``None``.

    ``now`` is a parameter rather than a call to the clock because every rule here
    depends on it: "十分钟后" is a delta, "9 点半" is an absolute time that may have
    already gone, and a test that cannot pin the clock cannot assert either.
    """
    phrase = (text or "").strip()
    if not phrase:
        return None
    base = (now or datetime.datetime.now()).replace(microsecond=0)

    absolute = _ISO.search(phrase)
    if absolute:
        try:
            return datetime.datetime.fromisoformat(
                f"{absolute.group('date')}T{absolute.group('time')}"
            )
        except ValueError:
            return None

    relative = _RELATIVE.search(phrase)
    if relative:
        amount = chinese_number(relative.group("amount"))
        minutes = _unit_minutes(relative.group("unit"))
        if amount is not None and minutes:
            return base + datetime.timedelta(minutes=amount * minutes)

    clock = _CLOCK.search(phrase)
    if clock:
        hour = int(clock.group("hour"))
        raw = (clock.group("minute") or "0").strip()
        minute = 30 if raw == "半" else 15 if raw == "一刻" else int(raw or 0)
        day = base.date()
        named = _DAY.search(phrase)
        if named:
            offset = {
                "今天": 0,
                "今日": 0,
                "明天": 1,
                "明早": 1,
                "明天早上": 1,
                "后天": 2,
                "大后天": 3,
            }[named.group("day")]
            day = (base + datetime.timedelta(days=offset)).date()
        elif datetime.datetime.combine(day, datetime.time(hour, minute)) <= base:
            # A bare "9 点半" said at 11 is about tomorrow, not about an hour ago.
            day = (base + datetime.timedelta(days=1)).date()
        return datetime.datetime.combine(day, datetime.time(hour, minute))

    return None


class _Scheduler(Protocol):
    """The slice of the scheduler a reminder needs."""

    def add_job(self, spec: JobSpec) -> JobSpec: ...

    def remove_job(self, job_id: str) -> bool: ...

    def set_enabled(self, job_id: str, enabled: bool) -> bool: ...

    def list_jobs(self) -> list[JobSpec]: ...

    def next_run_time(self, job_id: str) -> str: ...


@dataclass(frozen=True, slots=True)
class Reminder:
    """One reminder, as the UI and the model are allowed to see it."""

    job_id: str
    text: str
    when: str
    enabled: bool
    next_run: str
    created_at: str

    def to_dict(self) -> dict[str, object]:
        return {
            "job_id": self.job_id,
            "text": self.text,
            "when": self.when,
            "enabled": self.enabled,
            "next_run": self.next_run,
            "created_at": self.created_at,
        }


class ReminderService:
    """Create, list and cancel reminders on top of the scheduler."""

    def __init__(
        self,
        scheduler_provider: Callable[[], _Scheduler],
        *,
        clock: Callable[[], datetime.datetime] | None = None,
    ) -> None:
        # A provider, not the service. The scheduler is built from the tool action
        # runner, which needs the tool service, which is where reminders get
        # advertised as tools -- so no wiring order exists without a cycle in it, and
        # the cycle is broken here instead. This is the same lazy ``*_provider``
        # idiom the memory, knowledge and chat services already use.
        self._scheduler = scheduler_provider
        self._clock = clock or datetime.datetime.now

    def add(
        self, text: str, when_text: str = "", *, when: datetime.datetime | None = None
    ) -> Reminder:
        """Set a reminder.

        Raises:
            JarvisError: if the text is empty, or the time cannot be understood, or
                it names a moment that has already gone. Each message says what to
                do instead -- a refusal that does not name the accepted shapes is
                just a dead end with a status code.
        """
        body = " ".join(str(text or "").split())
        if not body:
            raise JarvisError("提醒内容不能为空")
        body = body[:MAX_TEXT_CHARS]
        moment = when or parse_when(when_text, now=self._clock())
        if moment is None:
            raise JarvisError(HELP, details={"when": when_text})
        if moment <= self._clock():
            raise JarvisError(
                f"那个时间已经过了（{moment:%Y-%m-%d %H:%M}）", details={"when": str(moment)}
            )
        spec = JobSpec(
            job_id=f"{JOB_PREFIX}{uuid.uuid4().hex[:8]}",
            name=body[:40],
            action=SPEAK_ACTION,
            arguments={"text": f"提醒：{body}", "title": "提醒"},
            trigger=TriggerKind.DATE,
            expression=moment.isoformat(timespec="minutes"),
        )
        stored = self._scheduler().add_job(spec)
        logger.info("reminder set: %s at %s", body[:40], stored.expression)
        return self._to_reminder(stored)

    def all_reminders(self) -> list[Reminder]:
        """Every reminder, soonest first. Never raises."""
        rows: list[Reminder] = []
        for spec in self._safe_jobs():
            if spec.job_id.startswith(JOB_PREFIX):
                rows.append(self._to_reminder(spec))
        rows.sort(key=lambda item: (not item.enabled, item.when))
        return rows

    def upcoming(self, limit: int = 3) -> list[Reminder]:
        """The ones still waiting -- what the HUD shows without a click."""
        return [row for row in self.all_reminders() if row.enabled][:limit]

    def cancel(self, key: str) -> str:
        """Delete by job id or by a phrase from the text. Returns what was removed.

        Matching on text is deliberately substring-only and refuses when two
        reminders match: "取消喝水" against three rows that all mention 喝水 would
        otherwise delete an arbitrary one, and an assistant that guesses which
        thing it destroyed is not worth trusting with the other two.
        """
        needle = (key or "").strip()
        if not needle:
            raise JarvisError("要说取消哪一条")
        rows = self.all_reminders()
        exact = [
            row for row in rows if row.job_id == needle or row.job_id == f"{JOB_PREFIX}{needle}"
        ]
        if exact:
            target = exact[0]
        else:
            hits = [row for row in rows if needle.lower() in row.text.lower()]
            if not hits:
                raise JarvisError(f"没找到和「{needle}」匹配的提醒", details={"count": len(rows)})
            if len(hits) > 1:
                listed = "、".join(f"{row.text}（{row.when}）" for row in hits[:4])
                raise JarvisError(f"有 {len(hits)} 条都像「{needle}」：{listed}。说得更具体一点")
            target = hits[0]
        self._scheduler().remove_job(target.job_id)
        logger.info("reminder cancelled: %s", target.text[:40])
        return f"{target.text}（原定 {target.when}）"

    def toggle(self, job_id: str, enabled: bool) -> bool:
        """Pause or resume one reminder without losing it."""
        return self._scheduler().set_enabled(job_id, bool(enabled))

    def stats(self) -> dict[str, object]:
        """Counters for the panel. Never raises."""
        rows = self.all_reminders()
        return {
            "total": len(rows),
            "waiting": sum(1 for row in rows if row.enabled),
            "done": sum(1 for row in rows if not row.enabled),
            "next": rows[0].when if rows else "",
        }

    def _safe_jobs(self) -> list[JobSpec]:
        try:
            return self._scheduler().list_jobs()
        except Exception:  # pragma: no cover - the scheduler already guards this
            logger.exception("reading jobs for the reminder list failed")
            return []

    def _to_reminder(self, spec: JobSpec) -> Reminder:
        text = str(spec.arguments.get("text", spec.name))
        return Reminder(
            job_id=spec.job_id,
            text=text.removeprefix("提醒："),
            when=spec.expression.replace("T", " "),
            enabled=spec.enabled,
            next_run=self._scheduler().next_run_time(spec.job_id) if spec.enabled else "",
            created_at=spec.created_at,
        )


__all__ = ["HELP", "Reminder", "ReminderService", "chinese_number", "parse_when"]
