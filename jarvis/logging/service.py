"""The :class:`LoggingService` lifecycle component.

Owns process-wide logging configuration. Started immediately after
``ConfigService`` (it consumes the validated ``logging.*`` section and the
resolved ``logs`` directory), it:

1. replaces the bootstrap console logging installed by ``__main__``;
2. attaches a console handler and/or a size-rotating file handler;
3. suppresses third-party noise structurally: the *root* logger is capped
   at ``logging.third_party_level`` while the ``jarvis`` logger runs at
   ``logging.level`` — no fragile per-library name lists needed;
4. installs a ``sys.excepthook`` that records any uncaught exception with
   full traceback before the process dies.

``stop()`` restores the previous handlers, levels and excepthook, closing
everything this service created (idempotent, never raises).
"""

from __future__ import annotations

import contextlib
import logging
import sys
from collections.abc import Callable
from dataclasses import dataclass
from logging.handlers import RotatingFileHandler
from pathlib import Path
from types import TracebackType
from typing import Final

from jarvis.config.schema import LoggingSection
from jarvis.core.constants import DEFAULT_ENCODING
from jarvis.logging.formats import StructuredFormatter

LOG_FILENAME: Final[str] = "jarvis.log"
"""Active log file name inside the ``logs`` directory."""

JARVIS_LOGGER_NAME: Final[str] = "jarvis"
"""Namespace root for all first-party loggers."""

UNHANDLED_LOGGER_NAME: Final[str] = "jarvis.unhandled"
"""Logger used by the last-resort excepthook."""

_ExcHook = Callable[[type[BaseException], BaseException, TracebackType | None], None]


@dataclass(frozen=True, slots=True)
class LoggingSettings:
    """Everything :class:`LoggingService` needs, as one immutable snapshot."""

    section: LoggingSection
    """Validated ``logging.*`` configuration."""

    logs_dir: Path
    """Directory receiving rotating log files."""


@dataclass(slots=True)
class _SavedState:
    """Pre-start logging state, restored verbatim on ``stop()``."""

    root_handlers: list[logging.Handler]
    root_level: int
    jarvis_level: int
    excepthook: _ExcHook


class LoggingService:
    """Lifecycle component configuring process-wide logging.

    Args:
        settings_provider: Deferred settings accessor. Deferred because the
            component is *registered* before ``ConfigService`` has started;
            it is only called inside :meth:`start`, by which time the
            configuration snapshot exists (components start in
            registration order).
    """

    def __init__(self, settings_provider: Callable[[], LoggingSettings]) -> None:
        self._settings_provider = settings_provider
        self._saved: _SavedState | None = None
        self._own_handlers: list[logging.Handler] = []

    @property
    def name(self) -> str:
        """Component name used by the application journal."""
        return "logging"

    @property
    def log_file(self) -> Path | None:
        """Path of the active log file, or ``None`` if file logging is off."""
        for handler in self._own_handlers:
            if isinstance(handler, RotatingFileHandler):
                return Path(handler.baseFilename)
        return None

    def start(self) -> None:
        """Install handlers, levels and the excepthook (no-op if running)."""
        if self._saved is not None:
            return
        settings = self._settings_provider()
        section = settings.section

        root = logging.getLogger()
        jarvis_logger = logging.getLogger(JARVIS_LOGGER_NAME)
        self._saved = _SavedState(
            root_handlers=list(root.handlers),
            root_level=root.level,
            jarvis_level=jarvis_logger.level,
            excepthook=sys.excepthook,
        )

        for handler in list(root.handlers):  # retire bootstrap handlers
            root.removeHandler(handler)

        formatter = StructuredFormatter()
        if section.console:
            console = logging.StreamHandler(stream=sys.stderr)
            console.setFormatter(formatter)
            self._own_handlers.append(console)
        if section.file_enabled:
            settings.logs_dir.mkdir(parents=True, exist_ok=True)
            rotating = RotatingFileHandler(
                settings.logs_dir / LOG_FILENAME,
                maxBytes=section.max_bytes,
                backupCount=section.backup_count,
                encoding=DEFAULT_ENCODING,
            )
            rotating.setFormatter(formatter)
            self._own_handlers.append(rotating)
        for handler in self._own_handlers:
            root.addHandler(handler)

        # Structural noise suppression: third-party loggers propagate to the
        # root and are capped there; first-party loggers live under the
        # "jarvis" namespace and get their own (usually more verbose) level.
        root.setLevel(section.third_party_level)
        jarvis_logger.setLevel(section.level)

        sys.excepthook = self._log_uncaught

    def stop(self) -> None:
        """Restore the pre-start logging state (idempotent, never raises)."""
        saved = self._saved
        if saved is None:
            return
        self._saved = None

        sys.excepthook = saved.excepthook

        root = logging.getLogger()
        for handler in self._own_handlers:
            root.removeHandler(handler)
            # A close failure must never block shutdown.
            with contextlib.suppress(OSError):
                handler.close()
        self._own_handlers.clear()

        for handler in saved.root_handlers:
            root.addHandler(handler)
        root.setLevel(saved.root_level)
        logging.getLogger(JARVIS_LOGGER_NAME).setLevel(saved.jarvis_level)

    def _log_uncaught(
        self,
        exc_type: type[BaseException],
        exc_value: BaseException,
        exc_traceback: TracebackType | None,
    ) -> None:
        """Last-resort hook: record the crash, then defer to the saved hook."""
        if issubclass(exc_type, KeyboardInterrupt):
            # A user Ctrl+C is not a crash; keep the default behaviour.
            self._default_hook(exc_type, exc_value, exc_traceback)
            return
        logging.getLogger(UNHANDLED_LOGGER_NAME).critical(
            "uncaught exception, process will terminate",
            exc_info=(exc_type, exc_value, exc_traceback),
        )

    def _default_hook(
        self,
        exc_type: type[BaseException],
        exc_value: BaseException,
        exc_traceback: TracebackType | None,
    ) -> None:
        saved = self._saved
        hook = saved.excepthook if saved is not None else sys.__excepthook__
        hook(exc_type, exc_value, exc_traceback)
