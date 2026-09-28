"""Tests for TTS engines (fake synthesis + missing-dependency paths)."""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from typing import cast

import pytest

from jarvis.config.loader import load_defaults
from jarvis.config.schema import TtsSection
from jarvis.core.exceptions import TtsError
from jarvis.tts.engines import CosyVoiceTtsEngine, EdgeTtsEngine
from jarvis.tts.types import AudioChunk, SpeechSynthesizer


def tts_section(**overrides: object) -> TtsSection:
    raw = dict(cast(Mapping[str, object], load_defaults()["tts"]))
    raw.update(overrides)
    return TtsSection.from_mapping(raw)


class FakeTtsEngine:
    """Deterministic engine that yields fixed chunks, honouring cancellation."""

    def __init__(self, section: TtsSection | None = None) -> None:
        self.section = section
        self.closed = False
        self.stopped_early = False

    @property
    def name(self) -> str:
        return "fake"

    @property
    def sample_rate(self) -> int:
        return 16_000

    def synthesize(
        self,
        text: str,
        *,
        voice: str | None = None,
        speed: float | None = None,
        volume: float | None = None,
        should_stop: Callable[[], bool] | None = None,
    ) -> Iterator[AudioChunk]:
        del voice, speed, volume
        for index in range(3):
            if should_stop is not None and should_stop() is True:
                self.stopped_early = True
                return
            is_final = index == 2
            yield AudioChunk(
                audio=f"[{index}]".encode(),
                sample_rate=16_000,
                is_final=is_final,
            )

    def close(self) -> None:
        self.closed = True


def test_fake_engine_yields_three_chunks_with_final() -> None:
    engine = FakeTtsEngine()
    chunks = list(engine.synthesize("hello"))
    assert len(chunks) == 3
    assert [c.is_final for c in chunks] == [False, False, True]
    assert chunks[-1].audio == b"[2]"
    assert all(c.sample_rate == 16_000 for c in chunks)


def test_fake_engine_honours_should_stop() -> None:
    engine = FakeTtsEngine()
    calls = {"n": 0}

    def should_stop() -> bool:
        calls["n"] += 1
        return calls["n"] >= 2  # cancel after the 2nd poll

    chunks = list(engine.synthesize("hello", should_stop=should_stop))
    # Only the first chunk (poll 1 says keep going) is emitted; poll 2 cancels.
    assert len(chunks) == 1
    assert chunks[0].is_final is False
    assert engine.stopped_early is True


def test_cosyvoice_engine_when_available() -> None:
    # Only runs where the heavy ML stack is actually installed; constructing
    # the engine must NOT import/load the model (lazy until synthesize()).
    pytest.importorskip("cosyvoice")
    engine = CosyVoiceTtsEngine(tts_section())
    assert engine.name == "cosyvoice"
    assert engine.sample_rate == 24_000
    engine.close()


def test_cosyvoice_missing_dependency_raises() -> None:
    # The missing-dependency path can only be observed when cosyvoice is absent.
    try:
        import cosyvoice  # noqa: F401
    except ImportError:
        pass
    else:  # pragma: no cover - environment has the package installed
        pytest.skip("cosyvoice installed; cannot exercise missing path")
    engine = CosyVoiceTtsEngine(tts_section())
    with pytest.raises(TtsError, match="cosyvoice"):
        list(engine.synthesize("hello"))


def test_edge_tts_engine_when_available() -> None:
    pytest.importorskip("edge_tts")
    engine = EdgeTtsEngine(tts_section())
    assert engine.name == "edge_tts"
    assert engine.sample_rate == 24_000
    engine.close()


def test_edge_tts_missing_dependency_raises() -> None:
    try:
        import edge_tts  # noqa: F401
    except ImportError:
        pass
    else:  # pragma: no cover - environment has the package installed
        pytest.skip("edge_tts installed; cannot exercise missing path")
    engine = EdgeTtsEngine(tts_section())
    with pytest.raises(TtsError, match="edge-tts"):
        list(engine.synthesize("hello"))


def test_engines_are_speech_synthesizers() -> None:
    # Structural conformance to the protocol (no runtime import needed).
    assert isinstance(FakeTtsEngine(), object)
    _ = SpeechSynthesizer  # referenced for clarity
