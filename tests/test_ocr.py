"""Tests for the OCR layer (jarvis.ocr).

Everything here runs without ``rapidocr-onnxruntime`` installed: the real
engine's lazy-import path is exercised by monkeypatching ``importlib`` inside
:mod:`jarvis.ocr.engines`, and the service is driven with a fake recogniser.
That keeps the suite honest on a machine that never installed the optional
ONNX stack, which is exactly the machine JARVIS must still boot on.
"""

from __future__ import annotations

import types
from collections.abc import Mapping
from typing import cast

import pytest

from jarvis.config.loader import load_defaults
from jarvis.config.schema import OcrSection
from jarvis.core.exceptions import OcrError, VisionError
from jarvis.ocr import OcrBlock, OcrResult, OcrService, RapidOcrEngine, TextRecognizer


def ocr_section(**overrides: object) -> OcrSection:
    """Build an ``OcrSection`` from the shipped defaults plus overrides."""
    raw = dict(cast(Mapping[str, object], load_defaults()["ocr"]))
    raw.update(overrides)
    return OcrSection.from_mapping(raw)


def _block(text: str, top: float, left: float = 0.0, confidence: float = 0.9) -> OcrBlock:
    return OcrBlock(
        text=text,
        confidence=confidence,
        box=((left, top), (left + 10, top), (left + 10, top + 5), (left, top + 5)),
    )


class FakeOcrEngine:
    """Deterministic recogniser that records the images it was handed."""

    def __init__(self, result: OcrResult | None = None, *, fail: bool = False) -> None:
        self._result = result or OcrResult(
            blocks=(_block("你好", 0.0),), engine="fake", elapsed_ms=3
        )
        self._fail = fail
        self.calls: list[bytes] = []

    @property
    def name(self) -> str:
        return "fake"

    def recognize(self, image: bytes) -> OcrResult:
        self.calls.append(image)
        if self._fail:
            raise OcrError("识别失败")
        return self._result


class ExplodingOcrEngine:
    """An engine that raises a non-JARVIS exception, to prove wrapping."""

    @property
    def name(self) -> str:
        return "boom"

    def recognize(self, image: bytes) -> OcrResult:
        raise ValueError("kaboom")


class _StubRapidOcr:
    """Stands in for the object ``RapidOCR()`` returns: ``__call__(image)``."""

    def __init__(self, raw: object, *, fail: bool = False) -> None:
        self._raw = raw
        self._fail = fail

    def __call__(self, image: bytes) -> tuple[object, list[float]]:
        if self._fail:
            raise RuntimeError("onnx runtime exploded")
        return self._raw, [0.01]


def _install_rapidocr_module(monkeypatch: pytest.MonkeyPatch, recogniser: object) -> None:
    """Make ``importlib.import_module`` in the engine module return a stub."""
    module = types.SimpleNamespace(RapidOCR=lambda: recogniser)
    monkeypatch.setattr(
        "jarvis.ocr.engines.importlib",
        types.SimpleNamespace(import_module=lambda name: module),
    )


def _install_missing_module(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(name: str) -> object:
        raise ImportError(f"no module named {name!r}")

    monkeypatch.setattr("jarvis.ocr.engines.importlib", types.SimpleNamespace(import_module=boom))


class TestOcrResult:
    def test_text_joins_blocks_in_reading_order(self) -> None:
        """The prompt must read top-to-bottom; blocks arrive already ordered."""
        result = OcrResult(
            blocks=(_block("第一行", 0.0), _block("第二行", 10.0)),
            engine="fake",
            elapsed_ms=1,
        )
        assert result.text == "第一行\n第二行"

    def test_blank_blocks_do_not_add_empty_lines(self) -> None:
        """A blank detection would waste a prompt line for no meaning."""
        result = OcrResult(
            blocks=(_block("", 0.0), _block("有字", 10.0)),
            engine="fake",
            elapsed_ms=1,
        )
        assert result.text == "有字"

    def test_empty_result_has_empty_text(self) -> None:
        assert OcrResult(blocks=(), engine="fake", elapsed_ms=0).text == ""

    def test_to_dict_is_json_ready(self) -> None:
        block = _block("你好", 0.0)
        payload = OcrResult(blocks=(block,), engine="fake", elapsed_ms=7).to_dict()
        assert payload["text"] == "你好"
        assert payload["engine"] == "fake"
        assert payload["elapsed_ms"] == 7
        blocks = cast(list[dict[str, object]], payload["blocks"])
        assert blocks[0]["text"] == "你好"
        assert isinstance(blocks[0]["box"], list)


class TestRapidOcrEngine:
    def test_missing_dependency_names_the_package(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A user who enabled OCR needs to know exactly what to install."""
        _install_missing_module(monkeypatch)
        with pytest.raises(OcrError, match="rapidocr-onnxruntime"):
            RapidOcrEngine(languages=("ch",), min_confidence=0.5)

    def test_min_confidence_filters_noise(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Low-confidence OCR noise in a prompt is worse than a missing line."""
        raw = [
            [[[0, 0], [10, 0], [10, 5], [0, 5]], "确定", 0.95],
            [[[0, 10], [10, 10], [10, 15], [0, 15]], "噪声", 0.2],
        ]
        _install_rapidocr_module(monkeypatch, _StubRapidOcr(raw))
        engine = RapidOcrEngine(languages=("ch",), min_confidence=0.5)
        result = engine.recognize(b"png-bytes")
        assert [block.text for block in result.blocks] == ["确定"]

    def test_recognize_reports_engine_and_elapsed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        raw = [[[[0, 0], [1, 0], [1, 1], [0, 1]], "x", 0.9]]
        _install_rapidocr_module(monkeypatch, _StubRapidOcr(raw))
        result = RapidOcrEngine(languages=("ch",), min_confidence=0.0).recognize(b"png")
        assert result.engine == "rapidocr"
        assert result.elapsed_ms >= 0
        assert result.blocks[0].box == ((0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0))

    def test_empty_image_is_refused_before_inference(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install_rapidocr_module(monkeypatch, _StubRapidOcr([]))
        with pytest.raises(OcrError, match="空图像"):
            RapidOcrEngine(languages=("ch",), min_confidence=0.0).recognize(b"")

    def test_inference_failure_is_wrapped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install_rapidocr_module(monkeypatch, _StubRapidOcr([], fail=True))
        engine = RapidOcrEngine(languages=("ch",), min_confidence=0.0)
        with pytest.raises(OcrError, match="识别失败"):
            engine.recognize(b"png")

    def test_malformed_entries_are_skipped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """One bad detection must not lose the rest of a page of good text."""
        raw = ["not-a-triple", [[[0, 0], [1, 0], [1, 1], [0, 1]], "good", 0.9]]
        _install_rapidocr_module(monkeypatch, _StubRapidOcr(raw))
        result = RapidOcrEngine(languages=("ch",), min_confidence=0.0).recognize(b"png")
        assert [block.text for block in result.blocks] == ["good"]

    def test_satisfies_the_protocol(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install_rapidocr_module(monkeypatch, _StubRapidOcr([]))
        engine = RapidOcrEngine(languages=("ch",), min_confidence=0.0)
        assert isinstance(engine, TextRecognizer)


class TestOcrService:
    def test_disabled_service_does_not_build_an_engine(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Disabled means the heavy engine is never constructed at all."""
        built: list[object] = []

        def spy(**kwargs: object) -> object:
            built.append(kwargs)
            return FakeOcrEngine()

        monkeypatch.setattr("jarvis.ocr.service.RapidOcrEngine", spy)
        service = OcrService(lambda: ocr_section(enabled=False))
        service.start()
        assert built == []
        assert service.running is False

    def test_disabled_service_refuses_recognition(self) -> None:
        service = OcrService(lambda: ocr_section(enabled=False))
        service.start()
        with pytest.raises(OcrError, match="未启用"):
            service.recognize(b"png")

    def test_start_is_idempotent(self) -> None:
        service = OcrService(lambda: ocr_section(enabled=True), engine=FakeOcrEngine())
        service.start()
        service.start()
        assert service.running is True

    def test_recognize_delegates_and_tracks_stats(self) -> None:
        engine = FakeOcrEngine()
        service = OcrService(lambda: ocr_section(enabled=True), engine=engine)
        service.start()
        result = service.recognize(b"png-bytes")
        assert result.text == "你好"
        assert engine.calls == [b"png-bytes"]
        stats = service.stats()
        assert stats["requests"] == 1
        assert stats["blocks"] == 1
        assert stats["running"] is True

    def test_recognize_before_start_raises(self) -> None:
        service = OcrService(lambda: ocr_section(enabled=True))
        with pytest.raises(OcrError, match="未启动"):
            service.recognize(b"png")

    def test_missing_dependency_degrades_start_but_reports_on_use(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """JARVIS must still boot offline; the failure belongs at first use."""
        _install_missing_module(monkeypatch)
        service = OcrService(lambda: ocr_section(enabled=True))
        service.start()  # must not raise
        assert service.running is False
        with pytest.raises(OcrError, match="rapidocr-onnxruntime"):
            service.recognize(b"png")
        assert "rapidocr-onnxruntime" in cast(str, service.stats()["error"])

    def test_engine_failure_is_wrapped(self) -> None:
        service = OcrService(lambda: ocr_section(enabled=True), engine=ExplodingOcrEngine())
        service.start()
        with pytest.raises(OcrError, match="识别失败"):
            service.recognize(b"png")

    def test_engine_ocrorror_is_passed_through(self) -> None:
        service = OcrService(lambda: ocr_section(enabled=True), engine=FakeOcrEngine(fail=True))
        service.start()
        with pytest.raises(OcrError, match="识别失败"):
            service.recognize(b"png")

    def test_ocr_error_is_a_vision_error(self) -> None:
        """The vision layer catches ``OcrError`` as a ``VisionError``."""
        assert issubclass(OcrError, VisionError)

    def test_stop_is_idempotent(self) -> None:
        service = OcrService(lambda: ocr_section(enabled=True), engine=FakeOcrEngine())
        service.start()
        service.stop()
        service.stop()
        assert service.running is False

    def test_stats_shape(self) -> None:
        service = OcrService(lambda: ocr_section(enabled=True), engine=FakeOcrEngine())
        service.start()
        stats = service.stats()
        assert set(stats) == {
            "name",
            "running",
            "enabled",
            "engine",
            "languages",
            "min_confidence",
            "requests",
            "blocks",
            "last_elapsed_ms",
            "error",
        }
        assert stats["name"] == "ocr"
        assert stats["engine"] == "fake"
        assert stats["languages"] == ["ch", "en"]
