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
from typing import TYPE_CHECKING

from jarvis.config.schema import TtsSection
from jarvis.core.exceptions import TtsError
from jarvis.tts.types import (
    FORMAT_PCM_S16LE,
    AudioChunk,
    ShouldStop,
)

if TYPE_CHECKING:  # pragma: no cover - import only for type checking
    from collections.abc import Iterator

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


# ---------------------------------------------------------------------------
# Default engine: CosyVoice (offline, ONNX)
# ---------------------------------------------------------------------------


class CosyVoiceTtsEngine:
    """Offline speech synthesis via CosyVoice2 (ONNX / torch), lazy-loaded.

    Construction is cheap and dependency-free; the heavy ``torch`` + ``cosyvoice``
    imports and model load only happen on the first :meth:`synthesize` call. If
    the packages are absent, the failure is a precise :class:`TtsError` rather
    than an import-time crash.
    """

    def __init__(self, section: TtsSection) -> None:
        self._section = section
        self._voice = section.voice or "中文女"
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
            fp16=False,
            use_flow_cache=False,
        )
        return self._model

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

        Args mirror :class:`~jarvis.tts.types.SpeechSynthesizer`; ``speed`` and
        ``volume`` are advisory and ignored by this engine (CosyVoice controls
        prosody through its own prompt/stream knobs).
        """
        del speed, volume  # advisory for this engine
        model = self._ensure_loaded()
        spk = voice or self._voice
        import numpy as np  # lazy: only when actually synthesizing

        # CosyVoice2.inference yields dicts with a 'tts_speech' torch tensor.
        generated = model.inference(text, spk, stream=False)  # type: ignore[attr-defined]
        for index, item in enumerate(generated):
            if should_stop is not None and should_stop():
                break
            speech = item["tts_speech"]
            if hasattr(speech, "squeeze"):
                speech = speech.squeeze(0)
            pcm = (np.asarray(speech.cpu().numpy(), dtype="<f4") * 32767.0).astype("<i2").tobytes()
            is_last = index == len(generated) - 1
            yield AudioChunk(
                audio=pcm,
                sample_rate=self.sample_rate,
                is_final=is_last,
                format=FORMAT_PCM_S16LE,
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
        chunks = self._drain(communicate)
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
