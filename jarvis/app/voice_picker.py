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

from jarvis.app.preferences import TTS_SPEED, TTS_VOICE, TTS_VOLUME, Preferences
from jarvis.app.voice_library import (
    CLONE_PREFIX,
    COMFORT_MS,
    MAX_MS,
    MAX_VOICES,
    resolves_to_cloud,
)
from jarvis.core.exceptions import JarvisError
from jarvis.tts.types import AudioChunk

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Iterator
    from pathlib import Path

    from jarvis.app.voice_library import VoiceLibrary
    from jarvis.config.schema import TtsSection
    from jarvis.tts.types import SpeechSynthesizer

logger = logging.getLogger("jarvis.app.voice_picker")

PREVIEW_TEXT: Final[str] = "你好，我是小夜。这一段就是选中的声音读出来的样子。"
"""One sentence, long enough to judge a voice, short enough not to overstay."""

SPEED_MIN: Final[float] = 0.5
SPEED_MAX: Final[float] = 1.5
VOLUME_MIN: Final[float] = 0.0
VOLUME_MAX: Final[float] = 1.0
"""The ranges the sliders offer. Wider ends are not useful: Edge-TTS clamps at ±100%
anyway, and a rate below half speed is a different product, not a setting."""

_STYLE_UNSET: Final[float] = -1.0
"""What "the window never touched this" reads as in the preferences file."""

EDGE_VOICES: Final[tuple[tuple[str, str], ...]] = (
    ("zh-CN-XiaoxiaoNeural", "晓晓 · 女声 · 温和（Edge 默认）"),
    ("zh-CN-XiaoyiNeural", "晓伊 · 女声 · 活泼"),
    ("zh-CN-XiaoxuanNeural", "晓萱 · 女声 · 明快"),
    ("zh-CN-YunxiNeural", "云希 · 男声 · 清亮"),
    ("zh-CN-YunjianNeural", "云健 · 男声 · 沉稳"),
    ("zh-CN-YunxiaNeural", "云夏 · 男声 · 可爱"),
    ("zh-CN-YunyangNeural", "云扬 · 男声 · 新闻播报"),
    ("zh-CN-liaoning-XiaobeiNeural", "晓北 · 女声 · 东北话"),
    ("zh-CN-shaanxi-XiaoniNeural", "晓妮 · 女声 · 陕西话"),
    ("zh-HK-HiuGaaiNeural", "晓佳 · 粤语女声"),
    ("zh-HK-HiuMaanNeural", "晓曼 · 粤语女声"),
    ("zh-HK-WanLungNeural", "云龙 · 粤语男声"),
    ("zh-TW-HsiaoChenNeural", "晓臻 · 女声 · 台湾国语"),
    ("zh-TW-HsiaoYuNeural", "晓雨 · 女声 · 台湾国语"),
    ("zh-TW-YunJheNeural", "云哲 · 男声 · 台湾国语"),
)
"""Every Chinese voice that was **measured to actually speak**, 2026-10-02.

This list used to be plausible instead of measured -- 16 names picked because they had
shipped for years -- and 9 of them answered **every** preview with
``NoAudioReceived``: 晓辰 / 晓涵 / 晓墨 / 晓秋 / 晓睿 / 晓颜 / 晓双 / 云野 / 云泽.
Those are the ones Microsoft serves only to the role-play API (they need style or role
parameters this engine does not send), and the operator found out by clicking 试听 and
reading an English exception inside a Chinese panel.

So: the list is the output of ``build/probe_voices.py --all``, which synthesises a line
through the product's own engine and prints whatever comes back. Re-run it when a voice
starts failing -- the set churns on Microsoft's side, not on ours.
"""

_CLOUD_ENGINE = "edge_tts"


SYNTH_TIMEOUT_SECONDS = 120.0
"""One synthesis request's ceiling.

The sidecar used to inherit a 900 s default: a stuck clone synthesis then pinned a
4 GB worker at full CPU for a quarter of an hour while the button said 合成中.
Two minutes is longer than any real sentence and short enough to notice.
"""


class VoicePicker:
    """The voice picker's backend. Owns no engine of its own except for previews."""

    name = "voice_picker"

    def __init__(
        self,
        section_provider: Callable[[], TtsSection],
        prefs: Preferences,
        *,
        emit: Callable[[bytes, int, bool], bool] | None = None,
        library: VoiceLibrary | None = None,
    ) -> None:
        """Create the service.

        Args:
            section_provider: The live ``tts`` config section.
            prefs: Where the window's choice is remembered.
            emit: Where preview PCM goes -- the same channel real speech uses, so
                the voice core reacts to a preview exactly like to an answer.
                ``None`` in a process with no page (the command-line entry points).
            library: The recorded voices, if this process has them. ``None``
                means the picker shows only the built-in list and says nothing
                about recording one.
        """
        self._section = section_provider
        self._prefs = prefs
        self._emit = emit
        self._library = library
        self._engine_cache: dict[str, SpeechSynthesizer] = {}
        self._preview_lock = threading.Lock()

    @staticmethod
    def cloning_state() -> tuple[bool, str]:
        """Whether a recorded voice can be *made and used* on this machine.

        Answered by checking for files, never by importing: ``cosyvoice`` lives
        in a *different* interpreter from this one (its ``transformers`` ceiling
        does not fit here), so ``find_spec`` in this process would answer "no"
        even on a machine where cloning works perfectly. What actually has to
        exist is the voice virtualenv, the CosyVoice checkout beside it and the
        worker script -- see :func:`jarvis.tts.sidecar.cloning_ready`, which owns
        that rule so the picker and the engine cannot disagree about it.
        """
        from jarvis.tts.sidecar import cloning_ready

        return cloning_ready()

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        logger.info("tts voice picker ready (engine=%s)", self._section().engine)

    def stop(self) -> None:
        return None

    # -- what can be chosen ------------------------------------------------

    def effective_voice(self) -> str:
        """The voice the next utterance will use: the window's pick, else config."""
        return self._prefs.text(TTS_VOICE) or self._section().voice

    def effective_speed(self) -> float:
        """How fast the next utterance is spoken: the window's slider, else config.

        Read per utterance, the same way the voice is: the point of the slider is
        that dragging it changes the *next* sentence rather than the next restart.
        """
        stored = self._prefs.real(TTS_SPEED, default=_STYLE_UNSET)
        if stored == _STYLE_UNSET:
            stored = float(self._section().speed or 1.0)
        return min(SPEED_MAX, max(SPEED_MIN, stored))

    def effective_volume(self) -> float:
        """How loud, on the same terms as :meth:`effective_speed`."""
        stored = self._prefs.real(TTS_VOLUME, default=_STYLE_UNSET)
        if stored == _STYLE_UNSET:
            stored = float(self._section().volume if self._section().volume is not None else 1.0)
        return min(VOLUME_MAX, max(VOLUME_MIN, stored))

    def set_style(self, *, speed: object = None, volume: object = None) -> dict[str, object]:
        """Move one or both sliders. Returns the same payload :meth:`voices` does.

        Values arrive from the page, so they are typed here rather than trusted: a
        slider that sends ``"fast"`` must not silently become 1.0, and the range is
        enforced where the number is written, not where it is drawn.
        """
        for value, (low, high), key in (
            (speed, (SPEED_MIN, SPEED_MAX), TTS_SPEED),
            (volume, (VOLUME_MIN, VOLUME_MAX), TTS_VOLUME),
        ):
            if value is None:
                continue
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                return {**self.voices(), "error": f"这个值不是数字：{value!r}"}
            number = float(value)
            if not low <= number <= high:
                return {**self.voices(), "error": f"超出范围（{low}–{high}）：{number}"}
            self._prefs.set(key, number)
        return {"error": "", **self.voices()}

    def voices(self) -> dict[str, object]:
        section = self._section()
        current = self.effective_voice()
        style = {
            "speed": self.effective_speed(),
            "volume": self.effective_volume(),
            "speed_min": SPEED_MIN,
            "speed_max": SPEED_MAX,
            "volume_min": VOLUME_MIN,
            "volume_max": VOLUME_MAX,
        }
        can_clone, clone_reason = self.cloning_state()
        cloning: dict[str, object] = {
            "available": can_clone,
            "reason": clone_reason,
            "min_ms": COMFORT_MS[0],
            "comfortable_ms": COMFORT_MS[1],
            "max_ms": MAX_MS,
            "max_voices": MAX_VOICES,
        }
        recorded = self._library.voices() if self._library is not None else []

        # An offline engine has no curated list to offer: its speaker ids are not
        # something this project keeps, so a picker that pretended otherwise would
        # be showing a list that does not exist. Say why instead of offering
        # something that cannot be picked.
        #
        # The refusal is skipped when the operator has recorded voices, because
        # those *are* pickable off any engine -- and hiding somebody's own
        # recording reads as "it was deleted".
        if section.engine != _CLOUD_ENGINE and not recorded:
            return {
                "error": f"当前合成引擎是 {section.engine}，内置音色只对 {_CLOUD_ENGINE} 开放",
                "engine": section.engine,
                "current": current,
                "choices": [],
                "cloning": cloning,
                **style,
            }

        choices: list[dict[str, object]] = []
        if section.engine == _CLOUD_ENGINE:
            choices.extend(
                {
                    "id": voice_id,
                    "label": label,
                    "kind": "builtin",
                    "engine": _CLOUD_ENGINE,
                    "current": voice_id == current,
                }
                for voice_id, label in EDGE_VOICES
            )

        # Recorded voices come last and are labelled with what they are. They
        # are shown even when the cloning engine is missing: hiding someone's
        # own recording because a package is absent reads as "it was deleted",
        # and the reason belongs on the preview button, not in the list.
        choices.extend(
            {
                **voice.to_public(),
                "label": f"{voice.name}（录的音色）",
                "current": voice.voice_id == current,
            }
            for voice in recorded
        )

        if current and not any(entry["id"] == current for entry in choices):
            # A config file may name a voice this list does not carry; hiding
            # the current voice would make the picker disagree with what you
            # hear.
            choices.insert(
                0,
                {
                    "id": current,
                    "label": f"{current}（配置里的当前音色）",
                    "kind": "builtin",
                    "engine": section.engine,
                    "current": True,
                },
            )

        return {
            "error": "",
            "engine": section.engine,
            "current": current,
            "choices": choices,
            "cloning": cloning,
            **style,
        }

    def _library_is_clone(self, voice_id: str) -> bool:
        return self._library is not None and self._library.is_clone(voice_id)

    def pick(self, voice_id: object) -> dict[str, object]:
        voice = str(voice_id or "").strip()
        listing = self.voices()
        if listing["error"]:
            return listing
        known = {voice_id for voice_id, _ in EDGE_VOICES} | {self._section().voice}
        if self._library is not None:
            known |= {entry.voice_id for entry in self._library.voices()}
        if voice not in known:
            return {**listing, "error": f"不认识的音色：{voice or '（空）'}"}
        if self._library_is_clone(voice) and not self.cloning_state()[0]:
            # Allowed to be *selected* -- losing the choice because a package
            # went missing is worse than choosing it and hearing the reason
            # when she next speaks.
            self._prefs.set(TTS_VOICE, voice)
            return {**self.voices(), "error": self.cloning_state()[1]}
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
        engine, error = self._engine_for_voice(str(voice_id or "").strip())
        if engine is None:
            return {"ok": False, "error": error}
        voice = str(voice_id or "").strip() or self.effective_voice()
        if not self._preview_lock.acquire(blocking=False):
            engine.close()
            return {"ok": False, "error": "上一条试听还没播完", "voice": voice}
        try:
            # Chunks go straight to the page as they arrive, so the preview starts
            # speaking while the rest is still being synthesised -- buffering the
            # whole sentence first would add a silent pause to every 试听.
            sent = 0
            for chunk in self._stream_preview(engine, voice):
                if isinstance(chunk, str):
                    return {"ok": False, "error": chunk, "voice": voice}
                sent += 1
                if not self._emit(chunk.audio, chunk.sample_rate, chunk.is_final):
                    return {"ok": False, "error": "页面没有接收音频", "voice": voice}
        finally:
            self._preview_lock.release()
            engine.close()
        if sent == 0:
            return {"ok": False, "error": "合成没有产出任何音频", "voice": voice}
        return {"ok": True, "error": "", "voice": voice, "chunks": sent}

    def preview_pcm(self, voice_id: object, text: str | None = None) -> dict[str, object]:
        """Synthesise one sentence and **return** the PCM instead of playing it.

        The phone has no page-audio channel of its own -- it gets the bytes back
        over the wire and plays them itself -- so this is the same job as
        :meth:`preview` with the last step left to the caller. Keeping the same
        engine-selection rule behind both is what stops "试听" from sounding
        different on the phone than it does on the desktop.

        Unlike :meth:`preview` this one *does* hold the whole sentence: the bytes
        have to be assembled into one blob to survive the round trip.
        """
        engine, error = self._engine_for_voice(str(voice_id or "").strip())
        if engine is None:
            return {"ok": False, "error": error}
        voice = str(voice_id or "").strip() or self.effective_voice()
        if not self._preview_lock.acquire(blocking=False):
            engine.close()
            return {"ok": False, "error": "上一条试听还没播完", "voice": voice}
        try:
            chunks: list[bytes] = []
            rate = 0
            for chunk in self._stream_preview(engine, voice, text):
                if isinstance(chunk, str):
                    return {"ok": False, "error": chunk, "voice": voice}
                chunks.append(chunk.audio)
                rate = chunk.sample_rate
        finally:
            self._preview_lock.release()
            engine.close()
        if not chunks:
            return {"ok": False, "error": "合成没有产出任何音频", "voice": voice}
        return {
            "ok": True,
            "error": "",
            "voice": voice,
            "pcm": b"".join(chunks),
            "sample_rate": rate,
        }

    def _engine_for_voice(self, voice: str) -> tuple[SpeechSynthesizer | None, str]:
        """Pick the engine a preview of ``voice`` must run on, or explain why none can.

        Which engine runs depends on the *voice*, not on ``tts.engine``: a
        recorded voice only exists inside an offline engine or on the vendor's
        side, so a picker that insisted on ``tts.engine`` here would silently
        preview the wrong thing -- or nothing.

        The cloud/local split for a recorded voice is decided by
        :func:`~jarvis.app.voice_library.resolves_to_cloud`, the same function
        the call path uses. Anything else risks 试听 and the spoken answer coming
        out in different engines.
        """
        from jarvis.tts.engines import EdgeTtsEngine

        section = self._section()
        library = self._library
        found = library.resolve(voice) if library is not None else None
        if found is not None and resolves_to_cloud(found):
            # Preview over the cloud too. Falling back to the offline engine here
            # would make 试听 take twenty seconds for a voice that answers in
            # under one -- and the operator would conclude the clone is broken.
            from jarvis.tts.cloud import SYNTH_MODEL
            from jarvis.tts.cloud_voices import QwenRealtimeVoice

            try:
                return (
                    QwenRealtimeVoice.acquire(
                        voice=found.cloud_voice,
                        model=found.cloud_model or SYNTH_MODEL,
                    ),
                    "",
                )
            except JarvisError as exc:
                return None, str(exc)
        if library is not None and found is not None:
            can_clone, reason = self.cloning_state()
            if not can_clone:
                return None, reason

            def resolve(voice_id: str) -> tuple[Path, str] | None:
                again = library.resolve(voice_id)
                return None if again is None else (again.ref_path, again.prompt_text)

            # A cloned voice is spoken by the *other* interpreter -- see
            # `jarvis.tts.sidecar` for why that is not an optimisation.
            from jarvis.tts.sidecar import CosyVoiceSidecar

            return (
                CosyVoiceSidecar(
                    section.model,
                    reference=resolve,
                    timeout=SYNTH_TIMEOUT_SECONDS,
                ),
                "",
            )
        if section.engine != _CLOUD_ENGINE:
            return (
                None,
                f"{section.engine} 的试听要先加载离线模型，这里不试；切到 {_CLOUD_ENGINE} 再听",
            )
        return EdgeTtsEngine(section), ""

    def engine_for_voice(self, voice_id: object) -> SpeechSynthesizer | None:
        """The engine that has to speak this voice, or ``None`` when the current one can.

        One rule for every caller -- preview, read-aloud, the phone -- because a recorded
        voice is spoken by the sidecar or by the vendor and never by edge-tts: handing
        edge-tts a ``clone:`` id dies inside its voice-name validation with a ValueError,
        which on the read-aloud path reads to the operator as "she answered but stayed
        silent" and on the picker as a button stuck at 合成中.

        Engines are cached per voice: the sidecar's worker takes ~45 s to load weights,
        and a reply path that rebuilt it per sentence would spend every answer waiting.
        """
        voice = str(voice_id or "").strip()
        if not voice or not voice.startswith(CLONE_PREFIX):
            return None
        cached = self._engine_cache.get(voice)
        if cached is not None:
            return cached
        engine, error = self._engine_for_voice(voice)
        if engine is None:
            logger.warning("没有引擎能说 %s：%s", voice, error)
            return None
        self._engine_cache[voice] = engine
        return engine

    def _stream_preview(
        self, engine: SpeechSynthesizer, voice: str, text: str | None = None
    ) -> Iterator[AudioChunk | str]:
        """Yield the preview's chunks, or a single ``str`` describing a failure.

        A generator rather than a list so :meth:`preview` can start playing the
        first chunk while the rest are still being produced. Failures are yielded
        rather than raised because the caller is already inside a ``try/finally``
        that has to close the engine and release the lock either way.
        """
        spoken = (text or "").strip() or PREVIEW_TEXT
        # The sliders apply to the preview too: hearing the voice at the rate and
        # volume it will actually speak is the whole point of a preview.
        try:
            yield from engine.synthesize(
                spoken,
                voice=voice,
                speed=self.effective_speed(),
                volume=self.effective_volume(),
            )
        except JarvisError as exc:
            logger.error("voice preview failed for %s: %s", voice, exc)
            yield str(exc)
        except Exception as exc:  # a network failure must read as a refusal, not a crash
            logger.exception("voice preview failed unexpectedly")
            yield f"{type(exc).__name__}: {exc}"


__all__ = ["EDGE_VOICES", "PREVIEW_TEXT", "VoicePicker"]
