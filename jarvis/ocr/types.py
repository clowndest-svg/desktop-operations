"""OCR result shapes and the recognizer protocol.

These types are the only vocabulary the rest of JARVIS uses for optical
character recognition: no engine-specific objects (RapidOCR's nested lists,
PaddleOCR's dicts) leak past :mod:`jarvis.ocr.engines`. Everything here is
frozen so a result can be handed to the LLM prompt builder and the UI at the
same time without either mutating the other's view.

Boxes are kept as four corner points rather than a rectangle because that is
what modern detection models actually produce: rotated and skewed text cannot
be described by an axis-aligned ``(x, y, w, h)``, and a caller that wants a
rectangle can derive it from the points without losing the original.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

__all__ = ["OcrBlock", "OcrResult", "TextRecognizer"]


@dataclass(frozen=True, slots=True)
class OcrBlock:
    """One detected span of text with its position and confidence."""

    text: str
    """The recognised string (already trimmed by the engine)."""

    confidence: float
    """Engine confidence in ``0..1``; used to drop noise before the prompt."""

    box: tuple[tuple[float, float], ...]
    """The four corner points (``(x, y)``) of the detection, in image pixels."""

    def to_dict(self) -> dict[str, object]:
        """JSON-ready view. Tuples become lists so a bridge can serialise it."""
        return {
            "text": self.text,
            "confidence": self.confidence,
            "box": [[x, y] for x, y in self.box],
        }


@dataclass(frozen=True, slots=True)
class OcrResult:
    """Everything one image produced, in reading order.

    ``blocks`` is ordered top-to-bottom, left-to-right — the order the engine
    returns and the order a human reads. :attr:`text` joins that order, so the
    LLM never receives a line that was moved out of place.
    """

    blocks: tuple[OcrBlock, ...]
    engine: str
    """Which engine produced this (``rapidocr``), for logs and diagnostics."""

    elapsed_ms: int
    """Wall-clock inference time; a latency spike is worth seeing in stats."""

    @property
    def text(self) -> str:
        """Blocks joined by newlines, blanks dropped.

        An empty block would otherwise add a stray blank line to the prompt and
        waste tokens without adding meaning.
        """
        return "\n".join(block.text for block in self.blocks if block.text)

    def to_dict(self) -> dict[str, object]:
        """JSON-ready view for the UI bridge and structured logging."""
        return {
            "blocks": [block.to_dict() for block in self.blocks],
            "text": self.text,
            "engine": self.engine,
            "elapsed_ms": self.elapsed_ms,
        }


@runtime_checkable
class TextRecognizer(Protocol):
    """Image-bytes in, :class:`OcrResult` out.

    Consumers depend on this protocol, never on a concrete engine, so the
    engine is swappable per configuration and trivially fakeable in tests
    (the whole suite runs without ONNX Runtime installed).
    """

    @property
    def name(self) -> str:
        """Engine identifier, e.g. ``rapidocr``."""
        ...

    def recognize(self, image: bytes) -> OcrResult:
        """Recognise the text in a PNG/JPEG-encoded image."""
        ...
