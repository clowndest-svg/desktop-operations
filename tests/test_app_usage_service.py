"""Tests for :class:`jarvis.app.usage_service.UsageService`.

The store is a real in-memory SQLite one: the thing worth breaking here is the SQL
and the nullability, and a fake database would test neither.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from jarvis.app.usage_service import (
    ALL_TIME,
    MAX_WINDOW_DAYS,
    UsageService,
    _clamped_window,
)
from jarvis.core.events import UsageEvent
from jarvis.database import SqliteStore, format_timestamp, utc_now


def _store() -> SqliteStore:
    store = SqliteStore(Path(":memory:"), journal_mode="MEMORY")
    store.start()
    return store


def _service() -> UsageService:
    """A started service: the composition root migrates before anything records."""
    service = UsageService(_store())
    service.start()
    return service


def _event(
    prompt: int = 100,
    completion: int = 20,
    cached: int | None = None,
    latency_ms: float = 500.0,
) -> UsageEvent:
    return UsageEvent(
        provider="test",
        model="m",
        prompt_tokens=prompt,
        completion_tokens=completion,
        cached_tokens=cached,
        latency_ms=latency_ms,
    )


class TestSummary:
    def test_an_empty_ledger_has_no_cache_percentage_rather_than_zero(self) -> None:
        """The distinction the owner will be shown: 无读数 is not 0%."""
        summary = _service().summary()

        assert summary.calls == 0
        assert summary.total_tokens == 0
        assert summary.cache_hit_percent is None
        assert summary.cache_data_reported is False

    def test_totals_add_up(self) -> None:
        service = _service()
        service.record(_event(prompt=100, completion=20))
        service.record(_event(prompt=300, completion=50))

        summary = service.summary()

        assert summary.calls == 2
        assert summary.prompt_tokens == 400
        assert summary.completion_tokens == 70
        assert summary.total_tokens == 470

    def test_a_provider_that_never_reports_cache_keeps_the_percentage_unknown(
        self,
    ) -> None:
        service = _service()
        service.record(_event(prompt=1000, cached=None))
        service.record(_event(prompt=1000, cached=None))

        summary = service.summary()

        assert summary.prompt_tokens == 2000
        assert summary.cache_hit_percent is None, (
            "0% would claim the cache never helped, which is a different statement "
            "from 'the provider never told us'"
        )

    def test_genuine_zero_cache_is_reported_as_zero(self) -> None:
        service = _service()
        service.record(_event(prompt=200, cached=0))

        summary = service.summary()

        assert summary.cache_hit_percent == 0.0
        assert summary.cache_data_reported is True

    def test_percentage_uses_only_the_rows_that_reported(self) -> None:
        service = _service()
        service.record(_event(prompt=100, cached=50))
        service.record(_event(prompt=100, cached=None))

        summary = service.summary()

        assert summary.calls_with_cache_data == 1
        assert summary.calls_without_cache_data == 1
        # 50 cached out of the 200 prompt tokens actually spent, not out of the
        # 100 that reported -- the denominator is the whole window's spend.
        assert summary.cache_hit_percent == pytest.approx(25.0)

    def test_cached_tokens_never_produce_a_percentage_above_one_hundred(
        self,
    ) -> None:
        """A provider that counts cached prompt tokens separately can overshoot."""
        service = _service()
        service.record(_event(prompt=100, cached=180))

        assert service.summary().cache_hit_percent == pytest.approx(100.0)


class TestWindow:
    def test_the_range_is_capped_at_one_month(self) -> None:
        assert _clamped_window(365) == MAX_WINDOW_DAYS

    def test_zero_means_the_whole_ledger_now_and_nonsense_still_falls_back(self) -> None:
        """A deliberate contract change, 2026-10-04.

        This test used to assert ``_clamped_window(0) == 1`` with the comment "fall back
        rather than scanning everything". ``0`` now means the whole ledger, because the
        owner asked for a from-the-beginning total and a month-capped panel cannot show one.

        What the old assertion was protecting is still protected, just one layer over:
        the unbounded *per-day series* scan is the expensive shape, and
        :meth:`UsageService.daily` still answers :data:`ALL_TIME` with an empty list.
        A total is one aggregate pass over one row per answered call.
        """
        assert _clamped_window(0) == ALL_TIME
        assert _clamped_window(-5) == 1, "负数是写错了，不是要看全部"
        assert _clamped_window("abc") == 7  # type: ignore[arg-type]

    def test_a_request_inside_the_cap_is_honoured(self) -> None:
        assert _clamped_window(14) == 14

    def test_events_outside_the_window_are_excluded(self) -> None:
        """A row outside the range must not be swept in by an open-ended WHERE."""
        service = _service()
        service.record(_event(prompt=100))
        far_future = format_timestamp(utc_now() + timedelta(days=400))
        service._repo.insert_event(far_future, _event(prompt=999))

        summary = service.summary()

        assert summary.calls == 1
        assert summary.prompt_tokens == 100


class TestDaily:
    def test_days_are_returned_with_a_percent_or_none(self) -> None:
        service = _service()
        service.record(_event(prompt=100, cached=25))

        rows = service.daily()

        assert len(rows) == 1
        assert rows[0]["calls"] == 1
        assert rows[0]["cache_hit_percent"] == pytest.approx(25.0)

    def test_a_silent_provider_yields_none_not_zero_per_day(self) -> None:
        service = _service()
        service.record(_event(prompt=100, cached=None))

        assert service.daily()[0]["cache_hit_percent"] is None


class TestRecordIsSafe:
    def test_a_broken_ledger_does_not_lose_the_answer(self) -> None:
        """The model call already succeeded; the bookkeeping must not veto it."""
        service = _service()
        service.start()
        service.stop()
        service._store.stop()

        service.record(_event())  # must not raise

    def test_start_is_idempotent(self) -> None:
        service = _service()
        service.start()
        service.start()

        service.record(_event())
        assert service.summary().calls == 1


def _whole(value: object) -> int:
    """A number out of a ``dict[str, object]`` row. 0 is not an option, so neither is one."""
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _backdated(
    service: UsageService,
    days_ago: int,
    *,
    prompt: int = 100,
    completion: int = 20,
    cached: int | None = None,
) -> None:
    """File one call at a stated age, straight through the repository.

    ``record`` always stamps "now", so without this there is no ledger that is older than
    today -- and "从始至终" versus "最近一个月" would be the same query, which is exactly
    the distinction these tests are about.
    """
    when = utc_now() - timedelta(days=days_ago)
    service._repo.insert_event(
        format_timestamp(when),
        _event(prompt=prompt, completion=completion, cached=cached),
    )


class TestTheWholeLedger:
    """``days=0``: the entire ledger, and a per-model breakdown that adds up to it."""

    def test_an_old_row_shows_up_only_in_the_all_time_reading(self) -> None:
        service = _service()
        service.record(_event(prompt=100, completion=20))
        _backdated(service, 90, prompt=900, completion=80)

        week = service.summary(7)
        whole = service.summary(ALL_TIME)

        assert week.calls == 1
        assert whole.calls == 2
        assert whole.prompt_tokens == 1000
        assert whole.all_time is True and week.all_time is False
        assert whole.days == ALL_TIME

    def test_the_slices_add_up_to_the_total(self) -> None:
        """父 = Σ子，同一个窗口里问出来的两半必须对得上。

        A pie whose slices do not sum to the number printed above it is not a decoration
        problem -- it means two queries used two different windows.
        """
        service = _service()
        for prompt, completion in ((100, 20), (300, 40), (50, 5)):
            service.record(
                UsageEvent(
                    provider="a",
                    model="m1",
                    prompt_tokens=prompt,
                    completion_tokens=completion,
                    latency_ms=10.0,
                )
            )
        service.record(
            UsageEvent(
                provider="b", model="m2", prompt_tokens=700, completion_tokens=90, latency_ms=20.0
            )
        )
        _backdated(service, 60, prompt=1, completion=1)

        total = service.summary(ALL_TIME)
        rows = service.by_provider(ALL_TIME)

        assert sum(_whole(row["prompt_tokens"]) for row in rows) == total.prompt_tokens
        assert sum(_whole(row["completion_tokens"]) for row in rows) == total.completion_tokens
        assert sum(_whole(row["calls"]) for row in rows) == total.calls
        assert [row["model"] for row in rows][:2] == ["m2", "m1"], "最大的那块排最前"

    def test_the_daily_series_stays_bounded_when_the_total_is_not(self) -> None:
        """The whole ledger answers for totals and slices, but not for a 400-bar chart.

        ``MAX_WINDOW_DAYS`` exists to stop an unbounded *series* scan; a single aggregate
        pass over one row per call is a different shape of query, and this pins which half
        of the panel got the exception.
        """
        service = _service()
        service.record(_event())
        _backdated(service, 200, prompt=5, completion=1)

        assert service.summary(ALL_TIME).calls == 2
        assert service.daily(7) and len(service.daily(7)) == 1
        assert service.daily(ALL_TIME) == []

    def test_the_range_is_labelled_from_the_ledger_not_from_today(self) -> None:
        service = _service()
        _backdated(service, 120, prompt=5, completion=1)
        service.record(_event(prompt=7, completion=2))

        span = service.ledger_span()
        summary = service.summary(ALL_TIME)

        assert span["rows"] == 2
        first, last = str(span["first_at"]), str(span["last_at"])
        assert first and first <= last
        assert 119 <= _whole(span["days"]) <= 121
        assert summary.since == first, "标题上写的起点必须就是账本里最早那一条"

    def test_an_empty_ledger_is_zeros_and_no_range(self) -> None:
        """Nothing recorded is not the same as 0% of something."""
        service = _service()
        summary = service.summary(ALL_TIME)
        assert summary.calls == 0 and summary.total_tokens == 0
        assert summary.cache_hit_percent is None
        assert summary.since and not summary.since.startswith("1970")
        assert service.by_provider(ALL_TIME) == []
        assert service.ledger_span()["rows"] == 0

    def test_cache_reading_survives_across_the_whole_ledger(self) -> None:
        """A silent provider stays silent all-time, and a reporting one is averaged in."""
        service = _service()
        service.record(_event(prompt=100, completion=5, cached=40))
        _backdated(service, 400, prompt=100, completion=5, cached=None)

        whole = service.summary(ALL_TIME)
        assert whole.calls_with_cache_data == 1
        assert whole.calls_without_cache_data == 1
        assert whole.cache_hit_percent == pytest.approx(20.0)

    def test_a_negative_window_is_a_mistake_not_a_request_for_everything(self) -> None:
        """Reading the whole table has to be asked for out loud."""
        assert _clamped_window(-1) == 1
        assert _clamped_window(0) == ALL_TIME
        assert _clamped_window("很多") == 7  # type: ignore[arg-type]
