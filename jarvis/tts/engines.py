"""TTS engines.

Two real synthesizers are provided, both imported lazily so that no native
ML stack or network client is required at import time (and ``python -m jarvis``
starts fine with TTS disabled by default):

* :class:`EdgeTtsEngine` — **default**, cloud, pure-Python ``edge-tts`` client
  talking to Microsoft's free online TTS endpoint (no local model).
* :class:`CosyVoiceTtsEngine` — offline alternative (CosyVoice2). Pulls in
  ``torch`` + ``cosyvoice``; both are optional and only touched when an
  utterance is actually synthesized. ``cosyvoice`` is not distributable as a
  clean wheel, so it needs a manual install and is not the out-of-box default.

Every engine follows the :class:`~jarvis.tts.types.SpeechSynthesizer`
contract and yields :class:`~jarvis.tts.types.AudioChunk` streams so callers
can play audio while it is produced ("边合成边播").
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from jarvis.config.schema import TtsSection
from jarvis.core.exceptions import TtsError
from jarvis.tts.types import (
    FORMAT_PCM_S16LE,
    AudioChunk,
    ShouldStop,
)

if TYPE_CHECKING:  # pragma: no cover - import only for type checking
    from collections.abc import Callable, Iterable, Iterator

logger = logging.getLogger("jarvis.tts.engines")


def _missing_dependency(package: str, engine: str, exc: Exception) -> TtsError:
    """Build a precise, actionable error for a missing optional dependency."""
    return TtsError(
        f"the '{engine}' tts engine requires the optional package '{package}', "
        f"which is not installed",
        details={
            "engine": engine,
            "missing_package": package,
            "hint": f"pip install jarvis-assistant[voice]  (or: pip install {package})",
            "cause": repr(exc),
        },
    )


def _with_last_flag[T](items: Iterable[T]) -> Iterator[tuple[T, bool]]:
    """Pair each item with whether it is the final one, in a single pass.

    Needed because CosyVoice's ``inference*`` methods return a *generator*: it
    has no ``len()``, so "is this the last chunk" cannot be answered by index.
    Buffering the whole stream just to count it would defeat the point of
    streaming (callers play each chunk as it arrives), so we keep exactly one
    item of lookahead.

    Args:
        items: Any iterable of generation results.

    Yields:
        ``(item, is_last)`` with ``is_last`` true for the final item only.
    """
    pending: T | None = None
    for item in items:
        if pending is not None:
            yield pending, False
        pending = item
    if pending is not None:
        yield pending, True


def _edge_failure(voice: str, exc: Exception) -> TtsError:
    """Turn the ``edge_tts`` library's exception into something a person can act on.

    ``NoAudioReceived`` is the one that matters in practice: the service answers it
    when a voice has been withdrawn, or when the voice is one of the role-play ones
    that only speaks when sent style/role parameters this client does not send. It
    arrived in the panel as the raw English line ``NoAudioReceived: No audio was
    received. Please verify that your parameters are correct.`` -- measured on nine
    voices in the shipped list (``build/probe_voices.py``) -- and "verify your
    parameters" points the operator at the one thing that is not wrong.

    Everything else (network, timeout, certificate) keeps its own message, because
    a Chinese wrapper around "connection reset" would hide the actual cause.
    """
    name = type(exc).__name__
    if name == "NoAudioReceived":
        return TtsError(
            f"音色 {voice} 没有返回音频：微软服务端要么已下架它，要么它只在角色扮演模式下出声。"
            f"换一个音色再试（「音色」面板里列出来的都是实测能出声的）。",
            details={"engine": "edge_tts", "voice": voice, "cause": name},
        )
    return TtsError(
        f"语音合成失败（{name}）：{exc}",
        details={"engine": "edge_tts", "voice": voice, "cause": repr(exc)},
    )


# ---------------------------------------------------------------------------
# Default engine: CosyVoice (offline, ONNX)
# ---------------------------------------------------------------------------


class CosyVoiceTtsEngine:
    """Offline speech synthesis via CosyVoice2 (ONNX / torch), lazy-loaded.

    Construction is cheap and dependency-free; the heavy ``torch`` + ``cosyvoice``
    imports and model load only happen on the first :meth:`synthesize` call. If
    the packages are absent, the failure is a precise :class:`TtsError` rather
    than an import-time crash.

    Two ways to pick who is speaking, and the difference matters:

    * **A built-in speaker** (``中文女`` and friends) -- CosyVoice's own list.
      Cheap, and the voice is whoever it is.
    * **A recorded reference** -- zero-shot cloning. The engine is handed a few
      seconds of somebody's audio *and the transcript of that audio*, and copies
      the timbre. This is what "录一段我的声音" produces.

    The reference is looked up through the ``reference`` callback rather than
    being read here: this module is a leaf that may only depend on ``core`` and
    ``config``, and the voice library lives in ``app``. The callback is also what
    keeps the engine testable without a store on disk.
    """

    def __init__(
        self,
        section: TtsSection,
        *,
        reference: Callable[[str], tuple[Path, str] | None] | None = None,
    ) -> None:
        """Create the engine.

        Args:
            section: The live ``tts`` config section.
            reference: ``voice_id -> (wav_path, transcript)`` for a recorded
                voice, or ``None`` when that id is not a recording. Passing
                ``None`` for the callback itself disables cloning entirely
                (the command-line entry points and the tests do this).
        """
        self._section = section
        self._voice = section.voice or "中文女"
        self._reference = reference
        self._model: object | None = None

    @property
    def name(self) -> str:
        return "cosyvoice"

    @property
    def sample_rate(self) -> int:
        # CosyVoice2 emits 24 kHz s16le PCM.
        return 24_000

    def _ensure_loaded(self) -> object:
        if self._model is not None:
            return self._model
        try:
            import torch  # noqa: F401  (kept for the side-effect of failing early)
            from cosyvoice.cli.cosyvoice import CosyVoice2
        except Exception as exc:  # pylint: disable=broad-except
            raise _missing_dependency("cosyvoice", "cosyvoice", exc) from exc
        self._model = CosyVoice2(
            self._section.model,
            load_jit=False,
            load_trt=False,
            load_vllm=False,
            # Half precision on the GPU is roughly a 2x speed-up on a card this
            # class of machine has, and the model is small enough that the quality
            # difference is not audible. CPU stays fp32: fp16 on CPU is emulated
            # and *slower*, which is the opposite of what the flag looks like.
            #
            # ``CosyVoice2`` turns fp16 off by itself when no CUDA device is
            # present, so passing it here is safe on either backend.
            fp16=self._section.device == "cuda",
        )
        return self._model

    def _lookup_reference(self, voice_id: str) -> tuple[Path, str] | None:
        """Ask the callback what this id is, tolerating a callback that throws.

        The callback reads the disk. A store that has been deleted out from under
        a running process must not turn into an exception on the synthesis path --
        it means "no recording for this id", which the caller already handles.
        """
        if self._reference is None:
            return None
        try:
            return self._reference(voice_id)
        except Exception:  # pragma: no cover - depends on the store's state
            logger.exception("voice reference lookup failed for %s", voice_id)
            return None

    def synthesize(
        self,
        text: str,
        *,
        voice: str | None = None,
        speed: float | None = None,
        volume: float | None = None,
        should_stop: ShouldStop | None = None,
    ) -> Iterator[AudioChunk]:
        """Synthesize ``text`` and yield the produced PCM chunk(s).

        Args mirror :class:`~jarvis.tts.types.SpeechSynthesizer`. ``volume`` is
        advisory and ignored (CosyVoice has no gain knob); ``speed`` is forwarded
        to the model, which honours it natively as a rate factor.
        """
        del volume  # advisory for this engine
        model = self._ensure_loaded()
        chosen = voice or self._voice
        # ``speed=None`` or <= 0 would divide by zero deep inside the flow module;
        # the model wants a positive factor and 1.0 is "normal".
        rate = 1.0 if speed is None or speed <= 0 else float(speed)
        import numpy as np  # lazy: only when actually synthesizing

        reference = self._lookup_reference(chosen)
        if reference is None:
            # A *built-in* speaker goes through ``inference_sft`` -- the plain
            # ``inference`` that older CosyVoice1 had does not exist on
            # CosyVoice2, and calling it raises AttributeError.
            generated = model.inference_sft(  # type: ignore[attr-defined]
                text, chosen, stream=False, speed=rate
            )
        else:
            generated = self._zero_shot(model, text, chosen, reference, rate)

        # ``generated`` is a *generator* in the real CosyVoice (one item per
        # streamed sentence) -- ``len()`` on it would raise, and callers would
        # never see the audio. Decide "is this the last chunk" with one item of
        # lookahead instead, which is correct for a generator and for the list
        # the tests hand in.
        for item, is_last in _with_last_flag(generated):
            if should_stop is not None and should_stop():
                break
            speech = item["tts_speech"]
            if hasattr(speech, "squeeze"):
                speech = speech.squeeze(0)
            pcm = (np.asarray(speech.cpu().numpy(), dtype="<f4") * 32767.0).astype("<i2").tobytes()
            yield AudioChunk(
                audio=pcm,
                sample_rate=self.sample_rate,
                is_final=is_last,
                format=FORMAT_PCM_S16LE,
            )

    def _zero_shot(
        self,
        model: object,
        text: str,
        voice_id: str,
        reference: tuple[Path, str],
        rate: float = 1.0,
    ) -> object:
        """Clone the timbre of a recorded clip for this one sentence.

        CosyVoice's zero-shot call wants three things: the text to say, the text
        the *reference* says, and the reference audio resampled to 16 kHz. The
        transcript is not optional -- without it the model has no way to separate
        "how this person sounds" from "what this person said", and it comes back
        speaking the reference sentence instead of the answer.

        ``prompt_wav`` is passed as the **path**, not as audio already read into
        a tensor: the model's own front end loads and resamples the clip itself
        (``frontend_zero_shot`` -> ``load_wav`` -> ``torchaudio.load``), so
        handing it a tensor would fail inside ``torchaudio`` on the first
        recorded voice anybody tried.
        """
        path, prompt_text = reference
        if not prompt_text.strip():
            raise TtsError(
                "这个自定义音色没有留下参考文本，她分不清音色和内容",
                details={"engine": "cosyvoice", "voice": voice_id},
            )
        if not path.is_file():
            raise TtsError(
                f"自定义音色的录音文件不在了：{path.name}",
                details={"engine": "cosyvoice", "voice": voice_id, "path": str(path)},
            )
        return model.inference_zero_shot(  # type: ignore[attr-defined]
            text,
            prompt_text,
            str(path),
            stream=False,
            speed=rate,
            # The reference clip is a few seconds of ordinary speech; running it
            # through text normalisation as well as the target sentence keeps the
            # two sides of the model on the same footing.
            text_frontend=True,
        )

    def close(self) -> None:
        # Release the model; torch has no explicit free, but dropping the
        # reference lets the GC reclaim GPU/CPU tensors.
        self._model = None


# ---------------------------------------------------------------------------
# Cloud alternative: Edge-TTS
# ---------------------------------------------------------------------------


class EdgeTtsEngine:
    """Online speech synthesis via Microsoft Edge TTS (``edge-tts``).

    A pure-Python websocket client — no local model. Construction is
    dependency-free; ``edge_tts`` is imported lazily on first use. Network or
    missing-package failures surface as a precise :class:`TtsError`.
    """

    def __init__(self, section: TtsSection) -> None:
        self._section = section
        self._voice = section.voice or "zh-CN-XiaoxiaoNeural"

    @property
    def name(self) -> str:
        return "edge_tts"

    @property
    def sample_rate(self) -> int:
        # Edge-TTS delivers 24 kHz audio; the decoded PCM keeps that rate.
        return 24_000

    @staticmethod
    def _rate_tag(speed: float | None) -> str:
        if speed is None:
            return "+0%"
        pct = max(-100, min(100, int((speed - 1.0) * 100)))
        return f"{pct:+d}%"

    @staticmethod
    def _volume_tag(volume: float | None) -> str:
        if volume is None:
            return "+0%"
        pct = max(-100, min(100, int((volume - 1.0) * 100)))
        return f"{pct:+d}%"

    def synthesize(
        self,
        text: str,
        *,
        voice: str | None = None,
        speed: float | None = None,
        volume: float | None = None,
        should_stop: ShouldStop | None = None,
    ) -> Iterator[AudioChunk]:
        """Synthesize ``text`` via Edge-TTS and yield it as one PCM chunk.

        Edge-TTS is natively async; we drain its stream synchronously, decode
        the MP3 it returns, and yield a single s16le chunk at 24 kHz. Because
        the whole utterance is collected anyway, ``should_stop`` can only abort
        before playback starts (a later async refactor could interleave the two
        for true play-while-synthesize on this engine).
        """
        try:
            import edge_tts
        except Exception as exc:  # pylint: disable=broad-except
            raise _missing_dependency("edge-tts", "edge_tts", exc) from exc

        voice_id = voice or self._voice
        communicate = edge_tts.Communicate(
            text,
            voice_id,
            rate=self._rate_tag(speed),
            volume=self._volume_tag(volume),
        )
        try:
            chunks = self._drain(communicate)
        except Exception as exc:
            raise _edge_failure(voice_id, exc) from exc
        if not chunks:
            return
        if should_stop is not None and should_stop():
            return
        yield AudioChunk(
            audio=self._to_pcm(b"".join(chunks)),
            sample_rate=self.sample_rate,
            is_final=True,
        )

    @staticmethod
    def _to_pcm(mp3: bytes) -> bytes:
        """Decode the MP3 Edge-TTS returns into the s16le PCM consumers expect.

        Every downstream reader (player, WAV writer) speaks ``pcm_s16le`` and
        skips anything else, so handing back raw MP3 means silence that looks
        like success.
        """
        try:
            import io

            import numpy
            import soundfile
        except ImportError as exc:
            raise _missing_dependency("soundfile", "edge_tts", exc) from exc
        try:
            samples, _rate = soundfile.read(io.BytesIO(mp3), dtype="float32")
        except Exception as exc:  # pragma: no cover - depends on the wire payload
            raise TtsError(
                "failed to decode the Edge-TTS audio",
                details={"engine": "edge_tts", "cause": repr(exc)},
            ) from exc
        if samples.ndim > 1:  # mono already; a stereo payload gets folded
            samples = samples.mean(axis=1)
        pcm: bytes = (numpy.clip(samples, -1.0, 1.0) * 32767.0).astype(numpy.int16).tobytes()
        return pcm

    @staticmethod
    def _drain(communicate: object) -> list[bytes]:
        import asyncio

        async def collect() -> list[bytes]:
            out: list[bytes] = []
            async for event in communicate.stream():  # type: ignore[attr-defined]
                if event.get("type") == "audio":
                    out.append(event["data"])
            return out

        return asyncio.run(collect())

    def close(self) -> None:
        # Stateless client; nothing to release.
        return None
