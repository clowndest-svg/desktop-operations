"""Concrete wake-word engine adapters.

Both engines are *optional* runtime dependencies (installed via the
``voice`` extra). Imports happen lazily inside ``__init__`` so that the
rest of JARVIS — and the whole test suite — works on machines without
them; a missing package surfaces as a precise :class:`WakeWordError` the
moment an engine is actually constructed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from jarvis.core.exceptions import WakeWordError
from jarvis.wakeword.types import WakeHit

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Sequence


def _missing_dependency(package: str, engine: str, exc: ImportError) -> WakeWordError:
    return WakeWordError(
        f"the '{engine}' engine requires the optional '{package}' package "
        "(install with: pip install jarvis-assistant[voice])",
        details={"missing_package": package, "engine": engine},
    )


class OpenWakeWordEngine:
    """OpenWakeWord adapter — offline, key-free, the default engine.

    Uses the pre-trained ``hey_jarvis`` ONNX model (community models for
    other phrases, e.g. Chinese 「贾维斯」, can be dropped into the models
    directory and listed in configuration — training is out of scope here).
    Expects 1280-sample (80 ms) frames of 16 kHz mono s16le PCM.
    """

    _FRAME_SAMPLES = 1280

    def __init__(self, keywords: Sequence[str], *, models_dir: str | None = None) -> None:
        if not keywords:
            raise WakeWordError("at least one wake keyword is required")
        try:
            import numpy
            from openwakeword.model import Model
        except ImportError as exc:
            raise _missing_dependency("openwakeword", "openwakeword", exc) from exc
        try:
            kwargs: dict[str, Any] = {
                "wakeword_models": list(keywords),
                "inference_framework": "onnx",
            }
            if models_dir is not None:
                kwargs["custom_model_paths"] = None  # explicit: we pass paths via names
            self._model = Model(**kwargs)
        except Exception as exc:
            raise WakeWordError(
                "failed to load OpenWakeWord model(s); run "
                "'python -m jarvis.wakeword.download' once to fetch them",
                details={"keywords": list(keywords), "cause": repr(exc)},
            ) from exc
        self._numpy = numpy

    @property
    def name(self) -> str:
        return "openwakeword"

    @property
    def frame_samples(self) -> int:
        return self._FRAME_SAMPLES

    def process(self, frame: bytes) -> tuple[WakeHit, ...]:
        expected = self._FRAME_SAMPLES * 2
        if len(frame) != expected:
            raise WakeWordError(
                "frame size mismatch",
                details={"expected_bytes": expected, "got_bytes": len(frame)},
            )
        pcm = self._numpy.frombuffer(frame, dtype=self._numpy.int16)
        scores: dict[str, float] = self._model.predict(pcm)
        return tuple(
            WakeHit(keyword=keyword, score=float(score)) for keyword, score in scores.items()
        )

    def close(self) -> None:
        # OpenWakeWord holds no native handles that need explicit release.
        self._model.reset()


class PorcupineEngine:
    """Picovoice Porcupine adapter — commercial-grade accuracy, needs a key.

    Porcupine does its own thresholding via per-keyword ``sensitivity``;
    a hit is therefore reported with score 1.0 and the detector-side
    threshold becomes a no-op for this engine. Expects 512-sample (32 ms)
    frames of 16 kHz mono s16le PCM.
    """

    def __init__(
        self,
        keywords: Sequence[str],
        *,
        access_key: str,
        sensitivity: float,
    ) -> None:
        if not keywords:
            raise WakeWordError("at least one wake keyword is required")
        if not access_key:
            raise WakeWordError(
                "Porcupine requires an access key",
                details={
                    "hint": "set the environment variable named by "
                    "wakeword.porcupine.access_key_env"
                },
            )
        try:
            import pvporcupine
        except ImportError as exc:
            raise _missing_dependency("pvporcupine", "porcupine", exc) from exc
        try:
            self._porcupine = pvporcupine.create(
                access_key=access_key,
                keywords=list(keywords),
                sensitivities=[sensitivity] * len(keywords),
            )
        except Exception as exc:
            raise WakeWordError(
                "failed to initialise Porcupine",
                details={"keywords": list(keywords), "cause": repr(exc)},
            ) from exc
        self._keywords = list(keywords)
        self._closed = False

    @property
    def name(self) -> str:
        return "porcupine"

    @property
    def frame_samples(self) -> int:
        return int(self._porcupine.frame_length)

    def process(self, frame: bytes) -> tuple[WakeHit, ...]:
        expected = self.frame_samples * 2
        if len(frame) != expected:
            raise WakeWordError(
                "frame size mismatch",
                details={"expected_bytes": expected, "got_bytes": len(frame)},
            )
        import struct

        pcm = struct.unpack_from(f"<{self.frame_samples}h", frame)
        index = int(self._porcupine.process(pcm))
        if index < 0:
            return ()
        return (WakeHit(keyword=self._keywords[index], score=1.0),)

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._porcupine.delete()
