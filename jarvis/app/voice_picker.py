"""Which voice the assistant speaks with, and a way to hear one before choosing.

Three small jobs, one place:

* **list** -- what can be picked, with the current one marked;
* **pick** -- write the choice where the next utterance will read it;
* **preview** -- synthesise one sentence with a voice *without* changing anything,
  so choosing is a decision made with ears rather than with names.

The list is curated rather than fetched. ``edge_tts`` can enumerate every voice
Microsoft currently serves, but that is a network round-trip on every popup open,
the answer changes without this project knowing, and ninety percent of it is
languages this assistant does not speak. A short list of stable Chinese voices,
plus whatever the operator's config already names, is the honest scope.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from typing import TYPE_CHECKING, Final

from jarvis.app.preferences import TTS_VOICE, Preferences
from jarvis.core.exceptions import JarvisError

if TYPE_CHECKING:  # pragma: no cover - typing only
    from jarvis.config.schema import TtsSection

logger = logging.getLogger("jarvis.app.voice_picker")

PREVIEW_TEXT: Final[str] = "你好，我是小夜。这一段就是选中的声音读出来的样子。"
"""One sentence, long enough to judge a voice, short enough not to overstay."""

EDGE_VOICES: Final[tuple[tuple[str, str], ...]] = (
    ("zh-CN-XiaoxiaoNeural", "晓晓 · 女声 · 温和（Edge 默认）"),
    ("zh-CN-XiaoyiNeural", "晓伊 · 女声 · 活泼"),
    ("zh-CN-XiaochenNeural", "晓辰 · 女声 · 知性"),
    ("zh-CN-XiaohanNeural", "晓涵 · 女声 · 温柔"),
    ("zh-CN-XiaomoNeural", "晓墨 · 女声 · 叙事"),
    ("zh-CN-XiaoqiuNeural", "晓秋 · 女声 · 成熟"),
    ("zh-CN-XiaoruiNeural", "晓睿 · 女声 · 年长"),
    ("zh-CN-XiaoxuanNeural", "晓萱 · 女声 · 明快"),
    ("zh-CN-XiaoyanNeural", "晓颜 · 女声 · 平静"),
    ("zh-CN-XiaoshuangNeural", "晓双 · 童声"),
    ("zh-CN-YunxiNeural", "云希 · 男声 · 清亮"),
    ("zh-CN-YunjianNeural", "云健 · 男声 · 沉稳"),
    ("zh-CN-YunyeNeural", "云野 · 男声 · 叙事"),
    ("zh-CN-YunzeNeural", "云泽 · 男声 · 低沉"),
    ("zh-HK-HiuMaanNeural", "晓曼 · 粤语女声"),
    ("zh-TW-HsiaoChenNeural", "晓臻 · 女声 · 台湾国语"),
)
"""Stable Edge-TTS Chinese voices. Names, not guesses: these ids have shipped for
years and a wrong one fails loudly at preview time rather than silently."""

_CLOUD_ENGINE = "edge_tts"


class VoicePicker:
    """The voice picker's backend. Owns no engine of its own except for previews."""

    name = "voice_picker"

    def __init__(
        self,
        section_provider: Callable[[], TtsSection],
        prefs: Preferences,
        *,
        emit: Callable[[bytes, int, bool], bool] | None = None,
    ) -> None:
        """Create the service.

        Args:
            section_provider: The live ``tts`` config section.
            prefs: Where the window's choice is remembered.
            emit: Where preview PCM goes -- the same channel real speech uses, so
                the voice core reacts to a preview exactly like to an answer.
                ``None`` in a process with no page (the command-line entry points).
        """
        self._section = section_provider
        self._prefs = prefs
        self._emit = emit
        self._preview_lock = threading.Lock()

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        logger.info("tts voice picker ready (engine=%s)", self._section().engine)

    def stop(self) -> None:
        return None

    # -- what can be chosen ------------------------------------------------

    def effective_voice(self) -> str:
        """The voice the next utterance will use: the window's pick, else config."""
        return self._prefs.text(TTS_VOICE) or self._section().voice

    def voices(self) -> dict[str, object]:
        section = self._section()
        current = self.effective_voice()
        if section.engine != _CLOUD_ENGINE:
            return {
                "error": f"当前合成引擎是 {section.engine}，音色选择只对 {_CLOUD_ENGINE} 开放",
                "engine": section.engine,
                "current": current,
                "choices": [],
            }
        choices = [
            {"id": voice_id, "label": label, "current": voice_id == current}
            for voice_id, label in EDGE_VOICES
        ]
        if current and not any(entry["id"] == current for entry in choices):
            # A config file may name a voice this list does not carry; hiding the
            # current voice would make the picker disagree with what you hear.
            choices.insert(
                0, {"id": current, "label": f"{current}（配置里的当前音色）", "current": True}
            )
        return {"error": "", "engine": section.engine, "current": current, "choices": choices}

    def pick(self, voice_id: object) -> dict[str, object]:
        voice = str(voice_id or "").strip()
        listing = self.voices()
        if listing["error"]:
            return listing
        known = {voice_id for voice_id, _ in EDGE_VOICES} | {self._section().voice}
        if voice not in known:
            return {**listing, "error": f"不认识的音色：{voice or '（空）'}"}
        self._prefs.set(TTS_VOICE, voice)
        logger.info("tts voice switched to %s", voice)
        return {"error": "", **self.voices()}

    # -- hearing one before choosing ---------------------------------------

    def preview(self, voice_id: object) -> dict[str, object]:
        """Synthesise :data:`PREVIEW_TEXT` with one voice and hand the PCM to the page.

        Deliberately does *not* change the selected voice: a preview that also
        commits turns "let me hear the other one" into "why is it talking
        differently now".
        """
        if self._emit is None:
            return {"ok": False, "error": "这个进程没有页面音频通道，试听不了"}
        section = self._section()
        if section.engine != _CLOUD_ENGINE:
            return {
                "ok": False,
                "error": (
                    f"{section.engine} 的试听要先加载离线模型，这里不试；"
                    f"切到 {_CLOUD_ENGINE} 再听"
                ),
            }
        voice = str(voice_id or "").strip() or self.effective_voice()
        if not self._preview_lock.acquire(blocking=False):
            return {"ok": False, "error": "上一条试听还没播完"}
        emit = self._emit
        try:
            return self._synthesize_preview(section, voice, emit)
        finally:
            self._preview_lock.release()

    def _synthesize_preview(
        self, section: TtsSection, voice: str, emit: Callable[[bytes, int, bool], bool]
    ) -> dict[str, object]:
        from jarvis.tts.engines import EdgeTtsEngine

        engine = EdgeTtsEngine(section)
        chunks = 0
        try:
            for chunk in engine.synthesize(PREVIEW_TEXT, voice=voice):
                chunks += 1
                if not emit(chunk.audio, chunk.sample_rate, chunk.is_final):
                    return {"ok": False, "error": "页面没有接收音频", "voice": voice}
        except JarvisError as exc:
            logger.error("voice preview failed for %s: %s", voice, exc)
            return {"ok": False, "error": str(exc), "voice": voice}
        except Exception as exc:  # a network failure must read as a refusal, not a crash
            logger.exception("voice preview failed unexpectedly")
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}", "voice": voice}
        finally:
            engine.close()
        if chunks == 0:
            return {"ok": False, "error": "合成没有产出任何音频", "voice": voice}
        return {"ok": True, "error": "", "voice": voice, "chunks": chunks}


__all__ = ["EDGE_VOICES", "PREVIEW_TEXT", "VoicePicker"]
