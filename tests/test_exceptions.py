"""Unit tests for the exception hierarchy (phase 2)."""

from __future__ import annotations

import pytest

from jarvis.app.application import Application, ComponentStartError
from jarvis.core.exceptions import (
    AsrError,
    AudioError,
    DangerousOperationRejectedError,
    JarvisError,
    OcrError,
    SecurityError,
    ToolExecutionError,
    ToolNotFoundError,
    VisionError,
)


def test_message_only_str() -> None:
    error = JarvisError("something failed")
    assert str(error) == "something failed"
    assert error.details == {}


def test_details_are_rendered_and_stored() -> None:
    error = JarvisError("tool failed", details={"tool": "ocr", "attempt": 2})
    assert error.message == "tool failed"
    assert error.details == {"tool": "ocr", "attempt": 2}
    assert str(error) == "tool failed (tool='ocr', attempt=2)"


def test_hierarchy_relationships() -> None:
    # Audio pipeline errors funnel into AudioError.
    assert issubclass(AsrError, AudioError)
    # OCR is a vision concern.
    assert issubclass(OcrError, VisionError)
    # Dangerous-operation rejection is a security concern.
    assert issubclass(DangerousOperationRejectedError, SecurityError)
    # Tool errors share a common base for registry-level handling.
    assert issubclass(ToolNotFoundError, JarvisError)
    assert issubclass(ToolExecutionError, JarvisError)


def test_single_except_clause_catches_everything() -> None:
    with pytest.raises(JarvisError):
        raise ToolNotFoundError("unknown tool", details={"name": "does-not-exist"})


class ExplodingComponent:
    """Component whose start() always fails."""

    @property
    def name(self) -> str:
        return "exploding"

    def start(self) -> None:
        raise RuntimeError("bang")

    def stop(self) -> None:  # pragma: no cover - never started
        raise AssertionError("stop must not be called for a component that never started")


def test_component_start_error_is_a_jarvis_error() -> None:
    app = Application()
    app.register(ExplodingComponent())

    with pytest.raises(JarvisError) as excinfo:
        app.start()

    error = excinfo.value
    assert isinstance(error, ComponentStartError)
    assert error.component_name == "exploding"
    assert error.details == {"component": "exploding"}
    assert isinstance(error.cause, RuntimeError)
