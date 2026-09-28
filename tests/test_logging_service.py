"""Unit tests for jarvis.logging.service (LoggingService lifecycle)."""

from __future__ import annotations

import logging
import sys
from collections.abc import Iterator
from logging.handlers import RotatingFileHandler
from pathlib import Path

import pytest

from jarvis.config.schema import LoggingSection
from jarvis.logging import LoggingService, LoggingSettings

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_section(**overrides: object) -> LoggingSection:
    base: dict[str, object] = {
        "level": "DEBUG",
        "console": False,
        "file_enabled": True,
        "max_bytes": 200,
        "backup_count": 2,
        "third_party_level": "WARNING",
    }
    base.update(overrides)
    return LoggingSection.from_mapping(base)


def make_service(tmp_path: Path, **overrides: object) -> LoggingService:
    settings = LoggingSettings(section=make_section(**overrides), logs_dir=tmp_path / "logs")
    return LoggingService(lambda: settings)


@pytest.fixture(autouse=True)
def _pristine_logging() -> Iterator[None]:
    """Snapshot and restore global logging state around every test."""
    root = logging.getLogger()
    jarvis_logger = logging.getLogger("jarvis")
    saved_handlers = list(root.handlers)
    saved_root_level = root.level
    saved_jarvis_level = jarvis_logger.level
    saved_hook = sys.excepthook
    try:
        yield
    finally:
        root.handlers[:] = saved_handlers
        root.setLevel(saved_root_level)
        jarvis_logger.setLevel(saved_jarvis_level)
        sys.excepthook = saved_hook


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestStartStop:
    def test_file_logging_writes_records(self, tmp_path: Path) -> None:
        service = make_service(tmp_path)
        service.start()
        try:
            logging.getLogger("jarvis.test").info("file record")
        finally:
            service.stop()
        log_file = tmp_path / "logs" / "jarvis.log"
        assert log_file.is_file()
        assert "file record" in log_file.read_text(encoding="utf-8")

    def test_rotation_by_size(self, tmp_path: Path) -> None:
        service = make_service(tmp_path, max_bytes=200, backup_count=2)
        service.start()
        try:
            for i in range(30):
                logging.getLogger("jarvis.test").info("rotation filler %03d", i)
        finally:
            service.stop()
        logs = sorted(p.name for p in (tmp_path / "logs").iterdir())
        assert "jarvis.log" in logs
        assert "jarvis.log.1" in logs
        assert len(logs) <= 3  # active + backup_count

    def test_stop_restores_previous_state(self, tmp_path: Path) -> None:
        root = logging.getLogger()
        before_handlers = list(root.handlers)
        before_hook = sys.excepthook

        service = make_service(tmp_path)
        service.start()
        assert list(root.handlers) != before_handlers or sys.excepthook is not before_hook
        service.stop()

        assert list(root.handlers) == before_handlers
        assert sys.excepthook is before_hook

    def test_start_and_stop_are_idempotent(self, tmp_path: Path) -> None:
        service = make_service(tmp_path)
        service.start()
        service.start()  # no-op
        handlers = logging.getLogger().handlers
        assert len([h for h in handlers if isinstance(h, RotatingFileHandler)]) == 1
        service.stop()
        service.stop()  # no-op

    def test_console_disabled_and_file_disabled_installs_nothing(self, tmp_path: Path) -> None:
        service = make_service(tmp_path, console=False, file_enabled=False)
        service.start()
        try:
            assert logging.getLogger().handlers == []
            assert service.log_file is None
        finally:
            service.stop()

    def test_log_file_property(self, tmp_path: Path) -> None:
        service = make_service(tmp_path)
        service.start()
        try:
            assert service.log_file == tmp_path / "logs" / "jarvis.log"
        finally:
            service.stop()


class TestLevels:
    def test_third_party_noise_is_suppressed(self, tmp_path: Path) -> None:
        service = make_service(tmp_path, level="DEBUG", third_party_level="WARNING")
        service.start()
        try:
            logging.getLogger("jarvis.sub").debug("first-party debug")
            logging.getLogger("noisy.thirdparty").info("third-party info")
            logging.getLogger("noisy.thirdparty").warning("third-party warning")
        finally:
            service.stop()
        content = (tmp_path / "logs" / "jarvis.log").read_text(encoding="utf-8")
        assert "first-party debug" in content
        assert "third-party info" not in content
        assert "third-party warning" in content

    def test_jarvis_level_respects_config(self, tmp_path: Path) -> None:
        service = make_service(tmp_path, level="WARNING")
        service.start()
        try:
            logging.getLogger("jarvis.sub").info("hidden info")
            logging.getLogger("jarvis.sub").warning("visible warning")
        finally:
            service.stop()
        content = (tmp_path / "logs" / "jarvis.log").read_text(encoding="utf-8")
        assert "hidden info" not in content
        assert "visible warning" in content


class TestExcepthook:
    def test_uncaught_exception_is_logged_with_traceback(self, tmp_path: Path) -> None:
        service = make_service(tmp_path)
        service.start()
        try:
            error = ValueError("boom")
            sys.excepthook(ValueError, error, error.__traceback__)
        finally:
            service.stop()
        content = (tmp_path / "logs" / "jarvis.log").read_text(encoding="utf-8")
        assert "uncaught exception" in content
        assert "ValueError: boom" in content

    def test_keyboard_interrupt_is_delegated_not_logged(self, tmp_path: Path) -> None:
        delegated: list[str] = []
        previous_hook = sys.excepthook
        sys.excepthook = lambda *args: delegated.append("called")
        try:
            service = make_service(tmp_path)
            service.start()
            try:
                interrupt = KeyboardInterrupt()
                sys.excepthook(KeyboardInterrupt, interrupt, None)
            finally:
                service.stop()
            content = (tmp_path / "logs" / "jarvis.log").read_text(encoding="utf-8")
            assert delegated == ["called"]
            assert "uncaught exception" not in content
        finally:
            sys.excepthook = previous_hook
