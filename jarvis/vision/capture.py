"""Concrete screen-capture backends (mss and Pillow).

Both are optional runtime dependencies and are imported through
:func:`importlib.import_module` rather than static ``import`` statements: that
keeps them off the import path of a headless install *and* keeps
``tests/test_packaging_declared_deps.py`` honest, since neither package is
declared in ``pyproject.toml`` (a static import would trip that gate).

The module import happens in ``__init__`` (cheap, no display access, no model
download) so a missing dependency fails fast and readably at the moment the
service selects a backend, rather than halfway through a capture. Constructing
a backend never opens a screen; only :meth:`capture` does.

``mss`` is the default — it is small, fast and understands multiple monitors.
``pillow`` exists as a fallback for machines that already have Pillow but not
mss; it can only grab the primary screen, and says so plainly instead of
silently capturing the wrong display.
"""

from __future__ import annotations

import importlib
import io
import logging
from datetime import UTC, datetime
from typing import Any

from jarvis.core.exceptions import VisionError
from jarvis.vision.types import Screenshot

__all__ = ["MssCapture", "PillowCapture"]

logger = logging.getLogger("jarvis.vision.capture")

_MSS_MODULE = "mss"
_MSS_TOOLS_MODULE = "mss.tools"
_PILLOW_GRAB_MODULE = "PIL.ImageGrab"


def _import(name: str) -> Any:
    """Import an optional module eagerly, or raise a precise :class:`VisionError`."""
    try:
        return importlib.import_module(name)
    except ImportError as exc:
        package = "pillow" if name.startswith("PIL") else "mss"
        raise VisionError(
            f"截图后端需要可选依赖 {package}，请先安装：pip install {package}",
            details={
                "module": name,
                "missing_package": package,
                "hint": f"pip install {package}",
                "cause": repr(exc),
            },
        ) from exc


def _now_iso() -> str:
    """Current UTC time as an ISO-8601 string (seconds precision is enough)."""
    return datetime.now(UTC).isoformat(timespec="seconds")


class MssCapture:
    """Screen capture via ``mss`` — fast, small, multi-monitor aware."""

    def __init__(self) -> None:
        self._module: Any = _import(_MSS_MODULE)
        self._tools: Any = _import(_MSS_TOOLS_MODULE)

    @property
    def name(self) -> str:
        return "mss"

    def capture(self, monitor: int) -> Screenshot:
        """Grab ``monitor`` (``0`` = the whole virtual desktop, mss convention)."""
        try:
            with self._module.mss() as session:
                monitors = list(session.monitors)
                if not 0 <= monitor < len(monitors):
                    raise VisionError(
                        f"显示器编号 {monitor} 不存在",
                        details={"monitor": monitor, "available": len(monitors)},
                    )
                shot = session.grab(monitors[monitor])
                png = self._tools.to_png(shot.rgb, shot.size)
                width, height = shot.size
        except VisionError:
            raise
        except Exception as exc:
            raise VisionError(
                "mss 截图失败",
                details={"monitor": monitor, "cause": repr(exc)},
            ) from exc
        return Screenshot(
            image=bytes(png),
            width=int(width),
            height=int(height),
            monitor=monitor,
            captured_at=_now_iso(),
        )


class PillowCapture:
    """Screen capture via ``PIL.ImageGrab`` — fallback, primary monitor only."""

    def __init__(self) -> None:
        self._grab: Any = _import(_PILLOW_GRAB_MODULE)

    @property
    def name(self) -> str:
        return "pillow"

    def capture(self, monitor: int) -> Screenshot:
        """Grab the primary screen; refuse any other monitor explicitly.

        Pillow's cross-platform grab has no monitor index, and guessing would
        silently capture the wrong display. Refusing with a message that names
        the mss backend is the honest alternative.
        """
        if monitor != 0:
            raise VisionError(
                "pillow 截图后端只支持主显示器（monitor=0）；多显示器请改用 vision.backend=mss",
                details={"monitor": monitor, "backend": self.name},
            )
        try:
            image = self._grab.grab()
            buffer = io.BytesIO()
            image.save(buffer, format="PNG")
            png = buffer.getvalue()
            width, height = image.size
        except Exception as exc:
            raise VisionError(
                "Pillow 截图失败",
                details={"monitor": monitor, "cause": repr(exc)},
            ) from exc
        return Screenshot(
            image=bytes(png),
            width=int(width),
            height=int(height),
            monitor=monitor,
            captured_at=_now_iso(),
        )
