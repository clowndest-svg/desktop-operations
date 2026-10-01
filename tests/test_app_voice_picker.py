"""The voice picker: a list, a choice, and a preview that commits to nothing.

The preview is the part worth pinning hardest. It synthesises through a *separate*
engine instance and never writes the preference, because "let me hear the other
one" must not turn into "why did it change" -- and because a preview that needed
the microphone-enabled voice stack would make the picker useless in exactly the
state most operators first open it in.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any, cast

import pytest

from jarvis.app.preferences import TTS_VOICE, Preferences
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

    def synthesize(
        self, text: str, *, voice: str | None = None, **_kwargs: Any
    ) -> Iterator[AudioChunk]:
        self.calls.append((text, voice))
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
