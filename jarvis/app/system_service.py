"""System service: the application-layer door the desktop UI reads through.

Rule 6 of the architecture says ``ui`` may only talk to interfaces exposed by
``app``. This is that interface for machine telemetry: it owns the L2 monitor
(which is the only place allowed to import ``psutil``) and hands the UI plain
JSON-ready dicts.

It also makes partial failure loud. A dashboard that silently shows an empty
disk list because one mount was unreadable is worse than one that says so.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from jarvis.core.exceptions import JarvisError

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Callable

    from jarvis.tools.monitor import SystemMonitor

logger = logging.getLogger("jarvis.app.system_service")


class SystemServiceError(JarvisError):
    """The machine could not be read at all."""


@dataclass(frozen=True, slots=True)
class SystemReport:
    """A snapshot plus the caveats needed to read it honestly."""

    metrics: dict[str, Any]
    warnings: tuple[str, ...] = field(default_factory=tuple)
    error: str = ""
    """Non-empty when the whole reading failed; the UI shows this, not zeros."""

    def to_dict(self) -> dict[str, Any]:
        return {
            "metrics": self.metrics,
            "warnings": list(self.warnings),
            "error": self.error,
        }


class SystemService:
    """Lifecycle component exposing system telemetry to the presentation layer."""

    name = "system"

    def __init__(self, monitor_factory: Callable[[], SystemMonitor]) -> None:
        self._monitor_factory = monitor_factory
        self._monitor: SystemMonitor | None = None
        self._failure = ""

    def start(self) -> None:
        """Build the monitor, downgrading to ``error`` reports when it is absent.

        A missing optional dependency (``psutil`` on a headless-ish install) must
        not roll back the whole application: the HUD still has the chat panel, the
        disk panel and the mic indicator. ``report()`` then says so out loud rather
        than showing zeros, which is the honest version of the same failure.
        """
        if self._monitor is not None:
            return
        try:
            self._monitor = self._monitor_factory()
        except JarvisError as exc:
            self._failure = str(exc)
            logger.error("system telemetry unavailable: %s", exc)
            return
        self._failure = ""
        logger.info("system service ready")

    def stop(self) -> None:
        self._monitor = None
        self._failure = ""

    @property
    def running(self) -> bool:
        return self._monitor is not None

    def report(self) -> SystemReport:
        """Take one snapshot. Never raises: the UI polls this on a timer."""
        monitor = self._monitor
        if monitor is None:
            reason = self._failure or "系统服务未启动"
            return SystemReport(metrics={}, error=f"遥测读取失败：{reason}")
        try:
            snapshot = monitor.snapshot()
        except JarvisError as exc:
            logger.error("system snapshot failed: %s", exc)
            return SystemReport(metrics={}, error=str(exc))
        except Exception as exc:  # pragma: no cover - platform specific
            logger.exception("system snapshot failed unexpectedly")
            return SystemReport(metrics={}, error=f"{type(exc).__name__}: {exc}")
        payload = snapshot.to_dict()
        warnings = tuple(snapshot.warnings)
        if warnings:
            logger.warning("system snapshot partial: %s", "; ".join(warnings))
        return SystemReport(metrics=payload, warnings=warnings)
