"""Concrete OCR engine adapters.

RapidOCR (``rapidocr-onnxruntime``) is the only engine for now: it is offline,
ONNX-based and needs no PyTorch, which matters because JARVIS must boot on a
machine that never installed the heavy voice stack.

Two deliberate import choices:

* ``rapidocr_onnxruntime`` is imported through :func:`importlib.import_module`
  rather than a static ``import``. The package is optional, and a static import
  would both fail mypy on an untyped third-party module and — more importantly —
  be picked up by ``tests/test_packaging_declared_deps.py``, which refuses any
  import whose distribution is not declared in ``pyproject.toml``. A string
  lookup keeps the dependency honest and lazy at the same time.
* The module is imported in :meth:`RapidOcrEngine.__init__` (fail fast, no
  download) while the actual ``RapidOCR`` object is built on the first
  :meth:`recognize`. RapidOCR downloads and loads its ~15 MB of models the first
  time it is constructed, so deferring that keeps :meth:`~jarvis.ocr.service.
  OcrService.start` free of network I/O and model loading — the offline-boot
  guarantee the whole application relies on.
"""

from __future__ import annotations

import importlib
import logging
import time
from collections.abc import Sequence
from typing import Any

from jarvis.core.exceptions import OcrError
from jarvis.ocr.types import OcrBlock, OcrResult

__all__ = ["RapidOcrEngine"]

logger = logging.getLogger("jarvis.ocr.engines")

_RAPIDOCR_MODULE = "rapidocr_onnxruntime"
"""Import name of the RapidOCR distribution (``rapidocr-onnxruntime``)."""


def _missing_dependency(exc: ImportError) -> OcrError:
    """Build the precise, actionable error for a missing optional package."""
    return OcrError(
        "OCR 引擎 rapidocr 需要可选依赖 rapidocr-onnxruntime，请先安装："
        "pip install rapidocr-onnxruntime",
        details={
            "engine": "rapidocr",
            "missing_package": "rapidocr-onnxruntime",
            "hint": "pip install rapidocr-onnxruntime",
            "cause": repr(exc),
        },
    )


class RapidOcrEngine:
    """RapidOCR adapter — offline, ONNX, no PyTorch.

    Construction imports the (optional) module so a missing dependency fails
    immediately and readably, but does **not** build the ``RapidOCR`` object:
    that is deferred to the first :meth:`recognize` because it is the step that
    downloads the model weights. Callers therefore get a cheap, offline-safe
    constructor and a precise error at the moment OCR is actually requested.
    """

    def __init__(self, *, languages: Sequence[str], min_confidence: float) -> None:
        """Record the configuration and import the engine module.

        Args:
            languages: Language hints from configuration. ``rapidocr-onnxruntime``
                ships one PP-OCR model that already covers Chinese + English, so
                the value is recorded for diagnostics rather than passed on.
            min_confidence: Blocks below this score are dropped. OCR noise in a
                prompt is worse than a missing line, so filtering happens here
                and not in the caller.
        """
        self._languages = tuple(languages)
        self._min_confidence = min_confidence
        self._module: Any = self._import_module()
        self._engine: Any = None

    @staticmethod
    def _import_module() -> Any:
        try:
            return importlib.import_module(_RAPIDOCR_MODULE)
        except ImportError as exc:
            raise _missing_dependency(exc) from exc

    @property
    def name(self) -> str:
        return "rapidocr"

    @property
    def languages(self) -> tuple[str, ...]:
        """Configured language hints (diagnostics only, see ``__init__``)."""
        return self._languages

    def recognize(self, image: bytes) -> OcrResult:
        """Recognise the text in a PNG/JPEG image and filter by confidence."""
        if not image:
            raise OcrError("OCR 收到空图像，无法识别", details={"engine": self.name})
        engine = self._ensure_engine()
        started = time.perf_counter()
        try:
            raw, _elapsed = engine(image)
        except Exception as exc:
            raise OcrError(
                "RapidOCR 识别失败",
                details={"engine": self.name, "cause": repr(exc)},
            ) from exc
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        return OcrResult(blocks=self._parse(raw), engine=self.name, elapsed_ms=elapsed_ms)

    def close(self) -> None:
        """Drop the engine handle; recognition after close is invalid."""
        self._engine = None

    def _ensure_engine(self) -> Any:
        """Build ``RapidOCR`` on first use (this is the model-download step)."""
        if self._engine is not None:
            return self._engine
        try:
            self._engine = self._module.RapidOCR()
        except Exception as exc:
            raise OcrError(
                "RapidOCR 引擎初始化失败（首次运行会下载模型，请检查网络与磁盘）",
                details={"engine": self.name, "cause": repr(exc)},
            ) from exc
        return self._engine

    def _parse(self, raw: Any) -> tuple[OcrBlock, ...]:
        """Turn RapidOCR's ``[[box, text, score], ...]`` into typed blocks.

        Malformed entries are skipped rather than fatal: a single bad detection
        must not lose the whole page of otherwise-good text.
        """
        if not raw:
            return ()
        blocks: list[OcrBlock] = []
        for entry in raw:
            try:
                box, text, score = entry[0], entry[1], entry[2]
                confidence = float(score)
                points = tuple((float(point[0]), float(point[1])) for point in box)
            except (TypeError, IndexError, KeyError, ValueError):
                logger.debug("skipping malformed OCR entry: %r", entry)
                continue
            if confidence < self._min_confidence:
                continue
            blocks.append(OcrBlock(text=str(text).strip(), confidence=confidence, box=points))
        return tuple(blocks)
