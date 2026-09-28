"""Unit tests for the application lifecycle (phase 1)."""

from __future__ import annotations

from pathlib import Path

import pytest

from jarvis import __version__
from jarvis.__main__ import main
from jarvis.app.application import Application, AppState, ComponentStartError


class RecordingComponent:
    """Test double that records lifecycle calls into a shared journal."""

    def __init__(self, name: str, journal: list[str], *, fail_on_start: bool = False) -> None:
        self._name = name
        self._journal = journal
        self._fail_on_start = fail_on_start

    @property
    def name(self) -> str:
        return self._name

    def start(self) -> None:
        if self._fail_on_start:
            raise RuntimeError("boom")
        self._journal.append(f"start:{self._name}")

    def stop(self) -> None:
        self._journal.append(f"stop:{self._name}")


def assert_state(app: Application, expected: AppState) -> None:
    """Assert the app state via a helper so mypy narrowing does not leak between calls."""
    assert app.state is expected


def test_start_and_stop_run_in_correct_order() -> None:
    journal: list[str] = []
    app = Application()
    app.register(RecordingComponent("a", journal))
    app.register(RecordingComponent("b", journal))

    app.start()
    assert_state(app, AppState.RUNNING)

    app.stop()
    assert_state(app, AppState.STOPPED)
    assert journal == ["start:a", "start:b", "stop:b", "stop:a"]


def test_start_failure_rolls_back_started_components() -> None:
    journal: list[str] = []
    app = Application()
    app.register(RecordingComponent("a", journal))
    app.register(RecordingComponent("b", journal, fail_on_start=True))

    with pytest.raises(ComponentStartError) as excinfo:
        app.start()

    assert excinfo.value.component_name == "b"
    assert_state(app, AppState.STOPPED)
    assert journal == ["start:a", "stop:a"]


def test_register_after_start_is_rejected() -> None:
    app = Application()
    app.start()
    with pytest.raises(RuntimeError):
        app.register(RecordingComponent("late", []))
    app.stop()


def test_duplicate_component_name_is_rejected() -> None:
    journal: list[str] = []
    app = Application()
    app.register(RecordingComponent("a", journal))
    with pytest.raises(ValueError, match="duplicate component name"):
        app.register(RecordingComponent("a", journal))


def test_stop_is_idempotent() -> None:
    app = Application()
    app.start()
    app.stop()
    app.stop()  # second call must be a no-op, not an error
    assert_state(app, AppState.STOPPED)


def test_main_smoke_run_returns_zero(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    r"""``python -m jarvis`` with nothing configured must come up and shut down.

    The data root is redirected to a temporary directory on purpose: without it the
    test reads whatever the developer has in ``%LOCALAPPDATA%\Jarvis`` -- and a
    machine with ``wakeword.enabled: true`` there would make this test open the
    microphone. A test that can touch real hardware is not a smoke test.
    """
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "jarvis-home"))

    assert main([]) == 0


def test_version_is_semver_like() -> None:
    major, minor, patch = __version__.split(".")
    assert major.isdigit()
    assert minor.isdigit()
    assert patch.isdigit()
