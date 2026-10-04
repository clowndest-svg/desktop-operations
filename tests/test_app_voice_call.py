"""One spoken turn from a phone, without a phone, a microphone or a model.

The tests here are mostly about *what happens when a piece is missing*, because
that is the part a user experiences: "she went quiet", "she answered but said
nothing", "the first sentence took forty seconds". The happy path is one test;
the rest are the refusals, each of which has to arrive as a sentence rather than
as an exception.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from jarvis.app.voice_call import MAX_SPOKEN_CHARS, VoiceCall, _cap_spoken
from jarvis.config.schema import AsrSection, TtsSection
from jarvis.tts.types import AudioChunk


def _asr_section() -> AsrSection:
    return AsrSection(
        enabled=True,
        engine="sensevoice",
        model="",
        device="cpu",
        language="zh",
        temperature=0.0,
        beam_size=1,
    )


def _tts_section(engine: str = "edge_tts", voice: str = "zh-CN-XiaoxiaoNeural") -> TtsSection:
    return TtsSection(
        enabled=True,
        engine=engine,
        voice=voice,
        speed=1.0,
        volume=1.0,
        device="cpu",
        model="",
    )


class _StubRecognizer:
    """Stands in for SenseVoiceAsrEngine."""

    name = "stub-asr"

    def __init__(self, text: str = "今天天气怎么样", *, raises: Exception | None = None) -> None:
        self.text = text
        self.raises = raises
        self.calls = 0
        self.closed = 0

    def recognize(self, pcm: bytes) -> Any:
        self.calls += 1
        if self.raises is not None:
            raise self.raises
        return type("Result", (), {"text": self.text})()

    def close(self) -> None:
        self.closed += 1


class _StubReply:
    def __init__(self, answer: str, error: str = "") -> None:
        self.answer = answer
        self.error = error


class _StubChat:
    def __init__(
        self, answer: str = "今天晴，气温 18 度", error: str = "", raises: Exception | None = None
    ) -> None:
        self.answer = answer
        self.error = error
        self.raises = raises
        self.asked: list[str] = []

    def ask(self, question: str) -> _StubReply:
        self.asked.append(question)
        if self.raises is not None:
            raise self.raises
        return _StubReply(self.answer, self.error)


class _StubPicker:
    def __init__(
        self, voice: str = "zh-CN-XiaoxiaoNeural", speed: float = 1.25, volume: float = 0.8
    ) -> None:
        self.voice = voice
        self.speed = speed
        self.volume = volume

    def effective_voice(self) -> str:
        return self.voice

    def effective_speed(self) -> float:
        return self.speed

    def effective_volume(self) -> float:
        return self.volume


class _StubLibrary:
    """A voice library with the two fields the engine choice actually reads.

    ``cloud`` decides whether a clone is spoken by the vendor or by the local
    sidecar, so it is the interesting parameter rather than a detail: the same
    voice id, recorded on the same machine, runs on a different engine depending
    on whether it was ever uploaded.
    """

    def __init__(self, cloned: set[str] | None = None, *, cloud: str = "") -> None:
        self.cloned = cloned or set()
        self.cloud = cloud

    def is_clone(self, voice_id: str) -> bool:
        return voice_id in self.cloned

    def resolve(self, voice_id: str) -> Any:
        if voice_id not in self.cloned:
            return None
        from jarvis.app.voice_library import ClonedVoice

        return ClonedVoice(
            voice_id=voice_id,
            name="我自己",
            prompt_text="希望你以后能够做的比我还好呦。",
            sample_rate=16_000,
            duration_ms=5_000,
            created_at="2026-10-03T00:00:00",
            ref_path=Path("E:/tmp/ref.wav"),
            cloud_voice=self.cloud,
            cloud_model="qwen3-tts-vc-realtime" if self.cloud else "",
        )


class _StubEngine:
    def __init__(self, *, chunks: int = 2, raises: Exception | None = None) -> None:
        self.chunks = chunks
        self.raises = raises
        self.calls: list[tuple[str, str | None, float | None, float | None]] = []
        self.closed = 0

    def synthesize(
        self,
        text: str,
        *,
        voice: str | None = None,
        speed: float | None = None,
        volume: float | None = None,
        should_stop: Any = None,
    ) -> Any:
        self.calls.append((text, voice, speed, volume))
        if self.raises is not None:
            raise self.raises
        for index in range(self.chunks):
            yield AudioChunk(
                audio=b"\x01\x02" * 10, sample_rate=24_000, is_final=index == self.chunks - 1
            )

    def close(self) -> None:
        self.closed += 1


def _call(
    *,
    recognizer: Any = None,
    chat: Any = None,
    picker: Any = None,
    library: Any = None,
    asr_engine: Any = None,
) -> VoiceCall:
    """A VoiceCall whose recognizer never touches the real one."""
    call = VoiceCall(
        asr_section=_asr_section,
        tts_section=_tts_section,
        chat=chat,
        picker=picker,
        library=library,
    )
    if recognizer is not None:
        call._asr = recognizer
    elif asr_engine is not None:
        call._asr = asr_engine
    return call


# -- readiness -----------------------------------------------------------------


def test_readiness_reports_everything_the_call_screen_needs() -> None:
    call = _call(recognizer=_StubRecognizer(), chat=_StubChat(), library=_StubLibrary())
    state = call.readiness()

    assert state["asr"] == "sensevoice"
    assert state["tts"] == "edge_tts"
    assert state["asr_loaded"] is True
    assert state["chat"] is True
    assert state["cloning"] is True
    assert state["asr_error"] == ""


def test_a_process_without_a_chat_service_says_so() -> None:
    call = _call(recognizer=_StubRecognizer())
    assert call.readiness()["chat"] is False


def test_warmup_starts_a_load_without_blocking(monkeypatch: pytest.MonkeyPatch) -> None:
    """The phone asks for this while the user is still raising it to their ear.

    Blocking here would move the forty-second model load onto the call screen's
    opening animation.
    """
    import threading

    started = threading.Event()
    release = threading.Event()

    def fake_load(self: VoiceCall) -> Any:
        started.set()
        release.wait(timeout=5)
        return None

    monkeypatch.setattr(VoiceCall, "_load_asr", fake_load)
    call = _call()
    state = call.warmup()

    assert started.wait(timeout=5), "warmup must actually start the load"
    assert state["asr_loaded"] is False, "warmup returns before the load finishes"
    release.set()


def test_warmup_is_a_no_op_once_the_recognizer_is_up() -> None:
    call = _call(recognizer=_StubRecognizer())
    assert call.warmup()["asr_loaded"] is True


def test_warmup_does_not_retry_a_failure() -> None:
    """A missing optional package will be missing next time too."""
    call = _call()
    call._asr_error = "ImportError: no funasr"
    assert call.warmup()["asr_error"] == "ImportError: no funasr"


# -- transcription -------------------------------------------------------------


def test_a_wrong_sample_rate_is_refused_with_the_number_that_arrived() -> None:
    call = _call(recognizer=_StubRecognizer())
    text, error = call.transcribe(b"\x00\x00" * 100, 44_100)

    assert text == ""
    assert "16kHz" in error and "44100Hz" in error


def test_no_audio_is_refused() -> None:
    call = _call(recognizer=_StubRecognizer())
    assert call.transcribe(b"", 16_000) == ("", "没收到录音")


def test_an_endless_sentence_is_refused_before_the_model_is_touched() -> None:
    """A payload past the request-body limit is a 413 the phone cannot explain."""
    call = _call(recognizer=_StubRecognizer())
    text, error = call.transcribe(b"\x00\x00" * (16_000 * 40), 16_000)

    assert text == ""
    assert "太长" in error


def test_a_load_failure_is_reported_as_a_sentence() -> None:
    call = _call()
    monkeypatch_import = "jarvis.asr.engines"
    import sys

    # Force the lazy import to fail the way a missing optional package does.
    sys.modules.pop(monkeypatch_import, None)
    original = sys.modules.get(monkeypatch_import)
    sys.modules[monkeypatch_import] = None  # type: ignore[assignment]
    try:
        text, error = call.transcribe(b"\x00\x00" * 100, 16_000)
    finally:
        if original is None:
            sys.modules.pop(monkeypatch_import, None)
        else:
            sys.modules[monkeypatch_import] = original

    assert text == ""
    assert error, "a recognizer that will not load must explain itself"


def test_control_tokens_do_not_reach_the_agent() -> None:
    """SenseVoice wraps its output in ``<|zh|><|NEUTRAL|>`` style markers.

    A leftover pair of angle brackets is not a sentence, and sending it to the
    model as one gets an answer about the brackets.
    """
    call = _call(recognizer=_StubRecognizer("<|zh|><|NEUTRAL|>打开记事本"))
    text, error = call.transcribe(b"\x00\x00" * 100, 16_000)

    assert error == ""
    assert text == "打开记事本"


def test_a_recognizer_that_throws_is_reported_not_propagated() -> None:
    call = _call(recognizer=_StubRecognizer(raises=RuntimeError("onnx exploded")))
    text, error = call.transcribe(b"\x00\x00" * 100, 16_000)

    assert text == ""
    assert "RuntimeError" in error and "onnx exploded" in error


# -- the whole turn ------------------------------------------------------------


def test_a_turn_hears_answers_and_speaks(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = _StubEngine(chunks=2)
    monkeypatch.setattr("jarvis.tts.engines.EdgeTtsEngine", lambda section: engine)
    chat = _StubChat(answer="今天晴，气温 18 度")
    call = _call(recognizer=_StubRecognizer("今天天气怎么样"), chat=chat, picker=_StubPicker())

    result = call.turn(b"\x00\x00" * 16_000, 16_000)

    assert result["heard"] == "今天天气怎么样"
    assert result["answer"] == "今天晴，气温 18 度"
    assert result["error"] == ""
    assert result["sample_rate"] == 24_000
    assert isinstance(result["audio"], bytes) and result["audio"]
    assert chat.asked == ["今天天气怎么样"], "the spoken question reaches the one brain"
    assert engine.closed == 1, "the synthesizer is released after the sentence"


def test_the_sliders_apply_to_what_a_phone_hears(monkeypatch: pytest.MonkeyPatch) -> None:
    """A phone call that ignored the rate and volume sliders would be a second
    voice setting that disagrees with the panel."""
    engine = _StubEngine()
    monkeypatch.setattr("jarvis.tts.engines.EdgeTtsEngine", lambda section: engine)
    call = _call(
        recognizer=_StubRecognizer(), chat=_StubChat(), picker=_StubPicker(speed=1.5, volume=0.25)
    )

    call.turn(b"\x00\x00" * 16_000, 16_000)

    _text, _voice, speed, volume = engine.calls[0]
    assert speed == 1.5
    assert volume == 0.25


def test_a_silent_transcript_asks_the_user_to_repeat() -> None:
    call = _call(recognizer=_StubRecognizer("   "), chat=_StubChat())
    result = call.turn(b"\x00\x00" * 16_000, 16_000)

    assert result["heard"] == ""
    assert result["error"] == "没听清，再说一次"
    assert result["audio"] is None


def test_a_chat_failure_still_reports_what_was_heard(monkeypatch: pytest.MonkeyPatch) -> None:
    """ "She heard you but could not answer" is a different failure from silence."""
    call = _call(recognizer=_StubRecognizer("你好"), chat=_StubChat(raises=RuntimeError("no key")))
    result = call.turn(b"\x00\x00" * 16_000, 16_000)

    assert result["heard"] == "你好"
    assert "RuntimeError" in str(result["error"])
    assert result["audio"] is None


def test_a_synthesis_failure_still_reports_the_text_that_would_be_spoken(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The phone shows the answer either way.

    "She went quiet" is only confusing when the screen is blank too.
    """
    engine = _StubEngine(raises=RuntimeError("no network"))
    monkeypatch.setattr("jarvis.tts.engines.EdgeTtsEngine", lambda section: engine)
    call = _call(recognizer=_StubRecognizer(), chat=_StubChat(answer="好的"), picker=_StubPicker())

    result = call.turn(b"\x00\x00" * 16_000, 16_000)

    assert result["answer"] == "好的"
    assert result["spoken"] == "好的"
    assert result["audio"] is None
    assert result["error"] == "", "an unspoken answer is not a failed answer"


def test_a_process_without_a_brain_refuses_in_words() -> None:
    call = _call(recognizer=_StubRecognizer())
    result = call.turn(b"\x00\x00" * 16_000, 16_000)

    assert result["heard"] == "今天天气怎么样"
    assert "没有接对话服务" in str(result["error"])


def test_every_turn_result_has_the_same_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    """The phone renders one shape. A missing key and an empty one look identical
    on screen and completely different in the console."""
    monkeypatch.setattr("jarvis.tts.engines.EdgeTtsEngine", lambda section: _StubEngine())
    good = _call(recognizer=_StubRecognizer(), chat=_StubChat(), picker=_StubPicker()).turn(
        b"\x00\x00" * 16_000, 16_000
    )
    bad = _call(recognizer=_StubRecognizer(), chat=_StubChat()).turn(b"\x00\x00" * 16_000, 44_100)

    assert set(good) == set(bad)


# -- which engine speaks -------------------------------------------------------


def test_a_recorded_voice_picks_the_offline_engine(monkeypatch: pytest.MonkeyPatch) -> None:
    """A recording with no cloud copy is spoken by the sidecar.

    The *voice* decides the engine, not ``tts.engine``: a built-in Edge voice
    exists only in the cloud engine and a recording exists only offline, so
    choosing from config would send a clone id to Edge, which has never heard
    of it.

    The offline engine is the *sidecar*, not ``CosyVoiceTtsEngine``: the
    in-process class cannot be imported into this environment at all (CosyVoice
    pins ``transformers<4.52`` and this one has 5.x), so the clone path drives a
    separate interpreter. The signature differs by design -- the sidecar takes
    the model path positionally and the reference by keyword -- which is why this
    test names the class instead of matching on shape.

    ``cloud=""`` is what makes this the offline case. See the next test for the
    other half of the rule.
    """
    seen: dict[str, Any] = {}

    class FakeSidecar:
        def __init__(self, model_dir: str, *, reference: Any = None, **kwargs: Any) -> None:
            seen["model_dir"] = model_dir
            seen["reference"] = reference

    monkeypatch.setattr("jarvis.tts.sidecar.CosyVoiceSidecar", FakeSidecar)
    call = _call(
        library=_StubLibrary({"clone:aaaaaaaaaaaa"}), picker=_StubPicker("clone:aaaaaaaaaaaa")
    )

    engine = call._engine_for("clone:aaaaaaaaaaaa")
    assert isinstance(engine, FakeSidecar)
    assert "reference" in seen, "a clone must be handed a way to find its recording"
    assert seen["reference"] is not None
    # The sidecar is constructed from the *model path*, not the section object:
    # it lives in another interpreter and can never see this process's config.
    assert "model_dir" in seen


def test_an_uploaded_voice_is_spoken_by_the_cloud(monkeypatch: pytest.MonkeyPatch) -> None:
    """A recording *with* a cloud copy goes to the realtime engine instead.

    This is the choice the whole cloud path exists for, and it is a trade rather
    than a preference: measured on this machine's GPU, the offline engine needs
    16 seconds to produce its first chunk and runs at about a quarter of real
    time. A phone call cannot wait that long, so a voice that has been uploaded
    is answered in a few hundred milliseconds -- and a voice that has not is
    still spoken locally, privately and for free.
    """
    acquired: dict[str, Any] = {}

    class FakeRealtime:
        @classmethod
        def acquire(cls, **kwargs: Any) -> Any:
            acquired.update(kwargs)
            return cls()

    monkeypatch.setattr("jarvis.tts.cloud_voices.QwenRealtimeVoice", FakeRealtime)
    call = _call(
        library=_StubLibrary({"clone:aaaaaaaaaaaa"}, cloud="xyvoiceSelf01"),
        picker=_StubPicker("clone:aaaaaaaaaaaa"),
    )

    engine = call._engine_for("clone:aaaaaaaaaaaa")

    assert isinstance(engine, FakeRealtime)
    assert acquired["voice"] == "xyvoiceSelf01", "the vendor's id, not the clone id"
    assert (
        acquired["model"] == "qwen3-tts-vc-realtime"
    ), "the model the voice was enrolled for, not a constant"


def test_a_builtin_voice_uses_the_cloud_engine(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeEdge:
        def __init__(self, section: TtsSection) -> None:
            self.section = section

    monkeypatch.setattr("jarvis.tts.engines.EdgeTtsEngine", FakeEdge)
    call = _call(library=_StubLibrary({"clone:aaaaaaaaaaaa"}), picker=_StubPicker())

    engine = call._engine_for("zh-CN-XiaoxiaoNeural")
    assert isinstance(engine, FakeEdge)


def test_without_a_library_every_voice_goes_to_the_cloud_engine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No library means cloning is disabled for this process, not that everything
    that looks like a clone id becomes one."""

    class FakeEdge:
        def __init__(self, section: TtsSection) -> None:
            self.section = section

    monkeypatch.setattr("jarvis.tts.engines.EdgeTtsEngine", FakeEdge)
    call = _call(picker=_StubPicker())

    assert isinstance(call._engine_for("clone:aaaaaaaaaaaa"), FakeEdge)


# -- reading a long answer out loud --------------------------------------------


def test_a_short_answer_is_spoken_whole() -> None:
    text, truncated = _cap_spoken("今天晴。")
    assert text == "今天晴。"
    assert truncated is False


def test_a_long_answer_is_cut_at_a_sentence_not_mid_word() -> None:
    """Cutting mid-word is what makes a truncated answer sound broken; cutting at
    a sentence is what makes it sound like she chose to stop."""
    sentence = "这是一句话。" * 60
    text, truncated = _cap_spoken(sentence)

    assert truncated is True
    assert len(text) <= MAX_SPOKEN_CHARS
    assert text.endswith("。"), "the cut lands on a sentence boundary"


def test_a_wall_of_text_with_no_punctuation_is_still_capped() -> None:
    """Mega bytes over the wire is worse than a hard cut."""
    text, truncated = _cap_spoken("啊" * (MAX_SPOKEN_CHARS * 3))
    assert truncated is True
    assert len(text) <= MAX_SPOKEN_CHARS


def test_a_boundary_too_close_to_the_start_is_ignored() -> None:
    """An answer that opens with 「好。」 then rambles must not be read as one word."""
    text = "好。" + "啊" * (MAX_SPOKEN_CHARS * 2)
    spoken, truncated = _cap_spoken(text)

    assert truncated is True
    assert len(spoken) > 2, "a two-character reading is not an answer"


def test_speak_returns_the_same_shape_as_a_turn(monkeypatch: pytest.MonkeyPatch) -> None:
    """One reply type renders both "she answered you" and "read that to me"."""
    monkeypatch.setattr("jarvis.tts.engines.EdgeTtsEngine", lambda section: _StubEngine())
    call = _call(picker=_StubPicker())
    spoken = call.speak("念这一句")

    assert set(spoken) == set(
        _call(recognizer=_StubRecognizer(), chat=_StubChat()).turn(b"\x00\x00" * 16_000, 16_000)
    )
    assert spoken["spoken"] == "念这一句"
    assert spoken["heard"] == ""
    assert spoken["answer"] == ""


def test_speak_of_an_empty_string_produces_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("jarvis.tts.engines.EdgeTtsEngine", lambda section: _StubEngine())
    call = _call(picker=_StubPicker())
    spoken = call.speak("   ")

    assert spoken["spoken"] == ""
    assert spoken["audio"] is None


def test_stop_closes_the_recognizer_and_clears_it() -> None:
    recognizer = _StubRecognizer()
    call = _call(recognizer=recognizer)
    call.stop()

    assert recognizer.closed == 1
    assert call.readiness()["asr_loaded"] is False


def test_stop_is_safe_when_nothing_was_loaded() -> None:
    _call().stop()
