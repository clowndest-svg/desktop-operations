"""Does a natural mid-sentence pause split one instruction into two turns?

The clean-speech and noise sweeps both came back at 1% character error and immune to
level, so the recogniser itself is not what the user is complaining about. The one
remaining mechanism that turns good acoustics into a bad experience is **endpointing**:
the pipeline closes a turn after ``max_silence_ms`` of quiet, and a person who pauses
to think in the middle of a sentence gets their instruction cut in half -- the first
half answered on its own, the second half heard as a new request. Nothing about the
model is wrong; the *cut* is.

This measures it directly: the same sentence with a 0 / 700 / 1200 ms pause inserted,
run through the real segmenter at three different ``max_silence_ms`` settings.

    .venv/Scripts/python scripts/bench_asr_endpoint.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))

from bench_asr import CAPTURE_RATE, CASES, cer, to_capture_rate

from jarvis.asr.engines import SenseVoiceAsrEngine
from jarvis.config.schema import AsrSection, TtsSection
from jarvis.tts.engines import EdgeTtsEngine
from jarvis.vad.engines import SileroVadEngine
from jarvis.vad.segmenter import VoiceActivitySegmenter

SENTENCE = "帮我把临时文件清理一下然后看看磁盘还剩多少空间"
GAPS_MS = (0, 700, 1200)
SETTINGS_MS = (500, 800, 1200)
"""The shipped default is 500; the two above it are what we are deciding between."""

TAIL_SILENCE_S = 1.4


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
    asr = SenseVoiceAsrEngine(
        AsrSection(
            enabled=True,
            engine="sensevoice",
            model="iic/SenseVoiceSmall",
            language="zh",
            temperature=0.0,
            beam_size=5,
            device="cpu",
            punctuation=False,
        )
    )

    def synthesise(text: str) -> np.ndarray:
        pcm24 = b"".join(chunk.audio for chunk in tts.synthesize(text))
        return np.frombuffer(to_capture_rate(pcm24, tts.sample_rate), dtype="<i2")

    def segment_and_transcribe(pcm: np.ndarray, max_silence_ms: int) -> list[str]:
        segmenter = VoiceActivitySegmenter(
            SileroVadEngine(),
            sample_rate=CAPTURE_RATE,
            threshold=0.5,
            min_speech_ms=250,
            max_silence_ms=max_silence_ms,
            speech_pad_ms=100,
            max_speech_ms=0,
        )
        data = pcm.tobytes() + bytes(int(CAPTURE_RATE * TAIL_SILENCE_S) * 2)
        buffer = bytearray()
        out: list[str] = []
        frame = 1024
        for offset in range(0, len(data), frame):
            chunk = data[offset : offset + frame]
            buffer.extend(chunk)
            for event in segmenter.feed(chunk):
                if event.type.name == "SPEECH_END" and event.segment is not None:
                    out.append(asr.recognize(bytes(buffer), segment=event.segment).text.strip())
                    # ``VoicePipeline._on_speech_end`` clears and resets between
                    # spans; without this the second segment re-reads the first one
                    # and the measured error is the harness's, not the product's.
                    buffer.clear()
                    segmenter.reset()
        return out

    base = synthesise(SENTENCE)
    print(f"原文：{SENTENCE}\n")
    for gap in GAPS_MS:
        pause = int(CAPTURE_RATE * gap / 1000)
        cut = len(base) // 2
        pcm = (
            base
            if gap == 0
            else np.concatenate([base[:cut], np.zeros(pause, dtype="<i2"), base[cut:]])
        )
        print(f"句中停顿 {gap:>4}ms（总时长 {len(pcm) / CAPTURE_RATE:.1f}s）")
        for setting in SETTINGS_MS:
            spans = segment_and_transcribe(pcm, setting)
            joined = "".join(spans)
            verdict = "被劈成两半" if len(spans) > 1 else "整句保留"
            print(
                f"   max_silence_ms={setting:>4}  {len(spans)} 段  {verdict}"
                f"  CER={cer(SENTENCE, joined) * 100:4.1f}%"
            )
            if len(spans) > 1:
                print(f"        {spans}")
        print("")

    # A second sentence, to make sure this is about pausing and not one unlucky clip.
    print("对照（另一句，同样插 700ms 停顿）：")
    other = CASES[3]
    base2 = synthesise(other)
    cut2 = len(base2) // 2
    pcm2 = np.concatenate(
        [base2[:cut2], np.zeros(int(CAPTURE_RATE * 0.7), dtype="<i2"), base2[cut2:]]
    )
    for setting in SETTINGS_MS:
        spans = segment_and_transcribe(pcm2, setting)
        print(
            f"   max_silence_ms={setting:>4}  {len(spans)} 段  "
            + ("被劈成两半" if len(spans) > 1 else "整句保留")
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
