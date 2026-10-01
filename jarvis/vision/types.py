"""Vision result shapes and the screen-capture protocol.

A :class:`Screenshot` is the most sensitive value this assistant produces, so
two rules are baked into the type rather than left to callers:

* It carries the raw PNG bytes (for OCR and, later, multimodal models) plus the
  geometry needed to map a box back to a screen coordinate.
* :meth:`Screenshot.to_dict` deliberately omits ``image``. The UI bridge and the
  structured logs both call it; shipping a multi-megabyte base64 blob into every
  log line would be both slow and a privacy leak. Only the byte *count* is
  exposed, which is what a status panel actually needs.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

__all__ = ["ScreenCapture", "Screenshot"]


@dataclass(frozen=True, slots=True)
class Screenshot:
    """One captured frame and the metadata needed to interpret it."""

    image: bytes
    """PNG-encoded pixels (PNG because it is lossless and OCR-safe)."""

    width: int
    height: int
    monitor: int
    """Monitor index the frame came from (``0`` = all monitors, mss convention)."""

    captured_at: str
    """ISO-8601 UTC timestamp, so a stale frame can be told from a fresh one."""

    def to_dict(self) -> dict[str, object]:
        """JSON-ready view **without** the image bytes (see module docstring)."""
        return {
            "width": self.width,
            "height": self.height,
            "monitor": self.monitor,
            "captured_at": self.captured_at,
            "bytes": len(self.image),
        }


@runtime_checkable
class ScreenCapture(Protocol):
    """Monitor-in, :class:`Screenshot` out.

    Implemented by the mss and Pillow backends and faked wholesale in tests, so
    the suite never touches a real display.
    """

    @property
    def name(self) -> str:
        """Backend identifier, e.g. ``mss``."""
        ...

    def capture(self, monitor: int) -> Screenshot:
        """Grab one monitor and return it as a PNG."""
        ...
