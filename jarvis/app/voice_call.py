"""One spoken turn from a phone: hear it, answer it, say it back.

This is the whole "手机上的语音通话" in one place, and it is deliberately
thin: **it owns no brain and no voice of its own.**

* The brain is :class:`~jarvis.app.chat_service.ChatService` -- the *same* one
  the desktop's input box uses. A spoken question and a typed question reach the
  same agent with the same tool table, the same memory and the same transcript.
  This is the lesson written down in :mod:`jarvis.app.voice_graph`: the moment
  voice gets its own answering path, the same question asked twice gets two
  different capabilities, and the user cannot tell which one they are talking to.
* The ears and mouth are the existing ``asr`` / ``tts`` engines. No new models,
  no second copy of anything.

What *is* new here is only the shape of the input: a finished utterance
handed over as PCM, rather than a microphone being watched. That makes the
phone path much simpler than the desktop's -- no wake word, no VAD, because
the phone's UI already knows when the sentence started and stopped.

Two costs are real and are reported rather than hidden:

* **The ASR model is heavy** (a few hundred megabytes, tens of seconds to load).
  It is loaded lazily, once, and kept. ``warmup()`` exists so the phone can ask
  for it while the user is still raising the phone to their ear instead of
  making them discover the delay on their first sentence.
* **A spoken answer cannot be arbitrarily long.** PCM is ~2 bytes per sample at
  24 kHz, so a three-minute monologue is megabytes of base64 over a LAN
  round-trip. Past :data:`MAX_SPOKEN_CHARS` the answer is spoken up to a
  sentence boundary and flagged, with the full text always returned for the
  screen. A voice call that truncates honestly beats one that stalls.
"""

from __future__ import annotations

import logging
import threading
from typing import TYPE_CHECKING

from jarvis.core.text import speakable

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Callable
    from pathlib import Path

    from jarvis.app.chat_service import ChatService
    from jarvis.app.voice_library import VoiceLibrary
    from jarvis.app.voice_picker import VoicePicker
    from jarvis.asr.types import SpeechRecognizer
    from jarvis.config.schema import AsrSection, TtsSection
    from jarvis.tts.types import SpeechSynthesizer

logger = logging.getLogger("jarvis.app.voice_call")

MAX_SPOKEN_CHARS = 300
"""How much of an answer is actually spoken.

~4 characters per second of Chinese speech, so this is roughly a minute of
audio -- about 2.9 MB of PCM, which is as much as one LAN round-trip should
carry. The rest of the answer is still shown on the phone.
"""

MAX_HEARD_MS = 30_000
"""Longest utterance accepted. Beyond this the payload stops fitting the
request-body limit the LAN endpoint enforces, and nobody says one sentence
for thirty seconds anyway."""

_SENTENCE_ENDS = ("。", "！", "？", "；", "\n", ". ", "! ", "? ")


class VoiceCall:
    """A phone's spoken round trip. Never raises: the caller is an RPC handler."""

    name = "voice_call"

    def __init__(
        self,
        *,
        asr_section: Callable[[], AsrSection],
        tts_section: Callable[[], TtsSection],
        chat: ChatService | None = None,
        picker: VoicePicker | None = None,
        library: VoiceLibrary | None = None,
    ) -> None:
        """Wire the pieces together.

        Args:
            asr_section: Live ``asr`` config. Read at load time, not here --
                the operator can change the engine between restarts and the
                running process should not need to agree with that.
            tts_section: Live ``tts`` config, read per synthesis so that
                moving the voice slider changes the next sentence.
            chat: The one brain. ``None`` means this process has no agent
                (a headless probe) and turns will refuse with a reason.
            picker: Where the current voice and its style come from.
            library: The cloned voices, if any. ``None`` disables cloning.
        """
        self._asr_section = asr_section
        self._tts_section = tts_section
        self._chat = chat
        self._picker = picker
        self._library = library
        self._asr: SpeechRecognizer | None = None
        self._asr_error = ""
        self._asr_lock = threading.Lock()

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        logger.info(
            "voice call ready (asr=%s, tts=%s)",
            self._asr_section().engine,
            self._tts_section().engine,
        )

    def stop(self) -> None:
        recognizer = self._asr
        self._asr = None
        if recognizer is not None:
            try:
                recognizer.close()
            except Exception:  # pragma: no cover - engines vary in how they fail
                logger.exception("closing the recognizer failed")

    # -- readiness ---------------------------------------------------------

    def readiness(self) -> dict[str, object]:
        """What is available, and what is not, before a turn is attempted.

        The phone shows this when it opens the call screen. Its whole point is
        that "she cannot hear you yet" is a state you can see *before* you have
        spoken one sentence into a void.
        """
        return {
            "asr": self._asr_section().engine,
            "tts": self._tts_section().engine,
            "asr_loaded": self._asr is not None,
            "asr_error": self._asr_error,
            "chat": self._chat is not None,
            "cloning": self._library is not None,
        }

    def warmup(self) -> dict[str, object]:
        """Start loading the recognizer now, not on the first sentence.

        Returns immediately. The first real turn still blocks until the load
        finishes, but by then it usually has -- and a phone that says "正在准备
        她的耳朵" while the user is still deciding what to ask is a phone that
        never feels slow.
        """
        if self._asr is not None or self._asr_error:
            return self.readiness()
        worker = threading.Thread(target=self._ensure_asr, name="voice-call-asr")
        worker.daemon = True
        worker.start()
        return self.readiness()

    # -- the turn ----------------------------------------------------------

    def turn(self, pcm: bytes, sample_rate: int) -> dict[str, object]:
        """Hear one utterance, answer it, and hand back the audio.

        Args:
            pcm: Raw s16le mono samples of one finished sentence.
            sample_rate: Rate of ``pcm``; 16 kHz is what the pipeline wants.

        Returns:
            A dict with ``heard``, ``answer``, ``spoken``, ``audio`` (bytes or
            ``None``), ``sample_rate``, ``truncated`` and ``error``. ``error``
            is empty on success; a non-empty one still comes with whatever parts
            succeeded, because "she heard you but could not answer" is a
            different failure from "nothing happened" and the phone says so
            differently.
        """
        heard, error = self.transcribe(pcm, sample_rate)
        if error:
            return self._result(heard="", answer="", error=error)
        if not heard:
            return self._result(heard="", answer="", error="没听清，再说一次")

        answer, error = self._answer(heard)
        if error:
            return self._result(heard=heard, answer=answer, error=error)

        spoken, audio, rate, truncated = self._speak(answer)
        return self._result(
            heard=heard,
            answer=answer,
            spoken=spoken,
            audio=audio,
            rate=rate,
            truncated=truncated,
        )

    def transcribe(self, pcm: bytes, sample_rate: int) -> tuple[str, str]:
        """PCM in, text out. Returns ``(text, error)``."""
        if sample_rate != 16_000:
            return "", f"录音必须是 16kHz，收到的是 {sample_rate}Hz"
        if not pcm:
            return "", "没收到录音"
        duration_ms = len(pcm) / 2 * 1000 / sample_rate
        if duration_ms > MAX_HEARD_MS:
            return "", f"这一句太长了（{duration_ms / 1000:.0f} 秒），先说短一点"

        recognizer = self._ensure_asr()
        if recognizer is None:
            return "", self._asr_error or "听不懂的那一半没装上"
        try:
            result = recognizer.recognize(pcm)
        except Exception as exc:  # pylint: disable=broad-except
            logger.exception("asr failed")
            return "", f"{type(exc).__name__}: {exc}"
        text = (result.text or "").strip()
        # SenseVoice wraps its output in control tokens like <|zh|>; the engine
        # already strips them, but a leftover pair of angle brackets is not a
        # sentence and must not reach the agent as one.
        while text.startswith("<|") and "|>" in text:
            text = text.split("|>", 1)[1].strip()
        return text, ""

    def speak(self, text: str) -> dict[str, object]:
        """Text in, spoken audio out. Public so the phone can also just read a message.

        Returns the same shape :meth:`turn` does -- with ``heard`` and ``answer``
        left empty -- so one RPC reply type renders both "she answered you" and
        "read that message to me" on the phone.
        """
        spoken, audio, rate, truncated = self._speak(text)
        return self._result(
            spoken=spoken, audio=audio, rate=rate, truncated=truncated, heard="", answer=""
        )

    # -- internals ---------------------------------------------------------

    def _answer(self, heard: str) -> tuple[str, str]:
        if self._chat is None:
            return "", "这个进程没有接对话服务，答不了"
        try:
            reply = self._chat.ask(heard)
        except Exception as exc:  # pylint: disable=broad-except
            logger.exception("chat failed on a spoken turn")
            return "", f"{type(exc).__name__}: {exc}"
        if reply.error and not reply.answer.strip():
            return "", reply.error
        return reply.answer, (reply.error or "")

    def _speak(self, text: str) -> tuple[str, bytes | None, int, bool]:
        """Synthesise what should be said, or fail with a sentence.

        A synthesis failure returns no audio but still reports how many
        characters *would* have been spoken: the phone shows the text either
        way, and "she went quiet" is only confusing if the screen is blank too.
        """
        spoken = speakable(text)
        if not spoken:
            return "", None, 0, False
        spoken, truncated = _cap_spoken(spoken)
        voice = self._picker.effective_voice() if self._picker else self._tts_section().voice
        speed = self._picker.effective_speed() if self._picker else None
        volume = self._picker.effective_volume() if self._picker else None
        try:
            engine = self._engine_for(voice)
        except Exception:  # pylint: disable=broad-except
            # No `as exc`: the traceback is already in the log, and the caller
            # only needs to know that nothing will be spoken.
            logger.exception("could not build a synthesizer")
            return spoken, None, 0, truncated
        try:
            chunks = list(engine.synthesize(spoken, voice=voice, speed=speed, volume=volume))
        except Exception as exc:  # pylint: disable=broad-except
            logger.error("synthesis failed: %s", exc)
            return spoken, None, 0, truncated
        finally:
            try:
                engine.close()
            except Exception:  # pragma: no cover - nothing useful to do here
                logger.debug("closing the synthesizer failed")
        if not chunks:
            return spoken, None, 0, truncated
        return spoken, b"".join(chunk.audio for chunk in chunks), chunks[0].sample_rate, truncated

    def _engine_for(self, voice: str) -> SpeechSynthesizer:
        """Build the synthesizer that can actually speak with ``voice``.

        Three engines are in play and none of them is interchangeable: the
        built-in Edge voices exist only in the cloud engine, and a cloned voice
        exists in *two* places -- uploaded to the vendor, and as a local
        recording. So the voice decides, not ``tts.engine``.

        A cloned voice prefers the cloud path when it has one, and the reason is
        measured rather than theoretical: the offline engine needs 16 seconds for
        the first chunk on this machine's GPU and emits at about a quarter of
        real time, which is a walkie-talkie, not a phone call. The local path is
        what runs when there is no key or no upload -- private and free, and
        slow, which is a trade the user gets to make by uploading or not.
        """
        from jarvis.tts.engines import EdgeTtsEngine

        section = self._tts_section()
        found = self._library.resolve(voice) if self._library is not None else None
        if found is None:
            return EdgeTtsEngine(section)
        if found.cloud_voice:
            # Lazily imported so the module stays importable without a network
            # client, and so `jarvis` still starts with no cloud voice at all.
            from jarvis.tts.cloud import SYNTH_MODEL
            from jarvis.tts.cloud_voices import QwenRealtimeVoice

            # The pooled session is what keeps the second sentence as fast as
            # the first: see the module docstring for why `close` does not
            # disconnect.
            return QwenRealtimeVoice.acquire(
                voice=found.cloud_voice,
                model=found.cloud_model or SYNTH_MODEL,
            )
        # Cloned voices without a cloud id run in the voice virtualenv, over the
        # sidecar protocol. The in-process `CosyVoiceTtsEngine` cannot be used
        # here at all: this environment's `transformers` is a major version above
        # CosyVoice's ceiling, so importing it raises rather than degrading.
        from jarvis.tts.sidecar import CosyVoiceSidecar

        # Lazily inside the callable: the engine module must not import `app`.
        def resolve(voice_id: str) -> tuple[Path, str] | None:
            found_again = self._library.resolve(voice_id) if self._library else None
            return None if found_again is None else (found_again.ref_path, found_again.prompt_text)

        return CosyVoiceSidecar(section.model, reference=resolve)

    def _ensure_asr(self) -> SpeechRecognizer | None:
        """Load the recognizer once. Safe to call from several threads at once.

        The lock is held across the whole load on purpose: two threads that
        both decide "not loaded yet" would each pull a model into memory, which
        on this class of machine means several gigabytes instead of one.
        """
        if self._asr is not None or self._asr_error:
            return self._asr
        with self._asr_lock:
            return self._load_asr()

    def _load_asr(self) -> SpeechRecognizer | None:
        """The load itself, with the double-check, run under the lock.

        Split out of :meth:`_ensure_asr` so the re-check lives in a method whose
        parameters the type checker cannot narrow: inside the ``with`` above, the
        outer guard has already proved ``self._asr`` is ``None``, and mypy treats
        the re-check there as dead code even though a second thread may have
        loaded the model in between.
        """
        if self._asr is not None or self._asr_error:
            return self._asr
        try:
            from jarvis.asr.engines import SenseVoiceAsrEngine

            engine = SenseVoiceAsrEngine(self._asr_section())
        except Exception as exc:  # pylint: disable=broad-except
            # Remembered, not retried: a missing optional package will be
            # missing on the next call too, and re-attempting would make
            # every sentence pay the failure again.
            self._asr_error = f"{type(exc).__name__}: {exc}"
            logger.error("could not load the recognizer: %s", self._asr_error)
            return None
        self._asr = engine
        logger.info("recognizer loaded (%s)", engine.name)
        return engine

    @staticmethod
    def _result(
        *,
        heard: str,
        answer: str,
        spoken: str = "",
        audio: bytes | None = None,
        rate: int = 0,
        truncated: bool = False,
        error: str = "",
    ) -> dict[str, object]:
        return {
            "heard": heard,
            "answer": answer,
            "spoken": spoken,
            "audio": audio,
            "sample_rate": rate,
            "truncated": truncated,
            "error": error,
        }


def _cap_spoken(text: str) -> tuple[str, bool]:
    """Shorten to :data:`MAX_SPOKEN_CHARS`, preferring a sentence boundary.

    Cutting mid-word is what makes a truncated answer sound broken; cutting at
    a sentence is what makes it sound like she chose to stop. If there is no
    boundary in reach (a wall of text with no punctuation) the hard cut is
    still better than sending megabytes.
    """
    if len(text) <= MAX_SPOKEN_CHARS:
        return text, False
    window = text[:MAX_SPOKEN_CHARS]
    best = -1
    for end in _SENTENCE_ENDS:
        found = window.rfind(end)
        if found > best:
            best = found + len(end)
    if best >= MAX_SPOKEN_CHARS // 3:
        return window[:best].rstrip(), True
    return window.rstrip(), True


__all__ = ["MAX_HEARD_MS", "MAX_SPOKEN_CHARS", "VoiceCall"]
