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

ALL_TIME: int = 0
"""``days=0``: the whole ledger, from the first recorded call to now.

Not a fourth window size but the absence of one, which is why :data:`MAX_WINDOW_DAYS`
does not apply to it. The ceiling exists because an unbounded **per-day series** turns a
month of history into a full-table group-by nobody asked for; a single total over the
same table is a different shape of query -- one pass, one row back, and the row count is
one per answered call, so a year of heavy use is still thousands of rows. What is *not*
answered here is a 365-bar chart: see :meth:`UsageService.daily`.
"""

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
    Migration(
        namespace=NAMESPACE,
        version=2,
        statements=(
            # Which turn or round-table task spent these tokens. A window grouping can
            # answer "how much this month"; it cannot answer "what did *that* job cost",
            # which is the question a task answered by three models over four rounds
            # starts asking the first time somebody looks at the bill.
            """
            ALTER TABLE llm_usage ADD COLUMN task_id TEXT NOT NULL DEFAULT ''
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_llm_usage_task
                ON llm_usage (task_id)
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
    def all_time(self) -> bool:
        """Whether this is the whole ledger rather than a window into it.

        Carried into the payload because ``days = 0`` cannot be read as "zero days" by
        anything that renders a label, and the page should not have to know the sentinel.
        """
        return self.days == ALL_TIME

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
            "all_time": self.all_time,
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
            " completion_tokens, cached_tokens, latency_ms, task_id)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                at,
                event.provider,
                event.model,
                event.prompt_tokens,
                event.completion_tokens,
                event.cached_tokens,
                event.latency_ms,
                event.task_id,
            ),
        )

    def by_task(self, task_id: str) -> list[Row]:
        return self.query(
            "SELECT provider, model, COUNT(*) AS calls,"
            " COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens,"
            " COALESCE(SUM(completion_tokens), 0) AS completion_tokens,"
            " COALESCE(SUM(cached_tokens), 0) AS cached_tokens,"
            " COUNT(cached_tokens) AS calls_with_cache_data,"
            " COALESCE(SUM(latency_ms), 0.0) AS latency_ms"
            " FROM llm_usage WHERE task_id = ?"
            " GROUP BY provider, model ORDER BY prompt_tokens + completion_tokens DESC",
            (task_id,),
        )

    def _window(self, since: str | None, until: str) -> tuple[str, tuple[str, ...]]:
        """The ``WHERE`` clause and its parameters, or neither when ``since`` is None.

        One SELECT list for both the windowed and the all-time reading, on purpose: two
        copies is how a total and its own breakdown stop adding up to each other.
        """
        if since is None:
            return "", ()
        return " WHERE at >= ? AND at <= ?", (since, until)

    def span(self) -> Row:
        """The first and last recorded moment, for labelling an all-time window.

        Read from the ledger rather than assumed, because "从始至终" that starts at the
        install date would be a range nobody can check: a ledger carried over from an older
        database starts earlier than the app did.
        """
        row = self.query_one(
            "SELECT MIN(at) AS first_at, MAX(at) AS last_at, COUNT(*) AS rows FROM llm_usage"
        )
        return row if row is not None else {}

    def aggregate(self, since: str | None, until: str) -> Row:
        where, params = self._window(since, until)
        row = self.query_one(
            "SELECT COUNT(*) AS calls,"
            " COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens,"
            " COALESCE(SUM(completion_tokens), 0) AS completion_tokens,"
            " COALESCE(SUM(cached_tokens), 0) AS cached_tokens,"
            " COUNT(cached_tokens) AS calls_with_cache_data,"
            " COALESCE(AVG(latency_ms), 0.0) AS avg_latency_ms"
            f" FROM llm_usage{where}",
            params,
        )
        return row if row is not None else {}

    def per_provider(self, since: str | None, until: str) -> list[Row]:
        where, params = self._window(since, until)
        return self.query(
            "SELECT provider, model, COUNT(*) AS calls,"
            " COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens,"
            " COALESCE(SUM(completion_tokens), 0) AS completion_tokens,"
            " COALESCE(SUM(cached_tokens), 0) AS cached_tokens,"
            " COUNT(cached_tokens) AS calls_with_cache_data,"
            " COALESCE(AVG(latency_ms), 0.0) AS avg_latency_ms"
            f" FROM llm_usage{where}"
            " GROUP BY provider, model ORDER BY prompt_tokens + completion_tokens DESC",
            params,
        )

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
        """Totals for the last ``days`` days, or the whole ledger when ``days`` is 0."""
        window = _clamped_window(days)
        since, until = self._bounds(window)
        row: Row = self._repo.aggregate(since, until)
        return UsageSummary(
            days=window,
            since=since or until,
            until=until,
            calls=as_int(row.get("calls")),
            prompt_tokens=as_int(row.get("prompt_tokens")),
            completion_tokens=as_int(row.get("completion_tokens")),
            cached_tokens=as_int(row.get("cached_tokens")),
            calls_with_cache_data=as_int(row.get("calls_with_cache_data")),
            avg_latency_ms=as_float(row.get("avg_latency_ms")),
        )

    def cost_of(self, task_id: str) -> list[dict[str, object]]:
        """What one turn or round-table task cost, per model that took part.

        Grouped rather than totalled because the point of asking three models is that
        they are not the same price: a single number would hide which of them the task
        actually spent itself on, which is the first thing anybody asks afterwards.

        A task that never asked anything returns an empty list rather than a row of
        zeros -- the same rule the window totals follow, so "no reading" and "no cost"
        stay two different statements.
        """
        rows: list[dict[str, object]] = []
        for raw in self._repo.by_task(str(task_id)):
            prompt = as_int(raw.get("prompt_tokens"))
            completion = as_int(raw.get("completion_tokens"))
            rows.append(
                {
                    "provider": as_str(raw.get("provider")),
                    "model": as_str(raw.get("model")),
                    "calls": as_int(raw.get("calls")),
                    "prompt_tokens": prompt,
                    "completion_tokens": completion,
                    "total_tokens": prompt + completion,
                    "cached_tokens": as_int(raw.get("cached_tokens")),
                    "latency_ms": round(as_float(raw.get("latency_ms")), 1),
                }
            )
        return rows

    def by_provider(self, days: int = DEFAULT_WINDOW_DAYS) -> list[dict[str, object]]:
        """Per provider/model row for the window, biggest spender first.

        Shares :meth:`_bounds` with :meth:`summary`, so the rows add up to the total the
        same call reports. Two windows that drift by a second is how a breakdown stops
        being a breakdown -- and it matters most for the all-time reading, where the chart
        is a pie and a slice that does not belong to the same total is visibly wrong.
        """
        window = _clamped_window(days)
        since, until = self._bounds(window)
        rows: list[dict[str, object]] = []
        for raw in self._repo.per_provider(since, until):
            prompt = as_int(raw.get("prompt_tokens"))
            completion = as_int(raw.get("completion_tokens"))
            cached = as_int(raw.get("cached_tokens"))
            cache_reported = as_int(raw.get("calls_with_cache_data")) > 0
            rows.append(
                {
                    "provider": as_str(raw.get("provider")),
                    "model": as_str(raw.get("model")),
                    "calls": as_int(raw.get("calls")),
                    "prompt_tokens": prompt,
                    "completion_tokens": completion,
                    "total_tokens": prompt + completion,
                    "cached_tokens": cached,
                    "cache_hit_percent": (
                        100.0 * min(cached, prompt) / prompt
                        if cache_reported and prompt > 0
                        else None
                    ),
                    "avg_latency_ms": round(as_float(raw.get("avg_latency_ms")), 1),
                }
            )
        return rows

    def _bounds(self, window: int) -> tuple[str | None, str]:
        """``(since, until)`` for a clamped window; ``since`` is None for the whole ledger.

        An empty ledger gets ``since = None`` too, which reads as "no rows" from the same
        code path a full one does -- and it is why the all-time label has to come from the
        data rather than from a date the app guessed.
        """
        now = format_timestamp(utc_now())
        if window != ALL_TIME:
            since, until = _window_bounds(window)
            return since, until
        first_at = as_str(self._repo.span().get("first_at"))
        return (first_at or None), now

    def ledger_span(self) -> dict[str, object]:
        """When this ledger starts, how long it covers, and how many rows are in it.

        The all-time panel needs the first recorded call to label its range, and a total
        with no stated beginning is exactly the kind of number that gets argued with.
        """
        row = self._repo.span()
        first_at = as_str(row.get("first_at"))
        last_at = as_str(row.get("last_at"))
        return {
            "first_at": first_at,
            "last_at": last_at,
            "rows": as_int(row.get("rows")),
            "days": _days_between(first_at, last_at),
        }

    def daily(self, days: int = DEFAULT_WINDOW_DAYS) -> list[dict[str, object]]:
        """Per-day rows for the chart, in chronological order.

        Days with no calls are simply absent. The chart must not draw them as
        zero, because "nothing used that day" and "nothing recorded" look the
        same in a line and mean different things to the person reading it.

        The all-time window answers with an empty list rather than a 400-bar chart: one bar
        per day past a few months stops being a picture, and the scan it costs is the exact
        shape :data:`MAX_WINDOW_DAYS` was put there to prevent. Totals and the per-model
        breakdown still cover everything -- it is only the daily series that stays bounded.
        """
        window = _clamped_window(days)
        if window == ALL_TIME:
            return []
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
    """Sanitise a requested window: ``0`` means the whole ledger, otherwise 1..a month.

    ``0`` is honoured rather than clamped up to 1 because it is a different question, not a
    too-small answer to the old one. Anything unparseable falls back to the default window,
    and a negative number is treated as a mistake in that direction rather than as 0 --
    reading the whole table should take someone asking for it out loud.
    """
    try:
        value = int(days)
    except (TypeError, ValueError):
        return DEFAULT_WINDOW_DAYS
    if value == ALL_TIME:
        return ALL_TIME
    return max(1, min(value, MAX_WINDOW_DAYS))


def _days_between(since: str, until: str) -> int:
    """How many days the ledger spans, from its own two ends. 0 when either is missing."""
    try:
        start = datetime.fromisoformat(since.replace("Z", "+00:00"))
        end = datetime.fromisoformat(until.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return 0
    return max(0, (end - start).days + 1) if end >= start else 0


def _window_bounds(days: int) -> tuple[str, str]:
    """``(since, until)`` as canonical timestamp text, ``since`` at local midnight."""
    now = utc_now()
    start = now - timedelta(days=max(1, days) - 1)
    midnight = datetime(start.year, start.month, start.day, tzinfo=UTC)
    return format_timestamp(midnight), format_timestamp(now)
