"""Tests for :class:`jarvis.app.usage_service.UsageService`.

The store is a real in-memory SQLite one: the thing worth breaking here is the SQL
and the nullability, and a fake database would test neither.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from jarvis.app.usage_service import MAX_WINDOW_DAYS, UsageService, _clamped_window
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

    def test_zero_and_nonsense_fall_back_rather_than_scanning_everything(self) -> None:
        assert _clamped_window(0) == 1
        assert _clamped_window(-5) == 1
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
