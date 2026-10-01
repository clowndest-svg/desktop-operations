"""Usage service: the token ledger behind the HUD's statistics popup.

Why this is its own component
-----------------------------
The numbers have to come from somewhere that survives a restart, and the answer
must be honest about what it does not know. Two providers were checked on this
machine before this file existed: neither reports cached tokens at all, so a
「命中率」 field had exactly two acceptable states -- a real percentage, or
「无读数」. That distinction is the reason the column is nullable and the summary
carries ``cache_data_reported`` rather than defaulting to zero.

Layering
--------
``llm`` produces :class:`~jarvis.core.events.UsageEvent` and may not import
``database``, so the composition root hands the client this service's
:meth:`record` as a sink. Everything the page sees goes through :meth:`summary`
and :meth:`daily`, which is also where the one-month window is enforced -- a query
API without that ceiling is how a "last 30 days" chart turns into a full-table scan
on somebody's year of history.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from jarvis.core.events import UsageEvent
from jarvis.database import (
    Migration,
    Repository,
    Row,
    SqliteStore,
    as_float,
    as_int,
    as_str,
    format_timestamp,
    utc_now,
)

logger = logging.getLogger("jarvis.app.usage_service")

NAMESPACE: str = "usage"

MAX_WINDOW_DAYS: int = 31
"""The longest range the ledger will aggregate. The owner asked for "最多查一个月"."""

DEFAULT_WINDOW_DAYS: int = 7

MIGRATIONS: tuple[Migration, ...] = (
    Migration(
        namespace=NAMESPACE,
        version=1,
        statements=(
            """
            CREATE TABLE IF NOT EXISTS llm_usage (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                at                TEXT    NOT NULL,
                provider          TEXT    NOT NULL,
                model             TEXT    NOT NULL,
                prompt_tokens     INTEGER NOT NULL,
                completion_tokens INTEGER NOT NULL,
                cached_tokens     INTEGER,
                latency_ms        REAL    NOT NULL DEFAULT 0
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_llm_usage_at
                ON llm_usage (at DESC)
            """,
        ),
    ),
)


@dataclass(frozen=True, slots=True)
class UsageSummary:
    """Aggregated token accounting for one window.

    ``cache_hit_percent`` is ``None`` unless at least one answered request in the
    window actually reported cached tokens. ``calls_without_cache_data`` exists so
    the screen can say *why* there is no percentage instead of showing a bare dash
    and leaving the owner guessing whether the feature is broken or the provider
    is silent.
    """

    days: int
    since: str
    until: str
    calls: int
    prompt_tokens: int
    completion_tokens: int
    cached_tokens: int
    calls_with_cache_data: int
    avg_latency_ms: float

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    @property
    def cache_hit_percent(self) -> float | None:
        if self.calls_with_cache_data == 0 or self.prompt_tokens <= 0:
            return None
        return 100.0 * min(self.cached_tokens, self.prompt_tokens) / self.prompt_tokens

    @property
    def cache_data_reported(self) -> bool:
        return self.calls_with_cache_data > 0

    @property
    def calls_without_cache_data(self) -> int:
        """How many calls in the window said nothing about caching.

        The screen needs this to explain a missing percentage; without it a dash is
        indistinguishable from a provider that genuinely never hit the cache.
        """
        return self.calls - self.calls_with_cache_data

    def to_dict(self) -> dict[str, object]:
        return {
            "days": self.days,
            "since": self.since,
            "until": self.until,
            "calls": self.calls,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "cached_tokens": self.cached_tokens,
            "cache_hit_percent": self.cache_hit_percent,
            "cache_data_reported": self.cache_data_reported,
            "calls_without_cache_data": self.calls_without_cache_data,
            "avg_latency_ms": round(self.avg_latency_ms, 1),
        }


class _UsageRepository(Repository):
    """Reads and writes for ``llm_usage``."""

    def insert_event(self, at: str, event: UsageEvent) -> int:
        return self.insert(
            "INSERT INTO llm_usage (at, provider, model, prompt_tokens,"
            " completion_tokens, cached_tokens, latency_ms)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                at,
                event.provider,
                event.model,
                event.prompt_tokens,
                event.completion_tokens,
                event.cached_tokens,
                event.latency_ms,
            ),
        )

    def aggregate(self, since: str, until: str) -> Row:
        row = self.query_one(
            "SELECT COUNT(*) AS calls,"
            " COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens,"
            " COALESCE(SUM(completion_tokens), 0) AS completion_tokens,"
            " COALESCE(SUM(cached_tokens), 0) AS cached_tokens,"
            " COUNT(cached_tokens) AS calls_with_cache_data,"
            " COALESCE(AVG(latency_ms), 0.0) AS avg_latency_ms"
            " FROM llm_usage WHERE at >= ? AND at <= ?",
            (since, until),
        )
        return row if row is not None else {}

    def per_day(self, since: str, until: str) -> list[Row]:
        return self.query(
            "SELECT substr(at, 1, 10) AS day, COUNT(*) AS calls,"
            " COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens,"
            " COALESCE(SUM(completion_tokens), 0) AS completion_tokens,"
            " COALESCE(SUM(cached_tokens), 0) AS cached_tokens,"
            " COUNT(cached_tokens) AS calls_with_cache_data"
            " FROM llm_usage WHERE at >= ? AND at <= ?"
            " GROUP BY day ORDER BY day",
            (since, until),
        )


class UsageService:
    """The ledger: records answered model calls and aggregates them over a window."""

    name = "usage"

    def __init__(self, store: SqliteStore) -> None:
        self._store = store
        self._repo = _UsageRepository(store)
        self._started = False

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        """Apply the schema. Idempotent, offline, no model load."""
        if self._started:
            return
        self._store.migrate(NAMESPACE, MIGRATIONS)
        self._started = True
        logger.info("usage service ready")

    def stop(self) -> None:
        self._started = False

    # -- write path --------------------------------------------------------

    def record(self, event: UsageEvent) -> None:
        """Append one answered call. This is the sink handed to the LLM client.

        Never raises. The call it is accounting for already succeeded, and a
        ledger that is briefly unavailable must not turn a good answer into an
        error on the user's screen -- the miss goes to the log instead.
        """
        try:
            self._repo.insert_event(format_timestamp(utc_now()), event)
        except Exception:
            logger.exception("could not record token usage")

    @property
    def sink(self) -> Callable[[UsageEvent], None] | None:
        """The bound recorder, for the composition root to inject."""
        return self.record

    # -- read path ---------------------------------------------------------

    def summary(self, days: int = DEFAULT_WINDOW_DAYS) -> UsageSummary:
        """Totals for the last ``days`` days, clamped to :data:`MAX_WINDOW_DAYS`."""
        window = _clamped_window(days)
        since, until = _window_bounds(window)
        row: Row = self._repo.aggregate(since, until)
        return UsageSummary(
            days=window,
            since=since,
            until=until,
            calls=as_int(row.get("calls")),
            prompt_tokens=as_int(row.get("prompt_tokens")),
            completion_tokens=as_int(row.get("completion_tokens")),
            cached_tokens=as_int(row.get("cached_tokens")),
            calls_with_cache_data=as_int(row.get("calls_with_cache_data")),
            avg_latency_ms=as_float(row.get("avg_latency_ms")),
        )

    def daily(self, days: int = DEFAULT_WINDOW_DAYS) -> list[dict[str, object]]:
        """Per-day rows for the chart, in chronological order.

        Days with no calls are simply absent. The chart must not draw them as
        zero, because "nothing used that day" and "nothing recorded" look the
        same in a line and mean different things to the person reading it.
        """
        window = _clamped_window(days)
        since, until = _window_bounds(window)
        rows = []
        for raw in self._repo.per_day(since, until):
            cached_reported = as_int(raw.get("calls_with_cache_data")) > 0
            prompt = as_int(raw.get("prompt_tokens"))
            cached = as_int(raw.get("cached_tokens"))
            rows.append(
                {
                    "day": as_str(raw.get("day")),
                    "calls": as_int(raw.get("calls")),
                    "prompt_tokens": prompt,
                    "completion_tokens": as_int(raw.get("completion_tokens")),
                    "cached_tokens": cached,
                    "cache_hit_percent": (
                        100.0 * min(cached, prompt) / prompt
                        if cached_reported and prompt > 0
                        else None
                    ),
                }
            )
        return rows


def _clamped_window(days: int) -> int:
    """Sanitise a requested window: at least one day, never more than a month."""
    try:
        value = int(days)
    except (TypeError, ValueError):
        return DEFAULT_WINDOW_DAYS
    return max(1, min(value, MAX_WINDOW_DAYS))


def _window_bounds(days: int) -> tuple[str, str]:
    """``(since, until)`` as canonical timestamp text, ``since`` at local midnight."""
    now = utc_now()
    start = now - timedelta(days=max(1, days) - 1)
    midnight = datetime(start.year, start.month, start.day, tzinfo=UTC)
    return format_timestamp(midnight), format_timestamp(now)
