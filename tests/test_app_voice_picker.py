"""The voice picker: a list, a choice, and a preview that commits to nothing.

The preview is the part worth pinning hardest. It synthesises through a *separate*
engine instance and never writes the preference, because "let me hear the other
one" must not turn into "why did it change" -- and because a preview that needed
the microphone-enabled voice stack would make the picker useless in exactly the
state most operators first open it in.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import pytest

from jarvis.app.preferences import TTS_SPEED, TTS_VOICE, TTS_VOLUME, Preferences
from jarvis.app.voice_picker import EDGE_VOICES, PREVIEW_TEXT, VoicePicker
from jarvis.config.schema import TtsSection
from jarvis.tts.types import AudioChunk


def _section(engine: str = "edge_tts", voice: str = "zh-CN-XiaoxiaoNeural") -> TtsSection:
    return TtsSection(
        enabled=True,
        engine=engine,
        voice=voice,
        speed=1.0,
        volume=1.0,
        device="cpu",
        model="",
    )


def _section_replacing(*, speed: float = 1.0, volume: float = 1.0) -> TtsSection:
    """The same section with different style values, the way a config file would say it."""
    return TtsSection(
        enabled=True,
        engine="edge_tts",
        voice="zh-CN-XiaoxiaoNeural",
        speed=speed,
        volume=volume,
        device="cpu",
        model="",
    )


def _picker(
    tmp_path: Any,
    *,
    engine: str = "edge_tts",
    emit: Any = None,
) -> tuple[VoicePicker, Preferences]:
    prefs = Preferences(tmp_path / "prefs.json")
    return VoicePicker(lambda: _section(engine), prefs, emit=emit), prefs


class _StubEngine:
    """Stands in for EdgeTtsEngine: two chunks, no network."""

    def __init__(self, section: TtsSection) -> None:
        self.section = section
        self.calls: list[tuple[str, str | None]] = []
        self.styles: list[tuple[float | None, float | None]] = []

    def synthesize(
        self,
        text: str,
        *,
        voice: str | None = None,
        speed: float | None = None,
        volume: float | None = None,
        **_kwargs: Any,
    ) -> Iterator[AudioChunk]:
        self.calls.append((text, voice))
        self.styles.append((speed, volume))
        yield AudioChunk(audio=b"\x01\x02", sample_rate=24_000, is_final=False)
        yield AudioChunk(audio=b"\x03\x04", sample_rate=24_000, is_final=True)

    def close(self) -> None:
        return None


class TestListing:
    def test_every_voice_is_listed_with_the_current_one_marked(self, tmp_path: Any) -> None:
        picker, _ = _picker(tmp_path)

        listing = picker.voices()
        choices = cast("list[dict[str, Any]]", listing["choices"])

        assert listing["error"] == ""
        assert len(choices) == len(EDGE_VOICES)
        marked = [entry for entry in choices if entry["current"]]
        assert [entry["id"] for entry in marked] == ["zh-CN-XiaoxiaoNeural"]

    def test_a_voice_the_list_does_not_carry_is_still_shown_as_current(self, tmp_path: Any) -> None:
        picker, _ = _picker(tmp_path)
        picker._section = lambda: _section(voice="zh-CN-SomeCustomNeural")

        listing = picker.voices()
        choices = cast("list[dict[str, Any]]", listing["choices"])

        assert listing["current"] == "zh-CN-SomeCustomNeural"
        assert choices[0]["id"] == "zh-CN-SomeCustomNeural"

    def test_an_offline_engine_says_why_there_is_no_list(self, tmp_path: Any) -> None:
        picker, _ = _picker(tmp_path, engine="cosyvoice")

        listing = picker.voices()

        assert listing["error"]
        assert listing["choices"] == []


class TestPicking:
    def test_picking_writes_the_preference_the_pipeline_reads(self, tmp_path: Any) -> None:
        picker, prefs = _picker(tmp_path)

        listing = picker.pick("zh-CN-YunxiNeural")

        assert listing["error"] == ""
        assert prefs.text(TTS_VOICE) == "zh-CN-YunxiNeural"
        assert picker.effective_voice() == "zh-CN-YunxiNeural"

    def test_an_unknown_voice_is_refused_and_changes_nothing(self, tmp_path: Any) -> None:
        picker, prefs = _picker(tmp_path)

        listing = picker.pick("zh-CN-NopeNeural")

        assert listing["error"]
        assert prefs.text(TTS_VOICE) == ""
        assert picker.effective_voice() == "zh-CN-XiaoxiaoNeural"

    def test_the_config_voice_is_always_pickable_even_off_list(self, tmp_path: Any) -> None:
        picker, prefs = _picker(tmp_path)
        picker._section = lambda: _section(voice="zh-CN-SomeCustomNeural")

        assert picker.pick("zh-CN-SomeCustomNeural")["error"] == ""
        assert prefs.text(TTS_VOICE) == "zh-CN-SomeCustomNeural"


class TestPreview:
    def test_preview_speaks_through_the_page_without_changing_the_choice(
        self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import jarvis.tts.engines as engines

        stubs: list[_StubEngine] = []

        def factory(section: TtsSection) -> _StubEngine:
            stub = _StubEngine(section)
            stubs.append(stub)
            return stub

        monkeypatch.setattr(engines, "EdgeTtsEngine", factory)
        sent: list[tuple[bytes, int, bool]] = []

        def emit(pcm: bytes, rate: int, final: bool) -> bool:
            sent.append((pcm, rate, final))
            return True

        picker, prefs = _picker(tmp_path, emit=emit)

        result = picker.preview("zh-CN-YunxiNeural")

        assert result["ok"] is True
        assert sent == [(b"\x01\x02", 24_000, False), (b"\x03\x04", 24_000, True)]
        assert stubs[0].calls == [(PREVIEW_TEXT, "zh-CN-YunxiNeural")]
        assert prefs.text(TTS_VOICE) == "", "hearing a voice must not select it"

    def test_preview_without_a_page_says_so(self, tmp_path: Any) -> None:
        picker, _ = _picker(tmp_path)

        assert picker.preview("zh-CN-XiaoxiaoNeural")["ok"] is False

    def test_preview_on_an_offline_engine_refuses_instead_of_loading_torch(
        self, tmp_path: Any
    ) -> None:
        picker, _ = _picker(tmp_path, engine="cosyvoice", emit=lambda *a: True)

        result = picker.preview("中文女")

        assert result["ok"] is False
        assert "edge_tts" in str(result["error"])

    def test_a_page_that_refuses_the_audio_is_reported(
        self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import jarvis.tts.engines as engines

        monkeypatch.setattr(engines, "EdgeTtsEngine", lambda section: _StubEngine(section))
        picker, _ = _picker(tmp_path, emit=lambda pcm, rate, final: False)

        result = picker.preview("zh-CN-XiaoxiaoNeural")

        assert result["ok"] is False
        assert "页面" in str(result["error"])


class TestTheStyleSliders:
    """Rate and volume: the other two halves of "how does she sound".

    The panel grew them because choosing a voice and then hearing it at the wrong
    speed is half a decision -- and because until now the engine ignored ``tts.speed``
    and ``tts.volume`` from the config entirely (the pipeline passed only a voice), so
    there was no working way to change either.
    """

    def test_they_start_from_the_config_values(self, tmp_path: Any) -> None:
        picker, _ = _picker(tmp_path)
        picker._section = lambda: _section_replacing(speed=1.25, volume=0.4)

        assert picker.effective_speed() == pytest.approx(1.25)
        assert picker.effective_volume() == pytest.approx(0.4)

    def test_setting_one_writes_it_and_reports_the_new_state(self, tmp_path: Any) -> None:
        picker, prefs = _picker(tmp_path)

        listing = picker.set_style(speed=1.3)

        assert listing["error"] == ""
        assert listing["speed"] == pytest.approx(1.3)
        assert prefs.real(TTS_SPEED) == pytest.approx(1.3)
        assert picker.effective_speed() == pytest.approx(1.3)

    def test_a_stored_value_survives_a_round_trip_as_a_float(self, tmp_path: Any) -> None:
        """``Preferences.number`` drops floats; the sliders needed a real accessor."""
        picker, prefs = _picker(tmp_path)

        picker.set_style(volume=0.85)

        assert prefs.real(TTS_VOLUME) == pytest.approx(0.85)
        assert picker.effective_volume() == pytest.approx(0.85)

    def test_a_value_out_of_range_is_refused_and_nothing_is_written(self, tmp_path: Any) -> None:
        picker, prefs = _picker(tmp_path)

        listing = picker.set_style(speed=9.0)

        assert "超出范围" in str(listing["error"])
        assert prefs.real(TTS_SPEED, default=-1.0) == pytest.approx(-1.0)
        assert picker.effective_speed() == pytest.approx(1.0)

    def test_a_slider_that_sends_a_word_does_not_become_one(self, tmp_path: Any) -> None:
        picker, prefs = _picker(tmp_path)

        listing = picker.set_style(speed="fast")

        assert "不是数字" in str(listing["error"])
        assert prefs.real(TTS_SPEED, default=-1.0) == pytest.approx(-1.0)

    def test_the_listing_carries_the_current_style_and_the_ranges(self, tmp_path: Any) -> None:
        """The panel must not hardcode 0.5–1.5: the range lives where it is enforced."""
        picker, _ = _picker(tmp_path)

        listing = picker.voices()

        assert listing["speed"] == pytest.approx(1.0)
        assert listing["speed_min"] == pytest.approx(0.5)
        assert listing["speed_max"] == pytest.approx(1.5)
        assert listing["volume_min"] == pytest.approx(0.0)
        assert listing["volume_max"] == pytest.approx(1.0)

    def test_the_preview_speaks_at_the_slider_values(
        self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Judging a rate without hearing it is what the preview exists to avoid."""
        import jarvis.tts.engines as engines

        stubs: list[_StubEngine] = []

        def factory(section: TtsSection) -> _StubEngine:
            stub = _StubEngine(section)
            stubs.append(stub)
            return stub

        monkeypatch.setattr(engines, "EdgeTtsEngine", factory)
        picker, _ = _picker(tmp_path, emit=lambda *a: True)
        picker.set_style(speed=1.3, volume=0.5)

        assert picker.preview("zh-CN-YunxiNeural")["ok"] is True

        speed_used, volume_used = stubs[0].styles[0]
        assert speed_used is not None and speed_used == pytest.approx(1.3)
        assert volume_used is not None and volume_used == pytest.approx(0.5)

    def test_a_clamped_config_value_never_leaves_the_slider_range(self, tmp_path: Any) -> None:
        """A config file saying 3.0 must not hand the engine a rate it will refuse."""
        picker, _ = _picker(tmp_path)
        picker._section = lambda: _section_replacing(speed=3.0, volume=-2.0)

        assert picker.effective_speed() == pytest.approx(1.5)
        assert picker.effective_volume() == pytest.approx(0.0)


class TestEngineRouting:
    """One rule for which engine speaks a recorded voice, shared by every caller."""

    def test_a_stock_voice_keeps_the_loaded_engine(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:

        picker, _ = _picker(tmp_path)
        called: list[str] = []

        def build(voice: str) -> tuple[object, str]:
            called.append(voice)
            return object(), ""

        monkeypatch.setattr(picker, "_engine_for_voice", build)
        assert picker.engine_for_voice("zh-CN-XiaoxiaoNeural") is None
        assert called == [], "普通音色不该去问克隆那条路"

    def test_a_clone_is_routed_once_and_then_cached(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        picker, _ = _picker(tmp_path)
        sentinel = object()
        calls: list[str] = []

        def build(voice: str) -> tuple[object, str]:
            calls.append(voice)
            return sentinel, ""

        monkeypatch.setattr(picker, "_engine_for_voice", build)
        assert picker.engine_for_voice("clone:104c7ef77bf5") is sentinel
        assert picker.engine_for_voice("clone:104c7ef77bf5") is sentinel
        assert calls == ["clone:104c7ef77bf5"], "第二句还要重建引擎的话，每句话都得等 45 秒加载"

    def test_a_clone_no_engine_can_speak_is_said_so_not_swallowed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        picker, _ = _picker(tmp_path)
        monkeypatch.setattr(picker, "_engine_for_voice", lambda voice: (None, "离线语音不可用"))
        assert picker.engine_for_voice("clone:104c7ef77bf5") is None
