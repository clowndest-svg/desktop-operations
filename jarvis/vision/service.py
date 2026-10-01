"""Vision service: screen capture, OCR hand-off and LLM summarisation.

This is the L2 door for "what is on my screen". It owns the capture backend and
composes the two lower capabilities it is allowed to reach — OCR (to read the
text) and the LLM (to summarise it) — without either of them knowing the other
exists.

The privacy rule drives the whole design: a screenshot is the most private
thing this assistant can touch, so it is never implicit. When ``vision.enabled``
is false :meth:`capture` refuses with a message that says how to turn it on,
rather than returning a black frame that looks like a working feature.

Like :class:`~jarvis.ocr.service.OcrService`, :meth:`start` never raises — a
missing optional backend must not stop JARVIS from booting — while
:meth:`capture` / :meth:`read_text` / :meth:`describe` raise :class:`VisionError`
so the UI always has a reason to show.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from jarvis.config.schema import VisionSection
from jarvis.core.exceptions import JarvisError, OcrError, VisionError
from jarvis.llm import ChatMessage, LlmClient
from jarvis.ocr import OcrResult, OcrService
from jarvis.prompt import render_prompt
from jarvis.vision.capture import MssCapture, PillowCapture
from jarvis.vision.types import ScreenCapture, Screenshot

__all__ = ["VisionService"]

logger = logging.getLogger("jarvis.vision.service")


class VisionService:
    """Lifecycle component owning screen capture and its composition."""

    name = "vision"

    def __init__(
        self,
        settings_provider: Callable[[], VisionSection],
        *,
        ocr_provider: Callable[[], OcrService] | None = None,
        llm_provider: Callable[[], LlmClient] | None = None,
        capture: ScreenCapture | None = None,
    ) -> None:
        """Create the service.

        Args:
            settings_provider: Returns the validated ``vision.*`` section, read
                lazily because configuration does not exist at registration.
            ocr_provider: Returns the running OCR service for :meth:`read_text`
                and :meth:`describe`. ``None`` means OCR is unavailable.
            llm_provider: Returns the chat client used to summarise OCR text in
                :meth:`describe`. ``None`` degrades to returning the raw text.
            capture: Pre-built backend. When ``None`` the backend named by
                configuration is constructed at :meth:`start`. It is only
                adopted when the feature is enabled.
        """
        self._settings_provider = settings_provider
        self._ocr_provider = ocr_provider
        self._llm_provider = llm_provider
        self._injected = capture
        self._capture: ScreenCapture | None = None
        self._failure = ""
        self._captures = 0
        self._reads = 0
        self._describes = 0

    @property
    def running(self) -> bool:
        """Whether a capture backend is active. ``False`` when disabled or failed."""
        return self._capture is not None

    def start(self) -> None:
        """Select and build the capture backend when enabled (never raises)."""
        settings = self._settings_provider()
        if not settings.enabled:
            logger.info("vision disabled by configuration; screen capture not enabled")
            return
        if self._capture is not None:
            return
        try:
            self._capture = (
                self._injected
                if self._injected is not None
                else self._build_capture(settings.backend)
            )
        except JarvisError as exc:
            self._failure = str(exc)
            logger.error("vision capture unavailable: %s", exc)
            return
        self._failure = ""
        logger.info("vision ready (backend=%s)", self._capture.name)

    def stop(self) -> None:
        """Release the backend (idempotent)."""
        self._capture = None
        self._failure = ""

    def capture(self, monitor: int = 0) -> Screenshot:
        """Grab ``monitor`` and return it, refusing when disabled or oversized.

        Raises:
            VisionError: the feature is disabled, was never started, failed to
                start, the backend failed, or the image exceeds
                ``vision.max_image_mb``.
        """
        settings = self._settings_provider()
        if not settings.enabled:
            raise VisionError("视觉功能未启用：请在配置中设置 vision.enabled=true 后重启")
        capture = self._capture
        if capture is None:
            raise VisionError(self._failure or "视觉服务未启动：请先调用 start()")
        try:
            shot = capture.capture(monitor)
        except VisionError:
            raise
        except Exception as exc:
            raise VisionError(
                "屏幕截图失败",
                details={"monitor": monitor, "cause": repr(exc)},
            ) from exc
        limit = settings.max_image_mb * 1024 * 1024
        if len(shot.image) > limit:
            raise VisionError(
                f"截图过大（{len(shot.image) // (1024 * 1024)} MB），"
                f"超过 vision.max_image_mb={settings.max_image_mb}",
                details={"bytes": len(shot.image), "max_image_mb": settings.max_image_mb},
            )
        self._captures += 1
        return shot

    def read_text(self, monitor: int = 0) -> tuple[Screenshot, OcrResult]:
        """Capture ``monitor`` and OCR it, returning both the frame and the text.

        The frame is returned alongside the text so a caller can show *what* was
        read (a preview thumbnail) and map a block back to a screen coordinate.
        """
        shot = self.capture(monitor)
        ocr = self._ocr_provider() if self._ocr_provider is not None else None
        if ocr is None:
            raise VisionError("OCR 未配置：read_text 需要 ocr_provider")
        try:
            result = ocr.recognize(shot.image)
        except OcrError:
            raise
        except Exception as exc:
            raise VisionError("OCR 识别失败", details={"cause": repr(exc)}) from exc
        self._reads += 1
        return shot, result

    def describe(self, monitor: int = 0) -> str:
        """Summarise what is on the screen as Chinese text.

        Degrades instead of failing when no LLM is available or the model call
        fails: the raw OCR text is returned and a warning is logged. Returning
        *something* the user can read beats a hard error on a feature whose whole
        point is "tell me what this says".
        """
        _shot, result = self.read_text(monitor)
        text = result.text.strip()
        llm = self._llm_provider() if self._llm_provider is not None else None
        if llm is None:
            logger.warning("vision.describe: 未配置 LLM，返回原始 OCR 文本")
            return text
        if not text:
            logger.warning("vision.describe: OCR 未识别到文字，无内容可总结")
            return ""
        messages = [
            ChatMessage.system(render_prompt("vision_describe")),
            ChatMessage.user(text),
        ]
        try:
            response = llm.complete(messages)
        except Exception as exc:  # a summary failure must not hide the text
            logger.warning("vision.describe: LLM 调用失败（%s），返回原始 OCR 文本", exc)
            return text
        self._describes += 1
        return response.content.strip()

    def stats(self) -> dict[str, object]:
        """JSON-ready snapshot of configuration and counters for the UI."""
        settings = self._settings_provider()
        capture = self._capture
        return {
            "name": self.name,
            "running": capture is not None,
            "enabled": settings.enabled,
            "backend": capture.name if capture is not None else settings.backend,
            "max_image_mb": settings.max_image_mb,
            "captures": self._captures,
            "reads": self._reads,
            "describes": self._describes,
            "error": self._failure,
        }

    @staticmethod
    def _build_capture(backend: str) -> ScreenCapture:
        if backend == "mss":
            return MssCapture()
        if backend == "pillow":
            return PillowCapture()
        raise VisionError("未知的截图后端", details={"backend": backend})  # pragma: no cover
