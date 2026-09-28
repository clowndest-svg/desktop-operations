"""Record a repeatable voice-chain demo: text questions -> spoken answers.

Each question is synthesized into a 16 kHz WAV, pushed through the real
VAD -> ASR -> LLM -> TTS stages, and its answer written next to it. The models
are loaded once and every stage is timed individually, so the run yields the
latency numbers a demo needs: how long between the question ending and the
answer starting, on a warm pipeline.

Nothing here is production code -- it drives ``VoiceDemoSession`` in a batch
loop because a demo reel needs several turns from one model load.

Run (voice extra installed; a model key in the environment unless --skip-llm):
    .venv/Scripts/python.exe scripts/record_demo.py
    .venv/Scripts/python.exe scripts/record_demo.py --skip-llm --reel
    .venv/Scripts/python.exe scripts/record_demo.py --text "your question"
"""

from __future__ import annotations

import argparse
import asyncio
import io
import json
import sys
import time
import wave
from dataclasses import asdict, dataclass, field
from pathlib import Path

from jarvis.app.demo import (
    DEMO_SAMPLE_RATE,
    VoiceDemoSession,
    read_mono_pcm16,
)
from jarvis.config import ConfigService
from jarvis.core.exceptions import JarvisError

DEFAULT_QUESTIONS = [
    "你好，我想咨询一下，我们店铺准备做活动，优惠券应该怎么设置才更有吸引力？",
    "客户加了群以后都不怎么说话，有什么办法能让群里活跃起来？",
    "售后退款的人特别多，我应该在群里先怎么回复比较合适？",
]

INPUT_VOICE = "zh-CN-XiaoxiaoNeural"
"""Voice used to speak the questions; deliberately different from the reply."""

REEL_GAP_SECONDS = 0.6


@dataclass
class _Turn:
    """One question and everything the chain did with it."""

    question: str
    transcript: str = ""
    reply: str = ""
    answer_wav: str = ""
    utterances: int = 0
    seconds: dict[str, float] = field(default_factory=dict)
    error: str = ""


def synthesize_question(text: str, path: Path, *, voice: str, rate: str) -> float:
    """Speak ``text`` into a 16 kHz mono s16le WAV the pipeline can consume."""
    import edge_tts
    import numpy
    import soundfile

    async def collect() -> bytes:
        buf = io.BytesIO()
        async for part in edge_tts.Communicate(text, voice, rate=rate).stream():
            if part["type"] == "audio":
                buf.write(part["data"])
        return buf.getvalue()

    started = time.perf_counter()
    samples, source_rate = soundfile.read(io.BytesIO(asyncio.run(collect())), dtype="float32")
    if samples.ndim > 1:
        samples = samples.mean(axis=1)
    if source_rate != DEMO_SAMPLE_RATE:
        import librosa

        samples = librosa.resample(samples, orig_sr=source_rate, target_sr=DEMO_SAMPLE_RATE)
    pcm = (numpy.clip(samples, -1.0, 1.0) * 32767.0).astype(numpy.int16)
    with wave.open(str(path), "wb") as sink:
        sink.setnchannels(1)
        sink.setsampwidth(2)
        sink.setframerate(DEMO_SAMPLE_RATE)
        sink.writeframes(pcm.tobytes())
    return time.perf_counter() - started


def run_turn(
    session: VoiceDemoSession,
    turn: _Turn,
    input_wav: Path,
    answer_wav: Path,
    *,
    skip_llm: bool,
) -> None:
    """Drive one question through the four warm stages, timing each."""
    pcm = read_mono_pcm16(input_wav)
    seconds = turn.seconds
    seconds["音频长度"] = round(len(pcm) / 2 / DEMO_SAMPLE_RATE, 2)

    started = time.perf_counter()
    segments = session.listen(pcm)
    seconds["VAD"] = round(time.perf_counter() - started, 2)
    turn.utterances = len(segments)

    started = time.perf_counter()
    turn.transcript = session.transcribe(pcm, segments)
    seconds["ASR"] = round(time.perf_counter() - started, 2)
    if not turn.transcript:
        turn.error = "ASR 没有产出任何文字"
        return

    if skip_llm:
        turn.reply = turn.transcript
    else:
        started = time.perf_counter()
        turn.reply = session.ask(turn.transcript)
        seconds["LLM"] = round(time.perf_counter() - started, 2)

    started = time.perf_counter()
    written, rate = session.speak(turn.reply, answer_wav)
    seconds["TTS"] = round(time.perf_counter() - started, 2)
    seconds["回复采样率"] = float(rate)
    if not written:
        turn.error = "TTS 没有产出音频"
        return
    turn.answer_wav = str(answer_wav)
    seconds["应答耗时"] = round(
        sum(seconds[key] for key in ("VAD", "ASR", "LLM", "TTS") if key in seconds), 2
    )


def write_reel(turns: list[_Turn], path: Path) -> bool:
    """Concatenate every answer with a short gap into one playable WAV."""
    import numpy

    frames: list[bytes] = []
    rate = 0
    for turn in turns:
        if not turn.answer_wav:
            continue
        with wave.open(turn.answer_wav, "rb") as source:
            rate = source.getframerate()
            frames.append(source.readframes(source.getnframes()))
    if not frames or rate == 0:
        return False
    gap = numpy.zeros(int(rate * REEL_GAP_SECONDS), dtype=numpy.int16).tobytes()
    with wave.open(str(path), "wb") as sink:
        sink.setnchannels(1)
        sink.setsampwidth(2)
        sink.setframerate(rate)
        for index, audio in enumerate(frames):
            sink.writeframes(audio)
            if index + 1 < len(frames):
                sink.writeframes(gap)
    return True


def build_parser() -> argparse.ArgumentParser:
    """Command line for the demo recorder."""
    parser = argparse.ArgumentParser(
        prog="record_demo",
        description="Batch-run the voice chain over demo questions and record the answers.",
    )
    parser.add_argument(
        "--text", action="append", default=[], metavar="Q", help="add one question (repeatable)"
    )
    parser.add_argument(
        "--questions",
        type=Path,
        default=None,
        metavar="FILE",
        help="file with one question per line; blank and #-prefixed lines ignored",
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=None,
        metavar="DIR",
        help="default: <Jarvis data dir>/cache/demo, outside the repo",
    )
    parser.add_argument(
        "--skip-llm", action="store_true", help="echo the transcript instead of calling the model"
    )
    parser.add_argument(
        "--reel", action="store_true", help="also concatenate all answers into demo_reel.wav"
    )
    parser.add_argument(
        "--voice", default=INPUT_VOICE, metavar="NAME", help="voice used to speak the questions"
    )
    parser.add_argument("--rate", default="-10%", metavar="PCT", help="question speech rate")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Record every turn, print the summary, and return a process exit code."""
    args = build_parser().parse_args(argv)

    questions = list(args.text)
    if args.questions is not None:
        questions += [
            line.strip()
            for line in Path(args.questions).read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.startswith("#")
        ]
    if not questions:
        questions = list(DEFAULT_QUESTIONS)

    config_service = ConfigService(config_file=None)
    try:
        config_service.start()
    except Exception:
        print("配置加载失败", file=sys.stderr)
        return 1
    config = config_service.config
    out_dir = args.out_dir or (config_service.paths.cache_dir / "demo")
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"演示产物目录：{out_dir}")
    print(f"模型：{config.llm.default_provider}（离线验证请加 --skip-llm）")

    turns: list[_Turn] = []
    failed = 0
    try:
        started = time.perf_counter()
        session = VoiceDemoSession(config)
        print(f"模型加载 {time.perf_counter() - started:.1f}s（一次性冷启动）\n")
        try:
            for index, question in enumerate(questions, start=1):
                turn = _Turn(question=question)
                print(f"[{index}/{len(questions)}] {question}")
                input_wav = out_dir / f"{index:02d}_question.wav"
                answer_wav = out_dir / f"{index:02d}_answer.wav"
                turn.seconds["合成问题"] = round(
                    synthesize_question(question, input_wav, voice=args.voice, rate=args.rate), 2
                )
                try:
                    run_turn(session, turn, input_wav, answer_wav, skip_llm=args.skip_llm)
                except JarvisError as exc:
                    turn.error = str(exc)
                except Exception as exc:  # pragma: no cover - surfaced to the operator
                    turn.error = f"{type(exc).__name__}: {exc}"
                if turn.error:
                    failed += 1
                    print(f"    失败：{turn.error}")
                else:
                    print(f"    转写：{turn.transcript}")
                    print(f"    回答：{turn.reply}")
                    print(f"    耗时：{turn.seconds}")
                turns.append(turn)
        finally:
            session.close()
    finally:
        config_service.stop()

    (out_dir / "manifest.json").write_text(
        json.dumps([asdict(turn) for turn in turns], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"\n清单：{out_dir / 'manifest.json'}")
    if args.reel:
        reel = out_dir / "demo_reel.wav"
        if write_reel(turns, reel):
            print(f"合并音频：{reel}")
        else:
            print("合并音频：没有可用回答，未生成", file=sys.stderr)

    answered = len(turns) - failed
    if failed:
        print(f"{answered}/{len(turns)} 条成功，{failed} 条失败", file=sys.stderr)
        return 1
    total = sum(turn.seconds.get("应答耗时", 0.0) for turn in turns)
    print(f"全部 {answered} 条成功；热态平均应答 {total / max(answered, 1):.1f} 秒")
    return 0


if __name__ == "__main__":
    sys.exit(main())
