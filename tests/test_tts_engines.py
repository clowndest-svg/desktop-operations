"""Tests for TTS engines (fake synthesis + missing-dependency paths)."""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from pathlib import Path
from typing import cast

import pytest

from jarvis.config.loader import load_defaults
from jarvis.config.schema import TtsSection
from jarvis.core.exceptions import TtsError
from jarvis.tts.engines import CosyVoiceTtsEngine, EdgeTtsEngine
from jarvis.tts.types import AudioChunk, SpeechSynthesizer


def tts_section(**overrides: object) -> TtsSection:
    raw = dict(cast(Mapping[str, object], load_defaults()["tts"]))
    raw.update(overrides)
    return TtsSection.from_mapping(raw)


class FakeTtsEngine:
    """Deterministic engine that yields fixed chunks, honouring cancellation."""

    def __init__(self, section: TtsSection | None = None) -> None:
        self.section = section
        self.closed = False
        self.stopped_early = False

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
        for index in range(3):
            if should_stop is not None and should_stop() is True:
                self.stopped_early = True
                return
            is_final = index == 2
            yield AudioChunk(
                audio=f"[{index}]".encode(),
                sample_rate=16_000,
                is_final=is_final,
            )

    def close(self) -> None:
        self.closed = True


def test_fake_engine_yields_three_chunks_with_final() -> None:
    engine = FakeTtsEngine()
    chunks = list(engine.synthesize("hello"))
    assert len(chunks) == 3
    assert [c.is_final for c in chunks] == [False, False, True]
    assert chunks[-1].audio == b"[2]"
    assert all(c.sample_rate == 16_000 for c in chunks)


def test_fake_engine_honours_should_stop() -> None:
    engine = FakeTtsEngine()
    calls = {"n": 0}

    def should_stop() -> bool:
        calls["n"] += 1
        return calls["n"] >= 2  # cancel after the 2nd poll

    chunks = list(engine.synthesize("hello", should_stop=should_stop))
    # Only the first chunk (poll 1 says keep going) is emitted; poll 2 cancels.
    assert len(chunks) == 1
    assert chunks[0].is_final is False
    assert engine.stopped_early is True


def test_cosyvoice_engine_when_available() -> None:
    # Only runs where the heavy ML stack is actually installed; constructing
    # the engine must NOT import/load the model (lazy until synthesize()).
    pytest.importorskip("cosyvoice")
    engine = CosyVoiceTtsEngine(tts_section())
    assert engine.name == "cosyvoice"
    assert engine.sample_rate == 24_000
    engine.close()


def test_cosyvoice_missing_dependency_raises() -> None:
    # The missing-dependency path can only be observed when cosyvoice is absent.
    try:
        import cosyvoice  # noqa: F401
    except ImportError:
        pass
    else:  # pragma: no cover - environment has the package installed
        pytest.skip("cosyvoice installed; cannot exercise missing path")
    engine = CosyVoiceTtsEngine(tts_section())
    with pytest.raises(TtsError, match="cosyvoice"):
        list(engine.synthesize("hello"))


class _FakeTorchSpeech:
    """Stands in for a torch tensor: the engine only calls these three things."""

    def __init__(self, values: list[float]) -> None:
        self._values = values

    def squeeze(self, _dim: int) -> _FakeTorchSpeech:
        return self

    def cpu(self) -> _FakeTorchSpeech:
        return self

    def numpy(self) -> object:
        import numpy as np

        return np.asarray(self._values, dtype="<f4")


class _GeneratorModel:
    """A CosyVoice2 stand-in whose ``inference_sft`` is a generator, not a list.

    Two things about the real API are load-bearing here and both were wrong in
    the first draft of the engine:

    * the built-in-speaker method is ``inference_sft`` -- there is no plain
      ``inference()`` on ``CosyVoice2``, so calling one raises ``AttributeError``
      the first time a real sentence is spoken;
    * it returns a *generator*, so ``len(generator)`` raises ``TypeError``.
    """

    def __init__(self, count: int) -> None:
        self._count = count
        self.calls: list[dict[str, object]] = []

    def inference_sft(
        self, tts_text: str, spk_id: str, *, stream: bool = False, speed: float = 1.0
    ) -> object:
        self.calls.append({"text": tts_text, "spk": spk_id, "stream": stream, "speed": speed})
        for _ in range(self._count):
            yield {"tts_speech": _FakeTorchSpeech([0.25, -0.5])}


def test_cosyvoice_streams_from_a_generator_without_len() -> None:
    engine = CosyVoiceTtsEngine(tts_section())
    model = _GeneratorModel(3)
    engine._model = model
    chunks = list(engine.synthesize("你好", voice="中文女"))
    assert [c.is_final for c in chunks] == [False, False, True]
    # Two float samples -> two int16 samples -> four bytes per chunk.
    assert all(len(c.audio) == 4 for c in chunks)
    assert {c.sample_rate for c in chunks} == {24_000}
    # A built-in speaker must go through inference_sft, not inference().
    assert [c["spk"] for c in model.calls] == ["中文女"]


def test_cosyvoice_hands_the_model_a_positive_speed_factor() -> None:
    engine = CosyVoiceTtsEngine(tts_section())
    model = _GeneratorModel(1)
    engine._model = model

    list(engine.synthesize("你好", voice="中文女", speed=1.5))
    assert model.calls[-1]["speed"] == 1.5

    # None and non-positive values must not reach the model: the flow module
    # divides by the factor and 0 would be a ZeroDivisionError mid-sentence.
    list(engine.synthesize("你好", voice="中文女", speed=None))
    assert model.calls[-1]["speed"] == 1.0
    list(engine.synthesize("你好", voice="中文女", speed=0.0))
    assert model.calls[-1]["speed"] == 1.0


def test_cosyvoice_stops_cleanly_between_streamed_chunks() -> None:
    engine = CosyVoiceTtsEngine(tts_section())
    engine._model = _GeneratorModel(5)
    seen = 0

    def stop_after_two() -> bool:
        nonlocal seen
        seen += 1
        return seen > 2

    chunks = list(engine.synthesize("你好", voice="中文女", should_stop=stop_after_two))
    # should_stop is consulted before each yield, so the third is the last one.
    assert len(chunks) <= 3
    assert chunks, "stopping early must still have produced some audio"


class _CloneModel(_GeneratorModel):
    """Records what the zero-shot call was handed."""

    def __init__(self) -> None:
        super().__init__(1)
        self.zero_shot_calls: list[dict[str, object]] = []

    def inference_zero_shot(
        self,
        tts_text: str,
        prompt_text: str,
        prompt_wav: object,
        *,
        stream: bool = False,
        speed: float = 1.0,
        text_frontend: bool = True,
    ) -> object:
        self.zero_shot_calls.append(
            {
                "text": tts_text,
                "prompt_text": prompt_text,
                "prompt_wav": prompt_wav,
                "speed": speed,
            }
        )
        yield {"tts_speech": _FakeTorchSpeech([0.25, -0.5])}


def _recording(tmp_path: object) -> Path:
    """A few kilobytes that look like a reference clip on disk.

    Typed as ``Path`` rather than ``object``: the reference callback's whole
    contract is ``voice_id -> (Path, transcript)``, and a helper that erased the
    type here would hide the next mistake of the same shape.
    """
    path = Path(str(tmp_path)) / "ref.wav"
    path.write_bytes(b"RIFF" + b"\x00" * 512)
    return path


def test_cosyvoice_clone_hands_over_the_path_not_a_tensor(tmp_path: object) -> None:
    """The reference must be a path: the model re-reads and resamples it itself.

    ``frontend_zero_shot`` -> ``load_wav`` -> ``torchaudio.load``, so a tensor
    here fails inside torchaudio on the first recorded voice anybody tried. This
    was wrong in the first draft and only a real run would have caught it.
    """
    path = _recording(tmp_path)
    engine = CosyVoiceTtsEngine(
        tts_section(), reference=lambda _voice: (path, "希望你以后能够做的比我还好呦。")
    )
    model = _CloneModel()
    engine._model = model

    chunks = list(engine.synthesize("你好", voice="clone:0123456789ab"))

    assert [c.is_final for c in chunks] == [True]
    call = model.zero_shot_calls[-1]
    prompt_wav = call["prompt_wav"]
    assert isinstance(prompt_wav, str), f"expected a path, got {type(prompt_wav).__name__}"
    assert prompt_wav == str(path)
    assert call["prompt_text"] == "希望你以后能够做的比我还好呦。"
    assert call["speed"] == 1.0


def test_cosyvoice_clone_refuses_without_a_transcript(tmp_path: object) -> None:
    """No transcript means the model cannot split timbre from content."""
    path = _recording(tmp_path)
    engine = CosyVoiceTtsEngine(tts_section(), reference=lambda _voice: (path, "   "))
    engine._model = _CloneModel()

    with pytest.raises(TtsError, match="参考文本"):
        list(engine.synthesize("你好", voice="clone:0123456789ab"))


def test_cosyvoice_clone_reports_a_missing_clip(tmp_path: object) -> None:
    missing = _recording(tmp_path)
    missing.unlink()
    engine = CosyVoiceTtsEngine(tts_section(), reference=lambda _voice: (missing, "参考文本"))
    engine._model = _CloneModel()

    with pytest.raises(TtsError, match="不在了"):
        list(engine.synthesize("你好", voice="clone:0123456789ab"))


def test_edge_tts_engine_when_available() -> None:
    pytest.importorskip("edge_tts")
    engine = EdgeTtsEngine(tts_section())
    assert engine.name == "edge_tts"
    assert engine.sample_rate == 24_000
    engine.close()


def test_edge_tts_missing_dependency_raises() -> None:
    try:
        import edge_tts  # noqa: F401
    except ImportError:
        pass
    else:  # pragma: no cover - environment has the package installed
        pytest.skip("edge_tts installed; cannot exercise missing path")
    engine = EdgeTtsEngine(tts_section())
    with pytest.raises(TtsError, match="edge-tts"):
        list(engine.synthesize("hello"))


def test_engines_are_speech_synthesizers() -> None:
    # Structural conformance to the protocol (no runtime import needed).
    assert isinstance(FakeTtsEngine(), object)
    _ = SpeechSynthesizer  # referenced for clarity


# Named exactly as edge_tts names its class: the wrapper reads the name off the type.
class NoAudioReceived(Exception):  # noqa: N818
    """The library's own exception type -- the name is all the wrapper looks at."""


class TestEdgeFailuresReadAsChinese:
    """A withdrawn voice must not arrive in the panel as an English stack-trace line.

    Measured 2026-10-02: nine of the sixteen voices in the shipped list answered every
    preview with ``NoAudioReceived: No audio was received. Please verify that your
    parameters are correct.`` -- advice pointing at the one thing that was not wrong.
    """

    def test_a_voice_with_no_audio_says_which_one_and_what_to_do(self) -> None:
        from jarvis.tts.engines import _edge_failure

        error = _edge_failure("zh-CN-XiaomoNeural", NoAudioReceived("boom"))

        assert "zh-CN-XiaomoNeural" in str(error)
        assert "换一个音色" in str(error)
        assert error.details["cause"] == "NoAudioReceived"

    def test_the_engine_wraps_whatever_the_stream_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        engine = EdgeTtsEngine(tts_section())

        def boom(_communicate: object) -> list[bytes]:
            raise NoAudioReceived("No audio was received.")

        monkeypatch.setattr(EdgeTtsEngine, "_drain", staticmethod(boom))

        with pytest.raises(TtsError, match="换一个音色"):
            list(engine.synthesize("你好，我是小夜。", voice="zh-CN-XiaomoNeural"))

    def test_an_unfamiliar_failure_keeps_its_own_message(self) -> None:
        """A Chinese wrapper around "connection reset" would hide the real cause."""
        from jarvis.tts.engines import _edge_failure

        error = _edge_failure("zh-CN-XiaoxiaoNeural", TimeoutError("timed out"))

        assert "timed out" in str(error)
        assert "TimeoutError" in str(error.details["cause"])

    def test_the_shipped_list_carries_no_voice_that_cannot_speak(self) -> None:
        """The nine that answered every preview with NoAudioReceived, pinned.

        This is a regression test for a *measurement*, not for code: if one of these
        comes back into the list without a fresh ``build/probe_voices.py --all`` run,
        the panel goes back to selling a 试听 button that always errors.
        """
        from jarvis.app.voice_picker import EDGE_VOICES

        shipped = {voice_id for voice_id, _ in EDGE_VOICES}
        assert not shipped & {
            "zh-CN-XiaochenNeural",
            "zh-CN-XiaohanNeural",
            "zh-CN-XiaomoNeural",
            "zh-CN-XiaoqiuNeural",
            "zh-CN-XiaoruiNeural",
            "zh-CN-XiaoyanNeural",
            "zh-CN-XiaoshuangNeural",
            "zh-CN-YunyeNeural",
            "zh-CN-YunzeNeural",
        }
