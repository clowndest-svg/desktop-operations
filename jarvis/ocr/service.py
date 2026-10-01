"""OCR service: the lifecycle component the rest of JARVIS talks to.

The service owns the engine and the configuration read, so callers (the vision
layer, the tools layer, the UI bridge) never construct an engine themselves —
that is the same discipline ``AsrService`` enforces for speech.

Error policy, spelled out because it is a deliberate choice:

* :meth:`start` **never raises**. A missing optional dependency must not stop
  JARVIS from booting offline; when the engine cannot be built the failure is
  recorded and :attr:`running` stays ``False``.
* :meth:`recognize` **always raises** :class:`OcrError` with a Chinese message
  when OCR is disabled, not started, or failed. The UI calls this method, and a
  silent empty result would be indistinguishable from "the screen is blank".
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from jarvis.config.schema import OcrSection
from jarvis.core.exceptions import JarvisError, OcrError
from jarvis.ocr.engines import RapidOcrEngine
from jarvis.ocr.types import OcrResult, TextRecognizer

__all__ = ["OcrService"]

logger = logging.getLogger("jarvis.ocr.service")


class OcrService:
    """Lifecycle component owning the OCR engine."""

    name = "ocr"

    def __init__(
        self,
        settings_provider: Callable[[], OcrSection],
        *,
        engine: TextRecognizer | None = None,
    ) -> None:
        """Create the service.

        Args:
            settings_provider: Returns the validated ``ocr.*`` section. Read
                lazily (never in ``__init__``) because a component is registered
                before ``ConfigService`` has loaded anything.
            engine: Pre-built recognizer, used by tests and by callers that
                already own an engine. When ``None`` the service builds the
                configured engine at :meth:`start`.
        """
        self._settings_provider = settings_provider
        self._engine = engine
        self._failure = ""
        self._requests = 0
        self._blocks = 0
        self._last_elapsed_ms = 0

    @property
    def running(self) -> bool:
        """Whether a usable engine is loaded. ``False`` when disabled or failed."""
        return self._engine is not None

    def start(self) -> None:
        """Build the engine when enabled (idempotent, never raises)."""
        if self._engine is not None:
            return
        settings = self._settings_provider()
        if not settings.enabled:
            logger.info("ocr disabled by configuration; engine not loaded")
            return
        try:
            engine = RapidOcrEngine(
                languages=settings.languages,
                min_confidence=settings.min_confidence,
            )
        except JarvisError as exc:
            self._failure = str(exc)
            logger.error("ocr engine unavailable: %s", exc)
            return
        self._engine = engine
        self._failure = ""
        logger.info("ocr ready (engine=%s)", engine.name)

    def stop(self) -> None:
        """Release the engine (idempotent)."""
        engine, self._engine = self._engine, None
        self._failure = ""
        if engine is not None:
            self._close(engine)

    def recognize(self, image: bytes) -> OcrResult:
        """Recognise the text in a PNG/JPEG image.

        Raises:
            OcrError: OCR is disabled, was never started, failed to start, or
                the engine itself failed. The message is user-facing Chinese.
        """
        settings = self._settings_provider()
        if not settings.enabled:
            raise OcrError("OCR 未启用：请在配置中设置 ocr.enabled=true 后重启")
        engine = self._engine
        if engine is None:
            raise OcrError(self._failure or "OCR 服务未启动：请先调用 start()")
        try:
            result = engine.recognize(image)
        except OcrError:
            raise
        except Exception as exc:  # no engine failure may escape unwrapped
            raise OcrError("OCR 识别失败", details={"cause": repr(exc)}) from exc
        self._requests += 1
        self._blocks += len(result.blocks)
        self._last_elapsed_ms = result.elapsed_ms
        return result

    def stats(self) -> dict[str, object]:
        """JSON-ready snapshot of configuration and counters for the UI."""
        settings = self._settings_provider()
        engine = self._engine
        return {
            "name": self.name,
            "running": engine is not None,
            "enabled": settings.enabled,
            "engine": engine.name if engine is not None else settings.engine,
            "languages": list(settings.languages),
            "min_confidence": settings.min_confidence,
            "requests": self._requests,
            "blocks": self._blocks,
            "last_elapsed_ms": self._last_elapsed_ms,
            "error": self._failure,
        }

    @staticmethod
    def _close(engine: TextRecognizer) -> None:
        """Call ``close`` if the engine offers one; teardown must not raise."""
        close = getattr(engine, "close", None)
        if not callable(close):
            return
        try:
            close()
        except Exception:  # pragma: no cover - defensive
            logger.exception("ocr engine close failed")
