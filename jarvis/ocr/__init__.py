"""OCR: text extraction from images and screen regions (RapidOCR).

Responsibility (delivered in phase 13):
    * :class:`OcrBlock` / :class:`OcrResult` — typed, frozen recognition
      results carrying text, per-block confidence and bounding boxes.
    * :class:`TextRecognizer` protocol with a RapidOCR adapter (offline, ONNX,
      no PyTorch, lazy model download on first use).
    * :class:`OcrService` — lifecycle component owning the engine; reads
      ``ocr.*`` configuration and hands the vision layer a ready recogniser.

Allowed dependencies: ``core``, ``config``.
"""

from jarvis.ocr.engines import RapidOcrEngine
from jarvis.ocr.service import OcrService
from jarvis.ocr.types import OcrBlock, OcrResult, TextRecognizer

__all__ = [
    "OcrBlock",
    "OcrResult",
    "OcrService",
    "RapidOcrEngine",
    "TextRecognizer",
]
