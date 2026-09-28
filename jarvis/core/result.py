"""A typed, Rust-style ``Result`` container for expected failure paths.

Exceptions remain the mechanism for programming errors and truly
exceptional situations. ``Result`` is for *expected*, recoverable failures
("tool not found", "no wake word detected", "cache miss") where the caller
must consciously handle both branches and where raising would turn normal
control flow into exception handling.

Usage::

    def find_tool(name: str) -> Result[Tool, str]:
        if name in registry:
            return Ok(registry[name])
        return Err(f"unknown tool: {name}")

    match find_tool("ocr"):
        case Ok(tool):
            tool.run()
        case Err(reason):
            logger.warning("skipped: %s", reason)
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Final, NoReturn, final


class UnwrapError(RuntimeError):
    """Raised when ``unwrap``/``expect`` is called on the wrong variant."""


@final
@dataclass(frozen=True, slots=True)
class Ok[T, E]:
    """Success variant carrying ``value``."""

    value: T

    def is_ok(self) -> bool:
        """Return ``True``: this is the success variant."""
        return True

    def is_err(self) -> bool:
        """Return ``False``: this is not the error variant."""
        return False

    def ok(self) -> T | None:
        """Return the success value."""
        return self.value

    def err(self) -> E | None:
        """Return ``None``: there is no error."""
        return None

    def unwrap(self) -> T:
        """Return the success value."""
        return self.value

    def unwrap_or(self, default: T) -> T:
        """Return the success value, ignoring ``default``."""
        return self.value

    def expect(self, message: str) -> T:
        """Return the success value, ignoring ``message``."""
        return self.value

    def map[U](self, fn: Callable[[T], U]) -> Result[U, E]:
        """Apply ``fn`` to the value, producing a new ``Ok``."""
        return Ok(fn(self.value))

    def map_err[F](self, fn: Callable[[E], F]) -> Result[T, F]:
        """Return ``self`` unchanged (there is no error to map)."""
        return Ok(self.value)

    def and_then[U](self, fn: Callable[[T], Result[U, E]]) -> Result[U, E]:
        """Chain another fallible computation on the success value."""
        return fn(self.value)


@final
@dataclass(frozen=True, slots=True)
class Err[T, E]:
    """Failure variant carrying ``error``."""

    error: E

    def is_ok(self) -> bool:
        """Return ``False``: this is not the success variant."""
        return False

    def is_err(self) -> bool:
        """Return ``True``: this is the error variant."""
        return True

    def ok(self) -> T | None:
        """Return ``None``: there is no success value."""
        return None

    def err(self) -> E | None:
        """Return the error value."""
        return self.error

    def unwrap(self) -> NoReturn:
        """Raise :class:`UnwrapError`: there is no success value."""
        raise UnwrapError(f"called unwrap() on Err({self.error!r})")

    def unwrap_or(self, default: T) -> T:
        """Return ``default``."""
        return default

    def expect(self, message: str) -> NoReturn:
        """Raise :class:`UnwrapError` with the caller-supplied ``message``."""
        raise UnwrapError(message)

    def map[U](self, fn: Callable[[T], U]) -> Result[U, E]:
        """Propagate the error unchanged."""
        return Err(self.error)

    def map_err[F](self, fn: Callable[[E], F]) -> Result[T, F]:
        """Apply ``fn`` to the error, producing a new ``Err``."""
        return Err(fn(self.error))

    def and_then[U](self, fn: Callable[[T], Result[U, E]]) -> Result[U, E]:
        """Propagate the error unchanged (``fn`` is not called)."""
        return Err(self.error)


type Result[T, E] = Ok[T, E] | Err[T, E]
"""Union alias: a computation that produced either ``Ok[T]`` or ``Err[E]``."""

RESULT_VARIANTS: Final[tuple[type, type]] = (Ok, Err)
"""All concrete variants, useful for isinstance checks in generic code."""
