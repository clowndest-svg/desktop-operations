"""Does the front-end actually need a pre-processor? Measure it before shipping one.

The clean-speech benchmark put SenseVoice at 1% character error, so "recognition is
garbage" has to come from what the microphone delivers. Two candidates, and they need
different fixes:

* **too quiet** → peak normalisation helps, *if* the recogniser is sensitive to level;
* **too noisy** → normalisation makes it *worse* (it amplifies the noise floor with
  the voice), and the honest answer is a gate, or telling the user to move closer.

Guessing wrong here ships a placebo. So this degrades the same known sentences four
ways -- quiet, loud-with-clipping-headroom, and two additive-noise SNRs -- and scores
each against raw and peak-normalised audio. Whatever the numbers say is what gets
implemented, including "nothing, the model already normalises internally".

    .venv/Scripts/python scripts/bench_asr_noise.py
"""

from __future__ import annotations

import sys
import time

from jarvis.asr.engines import SenseVoiceAsrEngine
from jarvis.config.schema import AsrSection, TtsSection
from jarvis.tts.engines import EdgeTtsEngine

sys.path.insert(0, "scripts")
import numpy as np
from bench_asr import CASES, cer, to_capture_rate


def peak_normalise(pcm: np.ndarray, target: float = 0.9, max_gain: float = 12.0) -> np.ndarray:
    """Scale to ``target`` peak, refusing to boost more than ``max_gain`` times.

    The ceiling is the whole point of doing this by hand: an unbounded normaliser
    turns a silent room into a room full of amplified hiss, and the recogniser is
    then handed noise that looks like speech.
    """
    peak = float(np.abs(pcm).max()) if pcm.size else 0.0
    if peak <= 1.0:
        return pcm
    gain = min(max_gain, target * 32767.0 / peak)
    return np.clip(pcm * gain, -32768, 32767)


def add_noise(pcm: np.ndarray, snr_db: float, rng: np.random.Generator) -> np.ndarray:
    signal = float(np.sqrt((pcm.astype(np.float64) ** 2).mean())) or 1.0
    noise = rng.standard_normal(pcm.size) * np.sqrt(signal**2 / 10 ** (snr_db / 10.0))
    return np.clip(pcm + noise, -32768, 32767)


def attenuate(pcm: np.ndarray, db: float) -> np.ndarray:
    return pcm * (10.0 ** (db / 20.0))


def main() -> int:
    tts = EdgeTtsEngine(
        TtsSection(
            enabled=True,
            engine="edge_tts",
            voice="zh-CN-XiaoxiaoNeural",
            speed=1.0,
            volume=1.0,
            device="cpu",
            model="",
        )
    )
    clean: list[tuple[str, np.ndarray]] = []
    for text in CASES:
        pcm24 = b"".join(chunk.audio for chunk in tts.synthesize(text))
        clean.append((text, np.frombuffer(to_capture_rate(pcm24, tts.sample_rate), dtype="<i2")))

    section = AsrSection(
        enabled=True,
        engine="sensevoice",
        model="iic/SenseVoiceSmall",
        language="zh",
        temperature=0.0,
        beam_size=5,
        device="cpu",
        punctuation=False,
    )
    started = time.monotonic()
    asr = SenseVoiceAsrEngine(section)
    print(f"模型加载 {time.monotonic() - started:.1f}s\n")

    rng = np.random.default_rng(20261001)
    conditions: list[tuple[str, np.ndarray]] = []
    for text, pcm in clean:
        conditions.append((text, pcm))
        conditions.append((text, attenuate(pcm, -26).astype("<i2")))
        conditions.append((text, add_noise(pcm, 10.0, rng).astype("<i2")))
        conditions.append((text, add_noise(pcm, 3.0, rng).astype("<i2")))

    labels = ("干净", "安静(-26dB)", "噪声 SNR10", "噪声 SNR3")
    rows: dict[str, list[float]] = {"原始": [], "归一化": []}
    for index, (text, pcm) in enumerate(conditions):
        label = labels[index % 4]
        raw = asr.recognize(pcm.astype("<i2").tobytes()).text
        fixed = asr.recognize(peak_normalise(pcm).astype("<i2").tobytes()).text
        rows["原始"].append(cer(text, raw))
        rows["归一化"].append(cer(text, fixed))
        peak = float(np.abs(pcm.astype(np.float64)).max())
        print(
            f"{label:<12} peak={peak:>7.0f}  原始 {rows['原始'][-1] * 100:>5.1f}%  "
            f"归一化 {rows['归一化'][-1] * 100:>5.1f}%"
        )
        if rows["原始"][-1] > 0.12:
            print(f"             原始读作: {raw}")
        if rows["归一化"][-1] > 0.12:
            print(f"             归一化读作: {fixed}")

    print("\n" + "=" * 46)
    for name, values in rows.items():
        print(f"{name:<10} 平均 CER {np.mean(values) * 100:>5.1f}%")
    for position, label in enumerate(labels):
        raw = values_of(rows["原始"], position)
        fixed = values_of(rows["归一化"], position)
        print(f"  {label:<12} 原始 {raw * 100:>5.1f}%   归一化 {fixed * 100:>5.1f}%")
    print("=" * 46)
    return 0


def values_of(series: list[float], position: int) -> float:
    return float(np.mean([value for index, value in enumerate(series) if index % 4 == position]))


if __name__ == "__main__":
    sys.exit(main())
