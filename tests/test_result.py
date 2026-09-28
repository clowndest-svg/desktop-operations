"""Unit tests for the Result type (phase 2)."""

from __future__ import annotations

import pytest

from jarvis.core.result import Err, Ok, Result, UnwrapError


def parse_int(raw: str) -> Result[int, str]:
    """Sample fallible function used across the tests."""
    try:
        return Ok(int(raw))
    except ValueError:
        return Err(f"not an integer: {raw!r}")


def test_ok_basics() -> None:
    result = parse_int("42")
    assert result.is_ok() is True
    assert result.is_err() is False
    assert result.ok() == 42
    assert result.err() is None
    assert result.unwrap() == 42
    assert result.unwrap_or(0) == 42
    assert result.expect("must parse") == 42


def test_err_basics() -> None:
    result = parse_int("oops")
    assert result.is_ok() is False
    assert result.is_err() is True
    assert result.ok() is None
    assert result.err() == "not an integer: 'oops'"
    assert result.unwrap_or(7) == 7


def test_err_unwrap_raises() -> None:
    result = parse_int("oops")
    with pytest.raises(UnwrapError, match="not an integer"):
        result.unwrap()


def test_err_expect_raises_with_custom_message() -> None:
    result = parse_int("oops")
    with pytest.raises(UnwrapError, match="custom context"):
        result.expect("custom context")


def test_map_transforms_only_ok() -> None:
    assert parse_int("21").map(lambda v: v * 2) == Ok(42)
    assert parse_int("x").map(lambda v: v * 2) == Err("not an integer: 'x'")


def test_map_err_transforms_only_err() -> None:
    assert parse_int("21").map_err(str.upper) == Ok(21)
    assert parse_int("x").map_err(str.upper) == Err("NOT AN INTEGER: 'X'")


def test_and_then_chains() -> None:
    def halve(value: int) -> Result[int, str]:
        if value % 2:
            return Err(f"{value} is odd")
        return Ok(value // 2)

    assert parse_int("42").and_then(halve) == Ok(21)
    assert parse_int("43").and_then(halve) == Err("43 is odd")
    assert parse_int("x").and_then(halve) == Err("not an integer: 'x'")


def test_pattern_matching() -> None:
    match parse_int("5"):
        case Ok(value):
            assert value == 5
        case Err():  # pragma: no cover - defensive branch
            pytest.fail("expected Ok")

    match parse_int("nope"):
        case Ok():  # pragma: no cover - defensive branch
            pytest.fail("expected Err")
        case Err(error):
            assert "nope" in error


def test_variants_are_immutable() -> None:
    ok: Ok[int, str] = Ok(1)
    with pytest.raises(AttributeError):
        ok.value = 2  # type: ignore[misc]
