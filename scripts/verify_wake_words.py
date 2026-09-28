"""Verify the configured wake words against real speech recognition -- no microphone.

Why this exists
---------------
``wakeword.keywords`` for the ``asr`` engine is a claim about what a model will
transcribe, and claims like that rot silently: "你好小夜" was measured to come back
as "你好小叶" in every recording tried, so an exact-match keyword list wakes *never*
while the code, the config and the logs all look correct.

So this measures it. Each phrase is synthesised with Edge-TTS at several voices and
speeds, pushed through the real Silero VAD and SenseVoice model, and matched by the
same :class:`~jarvis.wakeword.asr_engine.AsrWakeWordEngine` the assistant runs. No
microphone, no human, and therefore repeatable on a laptop in a quiet room.

Run:
    .venv/Scripts/python.exe scripts/verify_wake_words.py
    .venv/Scripts/python.exe scripts/verify_wake_words.py --phrases 你好小夜 今天星期几

Exits non-zero when a phrase fails, so it can gate a release.
"""

from __future__ import annotations

import argparse
import logging
import sys
import tempfile
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.record_demo import synthesize_question

from jarvis.app.demo import DEMO_SAMPLE_RATE
from jarvis.config import ConfigService
from jarvis.config.schema import AsrSection, WakeWordSection
from jarvis.vad.engines import SileroVadEngine
from jarvis.vad.segmenter import VoiceActivitySegmenter
from jarvis.wakeword.asr_engine import AsrWakeWordEngine

VOICES: tuple[tuple[str, str], ...] = (
    ("zh-CN-XiaoxiaoNeural", "+0%"),
    ("zh-CN-XiaoyiNeural", "+0%"),
    ("zh-CN-YunxiNeural", "+0%"),
    ("zh-CN-YunjianNeural", "+20%"),
    ("zh-CN-XiaoxiaoNeural", "+30%"),
    ("zh-CN-YunyangNeural", "-15%"),
)
"""Six speaker/speed combinations.

One voice proves the transcription, not the robustness: the point of the homophone
variants in the keyword list is that different speakers get mis-heard differently.
"""

_FRAME_BYTES = 512 * 2

TAIL_SILENCE_MS = 900
"""Silence appended to every synthesised clip.

The VAD only reports "the user finished speaking" after ``vad.max_silence_ms`` of
quiet, so a clip that ends the instant the words do never produces a segment -- and
the wake engine is never asked. On a real microphone the room keeps feeding frames
after the sentence, so this padding is not cheating: it is the part of the signal
that a synthesiser does not generate.
"""


def read_pcm(path: Path) -> bytes:
    with wave.open(str(path), "rb") as source:
        frames = source.readframes(source.getnframes())
    return frames + bytes(DEMO_SAMPLE_RATE * 2 * TAIL_SILENCE_MS // 1000)


def transcript_of(asr: object, pcm: bytes) -> str:
    """What the model actually heard, for the line a wake attempt failed on.

    A bare MISS cannot be acted on: "no keyword" covers both "it heard 你好小也"
    (add a variant) and "it heard nothing" (the clip is too fast to segment).
    """
    result = asr.recognize(pcm)  # type: ignore[attr-defined]
    return str(getattr(result, "text", "") or "").strip()


def run_clip(engine: AsrWakeWordEngine, pcm: bytes) -> tuple[str, int]:
    """Feed one clip through the engine. Returns (keyword, hits)."""
    hits = 0
    matched = ""
    for offset in range(0, len(pcm) - _FRAME_BYTES + 1, _FRAME_BYTES):
        for hit in engine.process(pcm[offset : offset + _FRAME_BYTES]):
            hits += 1
            matched = hit.keyword
    return matched, hits


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="验证唤醒词能否被真实识别出来")
    parser.add_argument(
        "--phrases",
        nargs="+",
        default=None,
        help="default: every keyword in the resolved configuration",
    )
    parser.add_argument("--voices", type=int, default=len(VOICES), help="how many voices to use")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s %(message)s")

    config = ConfigService()
    config.start()
    wake: WakeWordSection = config.config.wakeword
    asr_section: AsrSection = config.config.asr
    phrases = tuple(args.phrases or wake.keywords)

    print(f"engine={wake.engine} keywords={list(wake.keywords)}")
    print(f"transcribing with asr.engine={asr_section.engine}; voices={args.voices}")

    asr = build_transcriber(asr_section)
    vad = SileroVadEngine()
    segmenter = VoiceActivitySegmenter(
        vad,
        sample_rate=DEMO_SAMPLE_RATE,
        threshold=config.config.vad.threshold,
        min_speech_ms=config.config.vad.min_speech_ms,
        max_silence_ms=config.config.vad.max_silence_ms,
        speech_pad_ms=config.config.vad.speech_pad_ms,
        max_speech_ms=config.config.vad.max_speech_ms,
    )
    engine = AsrWakeWordEngine(keywords=phrases, transcriber=asr, segmenter=segmenter)

    failures = 0
    attempts = 0
    with tempfile.TemporaryDirectory(prefix="jarvis-wake-probe-") as scratch:
        directory = Path(scratch)
        for index, phrase in enumerate(phrases):
            for voice, rate in VOICES[: max(1, args.voices)]:
                attempts += 1
                wav = directory / f"{index}-{voice}.wav"
                try:
                    synthesize_question(phrase, wav, voice=voice, rate=rate)
                except Exception as exc:  # network / synthesis, not a wake failure
                    print(f"  SKIP {phrase!r} {voice}: {type(exc).__name__}: {exc}")
                    attempts -= 1
                    continue
                pcm = read_pcm(wav)
                heard = transcript_of(asr, pcm)
                matched, hits = run_clip(engine, pcm)
                ok = hits > 0
                failures += 0 if ok else 1
                print(
                    f"  {'OK  ' if ok else 'MISS'} {phrase!r} {voice} {rate}"
                    f" -> 听到 {heard!r} 命中 {matched or '无'}"
                )

    print(f"\n{attempts - failures}/{attempts} woke")
    if failures:
        print("唤醒词回归未通过：把实测到的误识别加进 wakeword.keywords，或换 engine。")
        return 1
    print("全部通过。")
    return 0


def build_transcriber(section: AsrSection) -> object:
    """The real SenseVoice engine, behind the transcriber protocol the wake engine wants."""
    from jarvis.asr.engines import SenseVoiceAsrEngine

    return SenseVoiceAsrEngine(section)


if __name__ == "__main__":  # pragma: no cover - manual tool
    sys.exit(main())
