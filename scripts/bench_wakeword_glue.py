"""Benchmark the wake-word pipeline GLUE overhead (no native dependencies).

This measures only the pure-Python parts we own:
  * FrameAssembler  — arbitrary chunks -> exact engine frames
  * WakeWordDetector — threshold + cooldown debouncing

It does NOT load OpenWakeWord / Porcupine (those need the optional
`voice` extra + network-fetched models). So the number below is a
*lower bound* on total per-frame cost: the engine's own ONNX/TFLite
inference is the dominant, still-unmeasured term.

Run:
    .venv/Scripts/python.exe scripts/bench_wakeword_glue.py
On a networked machine with the voice extra installed you can also pass
`--with-engine openwakeword` to include real inference in the timing.
"""

from __future__ import annotations

import argparse
import random
import time
from dataclasses import dataclass

from jarvis.audio.frames import FrameAssembler
from jarvis.wakeword.detector import WakeWordDetector
from jarvis.wakeword.types import WakeHit, WakeWordEngine


@dataclass
class _FakeEngine:
    """Deterministic-ish engine: returns a low score most frames."""

    frame_samples: int = 1280

    @property
    def name(self) -> str:
        return "fake"

    def process(self, frame: bytes) -> tuple[WakeHit, ...]:
        # Occasional "near-threshold" score to exercise the detector path.
        score = random.random() * 0.4
        return (WakeHit(keyword="hey_jarvis", score=score),)

    def close(self) -> None:
        pass


def _real_engine(frame_samples: int, keywords: list[str]):
    from jarvis.wakeword.engines import OpenWakeWordEngine

    return OpenWakeWordEngine(keywords)


def run(frame_samples: int, engine: WakeWordEngine, seconds: float) -> None:
    total_samples = int(16_000 * seconds)
    chunk_samples = 512  # mirrors WakeWordService._READ_CHUNK_SAMPLES
    bytes_per_sample = 2
    chunk = b"\x00" * (chunk_samples * bytes_per_sample)

    detector = WakeWordDetector(engine, threshold=0.5, cooldown_seconds=2.0)
    assembler = FrameAssembler(frame_samples * bytes_per_sample)

    # Warm up (JIT / cache effects are negligible in CPython, but be tidy).
    for _ in range(20):
        for f in assembler.push(chunk):
            detector.feed(f)

    n_frames = 0
    start = time.perf_counter()
    produced = 0
    remaining = total_samples
    while remaining > 0:
        for f in assembler.push(chunk):
            detector.feed(f)
            n_frames += 1
        produced += chunk_samples
        remaining -= chunk_samples
    elapsed = time.perf_counter() - start

    per_frame_us = (elapsed / n_frames) * 1_000_000
    # Real-time budget: one frame every (frame_samples / 16000) seconds.
    realtime_budget_us = (frame_samples / 16_000) * 1_000_000
    pct_of_budget = per_frame_us / realtime_budget_us * 100

    print(f"engine               : {engine.name}")
    print(f"frames processed     : {n_frames}")
    print(f"audio simulated      : {seconds:.1f}s ({total_samples} samples)")
    print(f"wall time            : {elapsed * 1000:.1f} ms")
    print(f"per-frame glue cost  : {per_frame_us:.2f} us")
    print(
        f"real-time budget     : {realtime_budget_us:.1f} us/frame "
        f"(16 kHz, {frame_samples} samples)"
    )
    print(f"glue vs budget       : {pct_of_budget:.3f}% of one frame's wall time")
    print(
        "note                 : excludes engine inference (OpenWakeWord/"
        "Porcupine); that term is the real unknown and must be measured "
        "on a machine with the voice extra installed."
    )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=float, default=5.0)
    ap.add_argument("--with-engine", choices=["openwakeword"], default=None)
    ap.add_argument("--keywords", default="hey_jarvis")
    args = ap.parse_args()

    frame_samples = 1280
    if args.with_engine == "openwakeword":
        engine: WakeWordEngine = _real_engine(frame_samples, args.keywords.split(","))
    else:
        engine = _FakeEngine(frame_samples=frame_samples)  # type: ignore[assignment]
    run(frame_samples, engine, args.seconds)


if __name__ == "__main__":
    main()
