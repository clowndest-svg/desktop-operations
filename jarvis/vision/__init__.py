"""Vision: screen/image understanding for multimodal reasoning.

Responsibility (delivered in phase 13):
    * :class:`Screenshot` — a captured frame plus geometry, with a
      ``to_dict`` that deliberately excludes the (large, private) pixels.
    * :class:`ScreenCapture` protocol with ``mss`` (default) and Pillow
      backends, both imported lazily so a headless install still boots.
    * :class:`VisionService` — lifecycle component that composes capture, OCR
      (:mod:`jarvis.ocr`) and the LLM (:mod:`jarvis.llm`) into ``capture`` /
      ``read_text`` / ``describe``.

Allowed dependencies: ``core``, ``config``, ``ocr``, ``llm``.
"""

from jarvis.vision.capture import MssCapture, PillowCapture
from jarvis.vision.service import VisionService
from jarvis.vision.types import ScreenCapture, Screenshot

__all__ = [
    "MssCapture",
    "PillowCapture",
    "ScreenCapture",
    "Screenshot",
    "VisionService",
]
