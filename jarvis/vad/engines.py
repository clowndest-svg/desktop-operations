"""Concrete VAD engine adapters.

Silero VAD is the default engine: open-source, offline, and distributed as a
TorchScript module bundled inside the ``silero-vad`` wheel (so unlike the
wake-word models it never needs a download). Like
the wake-word engines, it is an **optional** runtime dependency (installed
via the ``voice`` extra). Imports happen lazily inside ``__init__`` so the
rest of JARVIS — and the whole test suite — works on machines without it;
a missing package surfaces as a precise :class:`~jarvis.core.exceptions.VadError`
the moment an engine is actually constructed.
"""

from __future__ import annotations

import logging

from jarvis.core.exceptions import VadError
from jarvis.vad.types import DEFAULT_SAMPLE_RATE

logger = logging.getLogger("jarvis.vad.engines")


def _missing_dependency(package: str, engine: str, exc: ImportError) -> VadError:
    return VadError(
        f"the '{engine}' engine requires the optional '{package}' package "
        "(install with: pip install jarvis-assistant[voice])",
        details={"missing_package": package, "engine": engine},
    )


class SileroVadEngine:
    """Silero VAD adapter — offline, key-free, the default VAD engine.

    Silero consumes 512-sample (32 ms) frames of 16 kHz mono s16le PCM and
    returns a per-frame speech probability in ``[0, 1]``. Endpointing
    (where the utterance actually starts/ends, padding, min/max duration)
    is deliberately *not* done here — that lives in
    :class:`jarvis.vad.segmenter.VoiceActivitySegmenter`, keeping this
    engine a pure scorer consistent with the wake-word engines.
    """

    _FRAME_SAMPLES = 512

    def __init__(self, *, sample_rate: int = DEFAULT_SAMPLE_RATE) -> None:
        if sample_rate != DEFAULT_SAMPLE_RATE:
            raise VadError(
                "Silero VAD only supports 16 kHz input",
                details={"sample_rate": sample_rate},
            )
        try:
            import numpy
            import torch
            from silero_vad import load_silero_vad
        except ImportError as exc:
            raise _missing_dependency("silero-vad", "silero", exc) from exc
        try:
            # Reads the .jit model out of the installed wheel; needs a working
            # torch, which is the realistic failure mode here.
            self._model = load_silero_vad()
        except Exception as exc:  # pragma: no cover - environment specific
            raise VadError(
                "failed to load the Silero VAD model",
                details={"cause": repr(exc)},
            ) from exc
        self._numpy = numpy
        self._torch = torch
        self._sample_rate = sample_rate

    @property
    def name(self) -> str:
        return "silero"

    @property
    def frame_samples(self) -> int:
        return self._FRAME_SAMPLES

    def process(self, frame: bytes) -> float:
        expected = self._FRAME_SAMPLES * 2  # s16le = 2 bytes / sample
        if len(frame) != expected:
            raise VadError(
                "frame size mismatch",
                details={"expected_bytes": expected, "got_bytes": len(frame)},
            )
        pcm = (
            self._numpy.frombuffer(frame, dtype=self._numpy.int16).astype(self._numpy.float32)
            / 32768.0
        )
        try:
            # ``load_silero_vad`` returns a TorchScript module whose signature is
            # ``forward(Tensor x, int sr)``: it rejects ndarrays outright, and
            # wants the 1-D frame without a leading batch dimension.
            #
            # ``no_grad`` is not a speed nicety here -- it is what keeps the process
            # from growing. The wrapper is *stateful*: it feeds its own RNN state
            # back into the next call. With grad enabled each frame's graph is
            # therefore chained onto the previous one, so memory climbs by roughly
            # 50 MB/minute of silence, in native allocations that tracemalloc never
            # sees. The library's own helpers wrap the same call (utils_vad.py:127,
            # :211, :506); calling the model directly has to keep that up.
            with self._torch.no_grad():
                out = self._model(self._torch.from_numpy(pcm), self._sample_rate)
        except Exception as exc:  # pragma: no cover - environment specific
            raise VadError("Silero VAD inference failed", details={"cause": repr(exc)}) from exc
        # The graph tensor still carries its autograd history; detach before
        # reading the single scalar it collapses to.
        return float(self._numpy.asarray(out.detach().cpu().numpy()).reshape(-1)[0])

    def close(self) -> None:
        # The ONNX inference session holds no externally-released native handle.
        pass
