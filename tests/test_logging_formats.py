"""Unit tests for jarvis.logging.formats."""

from __future__ import annotations

import logging

from jarvis.logging import StructuredFormatter, metrics


def _record(message: str, extra: dict[str, float | int] | None = None) -> logging.LogRecord:
    record = logging.LogRecord(
        name="jarvis.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg=message,
        args=(),
        exc_info=None,
    )
    for key, value in (extra or {}).items():
        setattr(record, key, value)
    return record


class TestMetrics:
    def test_empty_when_nothing_given(self) -> None:
        assert metrics() == {}

    def test_only_provided_fields_are_attached(self) -> None:
        assert metrics(latency_ms=12.5, tokens_out=7) == {
            "latency_ms": 12.5,
            "tokens_out": 7,
        }

    def test_all_fields(self) -> None:
        fields = metrics(latency_ms=1.0, tokens_in=2, tokens_out=3, cost_usd=0.4)
        assert set(fields) == {"latency_ms", "tokens_in", "tokens_out", "cost_usd"}


class TestStructuredFormatter:
    def test_plain_record_has_no_suffix(self) -> None:
        line = StructuredFormatter().format(_record("hello"))
        assert line.endswith("| jarvis.test | hello")
        assert "latency_ms" not in line

    def test_metric_suffix_rendering_and_order(self) -> None:
        line = StructuredFormatter().format(
            _record(
                "reply ok",
                metrics(cost_usd=0.00094, latency_ms=843.25, tokens_in=512, tokens_out=128),
            )
        )
        assert line.endswith(
            "reply ok | latency_ms=843.2 tokens_in=512 tokens_out=128 cost_usd=0.000940"
        )

    def test_partial_metrics(self) -> None:
        line = StructuredFormatter().format(_record("x", metrics(tokens_in=9)))
        assert line.endswith("x | tokens_in=9")

    def test_zero_values_are_rendered(self) -> None:
        line = StructuredFormatter().format(_record("x", metrics(tokens_out=0, cost_usd=0.0)))
        assert line.endswith("x | tokens_out=0 cost_usd=0.000000")
