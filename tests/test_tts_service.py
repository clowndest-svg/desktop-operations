"""Tests for TtsService lifecycle (fake engine, no hardware / native deps)."""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from typing import cast

import pytest

from jarvis.config.loader import load_defaults
from jarvis.config.schema import TtsSection
from jarvis.core.exceptions import TtsError
from jarvis.tts.service import TtsService, TtsSettings, default_engine_factory
from jarvis.tts.types import AudioChunk


def is_running(service: TtsService) -> bool:
    """Read ``service.running`` through a call so mypy cannot narrow the
    property expression to ``Literal[True]`` across ``service.stop()``."""
    return service.running


def tts_section(**overrides: object) -> TtsSection:
    raw = dict(cast(Mapping[str, object], load_defaults()["tts"]))
    raw.update(overrides)
    return TtsSection.from_mapping(raw)


def settings(**overrides: object) -> TtsSettings:
    return TtsSettings(section=tts_section(**overrides))


class FakeTtsEngine:
    """Deterministic engine yielding fixed chunks, honouring cancellation."""

    def __init__(self, section: TtsSection | None = None) -> None:
        self.section = section
        self.closed = False
        self.texts: list[str] = []

    @property
    def name(self) -> str:
        return "fake"

    @property
    def sample_rate(self) -> int:
        return 16_000

    def synthesize(
        self,
        text: str,
        *,
        voice: str | None = None,
        speed: float | None = None,
        volume: float | None = None,
        should_stop: Callable[[], bool] | None = None,
    ) -> Iterator[AudioChunk]:
        del voice, speed, volume
        self.texts.append(text)
        for index in range(3):
            if should_stop is not None and should_stop() is True:
                return
            yield AudioChunk(
                audio=f"[{index}]".encode(),
                sample_rate=16_000,
                is_final=index == 2,
            )

    def close(self) -> None:
        self.closed = True


def test_disabled_service_does_not_load_engine() -> None:
    factory_calls: list[object] = []
    service = TtsService(
        lambda: settings(enabled=False),
        engine_factory=lambda s: factory_calls.append(s) or FakeTtsEngine(s),  # type: ignore[func-returns-value]
    )
    service.start()
    assert not is_running(service)
    assert factory_calls == []  # engine never even constructed
    service.stop()  # must be a safe no-op


def test_enabled_service_loads_engine_synthesizes_and_stops() -> None:
    engine = FakeTtsEngine()
    service = TtsService(
        lambda: settings(enabled=True),
        engine_factory=lambda s: engine,
    )
    service.start()
    assert is_running(service)
    assert not engine.closed

    chunks = list(service.synthesize("hello"))
    assert len(chunks) == 3
    assert chunks[-1].is_final is True

    service.stop()
    assert not is_running(service)
    assert engine.closed


def test_synthesize_before_start_raises() -> None:
    service = TtsService(
        lambda: settings(enabled=False),
        engine_factory=lambda s: FakeTtsEngine(s),
    )
    with pytest.raises(TtsError, match="not loaded"):
        list(service.synthesize("hello"))


def test_synthesize_to_bytes_collects_chunks() -> None:
    engine = FakeTtsEngine()
    service = TtsService(
        lambda: settings(enabled=True),
        engine_factory=lambda s: engine,
    )
    service.start()
    audio, fmt, rate = service.synthesize_to_bytes("hello")
    assert audio == b"[0][1][2]"
    assert fmt == "pcm_s16le"
    assert rate == 16_000
    service.stop()


def test_synthesize_honours_should_stop() -> None:
    engine = FakeTtsEngine()
    service = TtsService(
        lambda: settings(enabled=True),
        engine_factory=lambda s: engine,
    )
    service.start()
    calls = {"n": 0}

    def should_stop() -> bool:
        calls["n"] += 1
        return calls["n"] >= 2  # cancel after the 2nd poll

    chunks = list(service.synthesize("hello", should_stop=should_stop))
    assert len(chunks) == 1  # first chunk emitted, then cancelled
    assert chunks[0].is_final is False
    service.stop()


def test_stop_is_idempotent() -> None:
    service = TtsService(
        lambda: settings(enabled=True),
        engine_factory=lambda s: FakeTtsEngine(s),
    )
    service.start()
    service.stop()
    service.stop()  # second stop must not raise
    assert not is_running(service)


def test_default_engine_factory_unknown_engine_hard_fails() -> None:
    # Schema rejects this at validation time, but the factory is defensive too
    # for any caller that builds a section directly (bypassing validation).
    bad_section = TtsSection(
        enabled=True,
        engine="gpt-sovits",
        voice="x",
        speed=1.0,
        volume=1.0,
        device="cpu",
        model="x",
    )
    with pytest.raises(TtsError, match="unknown tts engine"):
        default_engine_factory(bad_section)


class TestSpeakableBoundary:
    """The voice says words and numbers, never the names of punctuation.

    These pin *where* the reduction happens: at the one waist every utterance
    passes, so the transcript and the screen keep their punctuation while the
    engine receives the speakable form.
    """

    def _service(self, engine: FakeTtsEngine) -> TtsService:
        service = TtsService(lambda: settings(enabled=True), engine_factory=lambda s: engine)
        service.start()
        return service

    def test_engine_receives_the_speakable_form(self) -> None:
        engine = FakeTtsEngine()
        service = self._service(engine)
        list(service.synthesize("CPU：45.0%（12 核），内存 1.5 GB。"))
        service.stop()
        assert engine.texts == ["CPU 百分之45.0 12 核 内存 1.5 GB"]

    def test_numbers_keep_their_separators(self) -> None:
        # "45.0" is forty-five; "45 0" is forty-five and a zero. Same for 3,000
        # and 19:32 -- a sanitizer that shaves these off is a corruption.
        engine = FakeTtsEngine()
        service = self._service(engine)
        list(service.synthesize("19:32 与 3,000 与 45.0"))
        service.stop()
        assert engine.texts == ["19:32 与 3,000 与 45.0"]

    def test_a_reply_with_nothing_speakable_stays_silent(self) -> None:
        engine = FakeTtsEngine()
        service = self._service(engine)
        chunks = list(service.synthesize("** 😀 \n```x = 1```"))
        service.stop()
        assert chunks == []
        assert engine.texts == []


class TestVoiceRouting:
    """The read-aloud path must speak a cloned voice with the engine that can."""

    def test_a_clone_voice_is_handed_to_the_routed_engine(self) -> None:
        default = FakeTtsEngine()
        routed = FakeTtsEngine()
        service = TtsService(
            lambda: settings(enabled=True),
            engine_factory=lambda s: default,
            voice_router=lambda voice: routed if voice.startswith("clone:") else None,
        )
        service.start()

        audio = service.synthesize_to_bytes("你好", voice="clone:104c7ef77bf5")

        assert routed.texts == ["你好"], "克隆音色被喂给了默认引擎（edge-tts 会直接 ValueError）"
        assert default.texts == []
        assert audio[0] != b""

    def test_a_stock_voice_never_leaves_the_loaded_engine(self) -> None:
        default = FakeTtsEngine()
        routed = FakeTtsEngine()
        service = TtsService(
            lambda: settings(enabled=True),
            engine_factory=lambda s: default,
            voice_router=lambda voice: routed if voice.startswith("clone:") else None,
        )
        service.start()

        service.synthesize_to_bytes("你好", voice="zh-CN-XiaoxiaoNeural")

        assert default.texts == ["你好"]
        assert routed.texts == []


class FlakyTtsEngine(FakeTtsEngine):
    """A cloned-voice engine that dies after ``after`` chunks have been handed out."""

    def __init__(self, after: int) -> None:
        super().__init__()
        self._after = after

    def synthesize(
        self,
        text: str,
        *,
        voice: str | None = None,
        speed: float | None = None,
        volume: float | None = None,
        should_stop: Callable[[], bool] | None = None,
    ) -> Iterator[AudioChunk]:
        self.texts.append(text)
        for _index in range(self._after):
            yield AudioChunk(audio=b"x" * 320, sample_rate=16_000, is_final=False)
        raise TtsError("离线语音这次太慢了，超过了等待上限 (花了 12.0 秒，一毫秒音频都没做出来)")


class TestAClonedVoiceMustNotCostTheAnswerItsVoice:
    """ "她回答了但没出声"是这台机器上真发生过的事，也是最难查的一种失败。

    离线复刻实测 8.2–15.8 倍实时：等 120 秒才出声的回答，等于没有回答。
    """

    @staticmethod
    def _service(routed: FakeTtsEngine) -> tuple[TtsService, FakeTtsEngine]:
        default = FakeTtsEngine()
        service = TtsService(
            lambda: settings(enabled=True),
            engine_factory=lambda s: default,
            voice_router=lambda voice: routed if voice.startswith("clone:") else None,
        )
        service.start()
        return service, default

    def test_nothing_audible_yet_falls_back_and_says_why(self) -> None:
        service, default = self._service(FlakyTtsEngine(after=0))

        chunks = list(service.synthesize("你好", voice="clone:25f32b03e146"))

        assert default.texts == ["你好"], "一个字都没出声时，这一句应该改用能出声的声音说"
        assert chunks, "回退之后必须有音频"
        assert "clone:25f32b03e146" in service.voice_fallback
        assert "太慢" in service.voice_fallback

    def test_half_an_answer_is_not_replayed(self) -> None:
        """已经播出去一半再改口，等于把开头说两遍 —— 那时宁可让它停在这里。"""
        service, default = self._service(FlakyTtsEngine(after=1))

        with pytest.raises(TtsError):
            list(service.synthesize("你好", voice="clone:25f32b03e146"))

        assert default.texts == []
        assert service.voice_fallback == ""

    def test_a_stock_voice_failure_is_not_routed_anywhere(self) -> None:
        """普通音色出问题就是引擎出问题，别悄悄换个声音还装作正常。"""
        service, default = self._service(FlakyTtsEngine(after=0))
        default.synthesize = (  # type: ignore[method-assign]
            lambda *a, **k: (_ for _ in ()).throw(TtsError("edge 挂了"))
        )

        with pytest.raises(TtsError):
            list(service.synthesize("你好", voice="zh-CN-XiaoxiaoNeural"))

        assert service.voice_fallback == ""
