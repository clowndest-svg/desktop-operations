"""Tests for the vision layer (jarvis.vision).

No real screen is ever grabbed and no network request is made: the mss and
Pillow backends are exercised by monkeypatching ``importlib`` inside
:mod:`jarvis.vision.capture`, and :class:`VisionService` is driven with a fake
capture plus a real (fake-engine-backed) :class:`OcrService`. A test that
touched a real display would be flaky on CI and would capture the developer's
own desktop — the exact privacy problem this layer exists to guard.
"""

from __future__ import annotations

import io
import types
from collections.abc import Iterator, Mapping, Sequence
from typing import cast

import pytest

from jarvis.config.loader import load_defaults
from jarvis.config.schema import OcrSection, VisionSection
from jarvis.core.exceptions import VisionError
from jarvis.llm import (
    ChatMessage,
    ChatResponse,
    GenerationOptions,
    LlmClient,
    LlmError,
    StreamChunk,
)
from jarvis.ocr import OcrBlock, OcrResult, OcrService
from jarvis.vision import MssCapture, PillowCapture, ScreenCapture, Screenshot, VisionService
from tests._fakes import FakeLlmClient


def vision_section(**overrides: object) -> VisionSection:
    raw = dict(cast(Mapping[str, object], load_defaults()["vision"]))
    raw.update(overrides)
    return VisionSection.from_mapping(raw)


def ocr_section(**overrides: object) -> OcrSection:
    raw = dict(cast(Mapping[str, object], load_defaults()["ocr"]))
    raw.update(overrides)
    return OcrSection.from_mapping(raw)


class _FakeRecognizer:
    """A :class:`~jarvis.ocr.TextRecognizer` returning a fixed string."""

    def __init__(self, text: str = "屏幕上的文字") -> None:
        self._text = text
        self.calls: list[bytes] = []

    @property
    def name(self) -> str:
        return "fake-ocr"

    def recognize(self, image: bytes) -> OcrResult:
        self.calls.append(image)
        return OcrResult(
            blocks=(
                OcrBlock(
                    text=self._text,
                    confidence=0.9,
                    box=((0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0)),
                ),
            ),
            engine=self.name,
            elapsed_ms=1,
        )


def _ocr_service(text: str = "屏幕上的文字") -> OcrService:
    service = OcrService(lambda: ocr_section(enabled=True), engine=_FakeRecognizer(text))
    service.start()
    return service


class FakeCapture:
    """A :class:`~jarvis.vision.ScreenCapture` that returns a canned PNG."""

    def __init__(
        self,
        image: bytes = b"\x89PNG\r\n\x1a\n-fake",
        *,
        width: int = 8,
        height: int = 4,
    ) -> None:
        self._image = image
        self._width = width
        self._height = height
        self.calls: list[int] = []

    @property
    def name(self) -> str:
        return "fake"

    def capture(self, monitor: int) -> Screenshot:
        self.calls.append(monitor)
        return Screenshot(
            image=self._image,
            width=self._width,
            height=self._height,
            monitor=monitor,
            captured_at="2026-01-01T00:00:00+00:00",
        )


class _FailingLlm:
    """An :class:`LlmClient` whose calls always fail, for the degrade path."""

    @property
    def provider_name(self) -> str:
        return "failing"

    @property
    def model(self) -> str:
        return "failing"

    def complete(
        self, messages: Sequence[ChatMessage], *, options: GenerationOptions | None = None
    ) -> ChatResponse:
        raise LlmError("模型不可用")

    def stream(
        self, messages: Sequence[ChatMessage], *, options: GenerationOptions | None = None
    ) -> Iterator[StreamChunk]:
        return iter(())


def make_service(
    *,
    enabled: bool = True,
    backend: str = "mss",
    max_image_mb: int = 8,
    capture: ScreenCapture | None = None,
    ocr: OcrService | None = None,
    llm: LlmClient | None = None,
) -> VisionService:
    section = vision_section(enabled=enabled, backend=backend, max_image_mb=max_image_mb)
    return VisionService(
        lambda: section,
        ocr_provider=(lambda: ocr) if ocr is not None else None,
        llm_provider=(lambda: llm) if llm is not None else None,
        capture=capture,
    )


class TestScreenshot:
    def test_to_dict_omits_the_pixels(self) -> None:
        """A multi-megabyte blob in every log line is slow and a privacy leak."""
        shot = Screenshot(
            image=b"x" * 32, width=8, height=4, monitor=0, captured_at="2026-01-01T00:00:00+00:00"
        )
        payload = shot.to_dict()
        assert "image" not in payload
        assert payload["bytes"] == 32
        assert payload["width"] == 8
        assert payload["monitor"] == 0


class TestVisionService:
    def test_capture_refused_when_disabled(self) -> None:
        """A screenshot is never implicit; disabled must refuse, not return black."""
        service = make_service(enabled=False, capture=FakeCapture())
        service.start()
        assert service.running is False
        with pytest.raises(VisionError, match="未启用"):
            service.capture()

    def test_capture_returns_screenshot_and_counts(self) -> None:
        capture = FakeCapture()
        service = make_service(capture=capture)
        service.start()
        shot = service.capture(1)
        assert shot.monitor == 1
        assert capture.calls == [1]
        assert service.stats()["captures"] == 1

    def test_start_is_idempotent(self) -> None:
        service = make_service(capture=FakeCapture())
        service.start()
        service.start()
        assert service.running is True

    def test_stop_releases_the_capture(self) -> None:
        service = make_service(capture=FakeCapture())
        service.start()
        service.stop()
        assert service.running is False
        with pytest.raises(VisionError, match="未启动"):
            service.capture()

    def test_backend_mss_is_selected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        built: list[str] = []

        def factory() -> FakeCapture:
            built.append("mss")
            return FakeCapture()

        monkeypatch.setattr("jarvis.vision.service.MssCapture", factory)
        service = make_service(backend="mss")
        service.start()
        assert built == ["mss"]
        assert service.running is True

    def test_backend_pillow_is_selected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        built: list[str] = []

        def factory() -> FakeCapture:
            built.append("pillow")
            return FakeCapture()

        monkeypatch.setattr("jarvis.vision.service.PillowCapture", factory)
        service = make_service(backend="pillow")
        service.start()
        assert built == ["pillow"]

    def test_missing_backend_dependency_degrades_start(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """JARVIS must still boot without mss; the failure surfaces on use."""

        def boom(name: str) -> object:
            raise ImportError(f"no module named {name!r}")

        monkeypatch.setattr(
            "jarvis.vision.capture.importlib", types.SimpleNamespace(import_module=boom)
        )
        service = make_service(backend="mss")
        service.start()  # must not raise
        assert service.running is False
        with pytest.raises(VisionError, match="mss"):
            service.capture()

    def test_oversized_image_is_refused(self) -> None:
        """A frame past ``max_image_mb`` is refused, not silently truncated."""
        oversized = b"x" * (1024 * 1024 + 1)
        service = make_service(max_image_mb=1, capture=FakeCapture(image=oversized))
        service.start()
        with pytest.raises(VisionError, match="过大"):
            service.capture()

    def test_read_text_returns_frame_and_text(self) -> None:
        ocr = _ocr_service("屏幕上写着你好")
        service = make_service(capture=FakeCapture(), ocr=ocr)
        service.start()
        shot, result = service.read_text()
        assert isinstance(shot, Screenshot)
        assert result.text == "屏幕上写着你好"
        assert service.stats()["reads"] == 1

    def test_read_text_without_ocr_provider_raises(self) -> None:
        service = make_service(capture=FakeCapture())
        service.start()
        with pytest.raises(VisionError, match="OCR"):
            service.read_text()

    def test_describe_without_llm_returns_raw_text_and_warns(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Degrading beats failing: the user still gets something readable."""
        service = make_service(capture=FakeCapture(), ocr=_ocr_service("原始文字"))
        service.start()
        with caplog.at_level("WARNING", logger="jarvis.vision.service"):
            summary = service.describe()
        assert summary == "原始文字"
        assert "LLM" in caplog.text

    def test_describe_uses_the_llm(self) -> None:
        llm = FakeLlmClient(reply="屏幕显示的是一个文本编辑器")
        service = make_service(capture=FakeCapture(), ocr=_ocr_service("hello world"), llm=llm)
        service.start()
        assert service.describe() == "屏幕显示的是一个文本编辑器"
        assert llm.calls  # the OCR text was actually handed to the model
        assert service.stats()["describes"] == 1

    def test_describe_degrades_when_the_llm_fails(self, caplog: pytest.LogCaptureFixture) -> None:
        service = make_service(
            capture=FakeCapture(), ocr=_ocr_service("原始文字"), llm=_FailingLlm()
        )
        service.start()
        with caplog.at_level("WARNING", logger="jarvis.vision.service"):
            summary = service.describe()
        assert summary == "原始文字"
        assert "LLM" in caplog.text

    def test_describe_with_no_text_returns_empty(self) -> None:
        service = make_service(capture=FakeCapture(), ocr=_ocr_service(""), llm=FakeLlmClient())
        service.start()
        assert service.describe() == ""

    def test_stats_shape(self) -> None:
        service = make_service(capture=FakeCapture())
        service.start()
        stats = service.stats()
        assert set(stats) == {
            "name",
            "running",
            "enabled",
            "backend",
            "max_image_mb",
            "captures",
            "reads",
            "describes",
            "error",
        }
        assert stats["name"] == "vision"
        assert stats["backend"] == "fake"


class _FakeShot:
    def __init__(self, rgb: bytes, size: tuple[int, int]) -> None:
        self.rgb = rgb
        self.size = size


class _FakeSession:
    def __init__(self, monitors: list[dict[str, int]], shot: _FakeShot) -> None:
        self.monitors = monitors
        self._shot = shot

    def __enter__(self) -> _FakeSession:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def grab(self, region: dict[str, int]) -> _FakeShot:
        return self._shot


def _install_mss(
    monkeypatch: pytest.MonkeyPatch,
    *,
    monitors: list[dict[str, int]],
    shot: _FakeShot,
    png: bytes,
) -> None:
    def factory() -> _FakeSession:
        return _FakeSession(monitors, shot)

    module = types.SimpleNamespace(mss=factory)
    tools = types.SimpleNamespace(to_png=lambda rgb, size: png)

    def importer(name: str) -> object:
        if name == "mss":
            return module
        if name == "mss.tools":
            return tools
        raise ImportError(name)

    monkeypatch.setattr(
        "jarvis.vision.capture.importlib", types.SimpleNamespace(import_module=importer)
    )


class TestMssCapture:
    def test_capture_builds_a_png(self, monkeypatch: pytest.MonkeyPatch) -> None:
        png = b"\x89PNG-mss"
        _install_mss(
            monkeypatch,
            monitors=[{"left": 0, "top": 0, "width": 100, "height": 50}],
            shot=_FakeShot(b"\x00" * 12, (100, 50)),
            png=png,
        )
        shot = MssCapture().capture(0)
        assert shot.image == png
        assert (shot.width, shot.height) == (100, 50)
        assert shot.monitor == 0
        assert shot.captured_at  # an ISO timestamp, never blank

    def test_out_of_range_monitor_is_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install_mss(
            monkeypatch,
            monitors=[{"left": 0, "top": 0, "width": 1, "height": 1}],
            shot=_FakeShot(b"", (1, 1)),
            png=b"",
        )
        with pytest.raises(VisionError, match="不存在"):
            MssCapture().capture(5)

    def test_missing_dependency_names_the_package(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Enabling vision without mss must fail with the package name, not a traceback."""

        def boom(name: str) -> object:
            raise ImportError(name)

        monkeypatch.setattr(
            "jarvis.vision.capture.importlib", types.SimpleNamespace(import_module=boom)
        )
        with pytest.raises(VisionError, match="mss"):
            MssCapture()

    def test_satisfies_the_protocol(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install_mss(
            monkeypatch,
            monitors=[{"left": 0, "top": 0, "width": 1, "height": 1}],
            shot=_FakeShot(b"", (1, 1)),
            png=b"",
        )
        assert isinstance(MssCapture(), ScreenCapture)


class _FakePillowImage:
    def __init__(self, size: tuple[int, int], payload: bytes) -> None:
        self.size = size
        self._payload = payload

    def save(self, buffer: io.BytesIO, *, format: str) -> None:
        del format
        buffer.write(self._payload)


def _install_pillow(monkeypatch: pytest.MonkeyPatch, image: _FakePillowImage) -> None:
    def importer(name: str) -> object:
        if name == "PIL.ImageGrab":
            return types.SimpleNamespace(grab=lambda: image)
        raise ImportError(name)

    monkeypatch.setattr(
        "jarvis.vision.capture.importlib", types.SimpleNamespace(import_module=importer)
    )


class TestPillowCapture:
    def test_primary_monitor_captures_a_png(self, monkeypatch: pytest.MonkeyPatch) -> None:
        png = b"\x89PNG-pillow"
        _install_pillow(monkeypatch, _FakePillowImage((20, 10), png))
        shot = PillowCapture().capture(0)
        assert shot.image == png
        assert (shot.width, shot.height) == (20, 10)

    def test_other_monitor_is_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Pillow cannot select a monitor; guessing would capture the wrong one."""
        _install_pillow(monkeypatch, _FakePillowImage((20, 10), b""))
        with pytest.raises(VisionError, match="主显示器"):
            PillowCapture().capture(1)

    def test_missing_dependency_names_the_package(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def boom(name: str) -> object:
            raise ImportError(name)

        monkeypatch.setattr(
            "jarvis.vision.capture.importlib", types.SimpleNamespace(import_module=boom)
        )
        with pytest.raises(VisionError, match="pillow"):
            PillowCapture()
