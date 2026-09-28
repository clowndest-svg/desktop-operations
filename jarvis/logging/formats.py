"""Log record layout and structured metric fields.

JARVIS needs per-record accounting data (spec chapter 15): request latency,
token usage and cost. Rather than inventing a parallel logging API, callers
attach these as standard ``extra=`` fields via :func:`metrics`, and
:class:`StructuredFormatter` renders them as a stable ``key=value`` suffix
that is trivially machine-parseable::

    ... | jarvis.llm | reply ok | latency_ms=843.2 tokens_in=512 tokens_out=128 cost_usd=0.000940

Fields are optional and independent; records without metrics render exactly
like plain ones.
"""

from __future__ import annotations

import logging
from typing import Final

LOG_FORMAT: Final[str] = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
"""Base layout shared by the console and file handlers."""

LOG_DATE_FORMAT: Final[str] = "%Y-%m-%d %H:%M:%S"
"""Timestamp layout (second precision; ``%(msecs)`` noise is not needed)."""

METRIC_LATENCY_MS: Final[str] = "latency_ms"
METRIC_TOKENS_IN: Final[str] = "tokens_in"
METRIC_TOKENS_OUT: Final[str] = "tokens_out"
METRIC_COST_USD: Final[str] = "cost_usd"

_METRIC_ORDER: Final[tuple[str, ...]] = (
    METRIC_LATENCY_MS,
    METRIC_TOKENS_IN,
    METRIC_TOKENS_OUT,
    METRIC_COST_USD,
)


def metrics(
    *,
    latency_ms: float | None = None,
    tokens_in: int | None = None,
    tokens_out: int | None = None,
    cost_usd: float | None = None,
) -> dict[str, float | int]:
    """Build an ``extra=`` mapping carrying accounting fields.

    Example:
        >>> logger.info("reply ok", extra=metrics(latency_ms=843.2, tokens_in=512))

    Only the fields actually provided are attached; ``None`` means absent.
    """
    fields: dict[str, float | int] = {}
    if latency_ms is not None:
        fields[METRIC_LATENCY_MS] = latency_ms
    if tokens_in is not None:
        fields[METRIC_TOKENS_IN] = tokens_in
    if tokens_out is not None:
        fields[METRIC_TOKENS_OUT] = tokens_out
    if cost_usd is not None:
        fields[METRIC_COST_USD] = cost_usd
    return fields


def _render_metric(name: str, value: object) -> str:
    if name == METRIC_LATENCY_MS and isinstance(value, int | float):
        return f"{name}={float(value):.1f}"
    if name == METRIC_COST_USD and isinstance(value, int | float):
        return f"{name}={float(value):.6f}"
    return f"{name}={value}"


class StructuredFormatter(logging.Formatter):
    """Standard layout plus an optional ``key=value`` metric suffix."""

    def __init__(self) -> None:
        super().__init__(fmt=LOG_FORMAT, datefmt=LOG_DATE_FORMAT)

    def format(self, record: logging.LogRecord) -> str:
        base = super().format(record)
        suffix = " ".join(
            _render_metric(name, getattr(record, name))
            for name in _METRIC_ORDER
            if getattr(record, name, None) is not None
        )
        return f"{base} | {suffix}" if suffix else base
