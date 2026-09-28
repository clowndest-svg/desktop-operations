"""Probe real wake-word reliability against the live microphone.

The shipped `wakeword.threshold` (0.5) is a guess. This measures the truth: it
records the raw OpenWakeWord score distribution over a silence baseline and a
speaking window, so a threshold can be picked from data instead of hope.

Nothing is written to disk -- only scores and timestamps are reported.

Run:
    .venv/Scripts/python.exe scripts/probe_wakeword.py
    .venv/Scripts/python.exe scripts/probe_wakeword.py --seconds 60 --device 14
"""

from __future__ import annotations

import argparse
import logging
import sys
import time

from jarvis.audio import SounddeviceSource
from jarvis.config import ConfigService
from jarvis.wakeword import OpenWakeWordEngine

BASELINE_SECONDS = 5.0
"""Window used to measure what the model scores when nobody is talking."""

PEAK_REPORT_COUNT = 12


def build_engine(kind: str, keywords: list[str]) -> object:
    """Wake engine to probe; ``asr`` has to load the recognition model first."""
    if kind == "openwakeword":
        return OpenWakeWordEngine(keywords)

    from jarvis.asr.engines import SenseVoiceAsrEngine
    from jarvis.config.loader import load_defaults
    from jarvis.config.schema import AsrSection
    from jarvis.vad.engines import SileroVadEngine
    from jarvis.vad.segmenter import VoiceActivitySegmenter
    from jarvis.wakeword import AsrWakeWordEngine

    print("加载 SenseVoice 模型（约 25 秒）...")
    raw = dict(load_defaults()["asr"])
    raw["enabled"] = True
    return AsrWakeWordEngine(
        keywords=keywords,
        transcriber=SenseVoiceAsrEngine(AsrSection.from_mapping(raw)),
        segmenter=VoiceActivitySegmenter(SileroVadEngine(), sample_rate=16_000),
    )


def report(scores: list[tuple[float, float]], label: str, thresholds: list[float]) -> None:
    """Summarize one phase's (timestamp, score) samples for the operator."""
    if not scores:
        print(f"  {label}: 无命中")
        return
    peaks = sorted(scores, key=lambda item: item[1], reverse=True)[:PEAK_REPORT_COUNT]
    print(f"  {label}: 帧数 {len(scores)}  峰值 {max(s for _, s in scores):.3f}")
    for threshold in thresholds:
        hits = sum(1 for _, s in scores if s >= threshold)
        print(f"    阈值 {threshold:.2f} -> 命中 {hits} 帧")
    print("    最高几次（相对该阶段起点的秒数）:", end=" ")
    base = scores[0][0]
    print(", ".join(f"{at - base:.2f}s={value:.3f}" for at, value in peaks[:6]))


def describe_input_device(device: int | None) -> str:
    """Name the microphone we are about to grab, so a virtual device is obvious."""
    try:
        import sounddevice as sd
    except ImportError:
        return "（sounddevice 未安装）"
    try:
        info = sd.query_devices(kind="input") if device is None else sd.query_devices(device)
        return f"{info['name']} @ {int(info['default_samplerate'])}Hz"
    except Exception as exc:
        return f"（查询失败：{type(exc).__name__}）"


def probe(
    seconds: float, device: int | None, thresholds: list[float], keywords: list[str], kind: str
) -> int:
    """Sample the microphone and report wake scores per phase; returns exit code."""
    print(f"唤醒引擎：{kind}")
    print(f"唤醒词：{', '.join(keywords)}")
    print(f"输入设备：{describe_input_device(device)}")
    source = SounddeviceSource(device=device)
    engine = build_engine(kind, keywords)
    frame_bytes = engine.frame_samples * 2  # type: ignore[attr-defined]
    print(f"每帧 {engine.frame_samples} 采样 / {frame_bytes} 字节，开始采集 {seconds:.0f} 秒\n")

    baseline: list[tuple[float, float]] = []
    spoken: list[tuple[float, float]] = []
    started = time.perf_counter()
    try:
        source.open()
    except Exception as exc:
        print(f"麦克风打不开：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    next_note = BASELINE_SECONDS
    try:
        print(f"[0-{BASELINE_SECONDS:.0f}s] 静音基线 —— 请保持安静")
        while True:
            chunk = source.read(engine.frame_samples)
            at = time.perf_counter() - started
            for hit in engine.process(chunk):
                (baseline if at < BASELINE_SECONDS else spoken).append((at, hit.score))
            if at >= seconds:
                break
            if at >= next_note:
                print(f"    [{at:.0f}s] 剩余 {seconds - at:.0f}s，请重复说唤醒词", flush=True)
                next_note = at + 5.0
    except KeyboardInterrupt:
        print("\n提前结束")
    finally:
        source.close()
        engine.close()

    print()
    report(baseline, "静音基线", thresholds)
    report(spoken, "说话窗口", thresholds)
    if kind == "asr":
        print(
            "\n判读：asr 引擎只在整句转写里匹配到关键词时才算命中，没有分数分布。"
            "若说话窗口为 0 命中，看上面的 DEBUG 行确认识别到底听成了什么。"
        )
    else:
        print(
            "\n判读：阈值要明显高于静音基线峰值，又低于你说话时的峰值；两者重叠说明这个关键词不可用。"
        )
    return 0


def main(argv: list[str] | None = None) -> int:
    """Parse arguments and run the probe."""
    parser = argparse.ArgumentParser(
        prog="probe_wakeword", description="Measure real wake-word score distribution."
    )
    parser.add_argument("--seconds", type=float, default=45.0, help="total capture time")
    parser.add_argument("--device", type=int, default=None, help="sounddevice input index")
    parser.add_argument("--keywords", default=None, help="comma separated; default = config's")
    parser.add_argument("--thresholds", default="0.3,0.5", help="comma separated cut-offs")
    parser.add_argument(
        "--engine",
        choices=("openwakeword", "asr"),
        default="openwakeword",
        help="asr = wake by matching the transcript (use this for Chinese keywords)",
    )
    args = parser.parse_args(argv)

    if args.engine == "asr":
        # Seeing what the recogniser actually heard is the whole diagnostic when
        # a Chinese wake word refuses to fire.
        logging.basicConfig(level=logging.DEBUG, format="%(levelname)s %(name)s %(message)s")
        thresholds = [1.0]
    else:
        thresholds = [float(item) for item in args.thresholds.split(",") if item]
    if args.keywords:
        keywords = [item.strip() for item in args.keywords.split(",") if item.strip()]
    else:
        config_service = ConfigService(config_file=None)
        config_service.start()
        try:
            keywords = list(config_service.config.wakeword.keywords)
        finally:
            config_service.stop()

    phase = BASELINE_SECONDS + 3.0
    print(f"提示：{phase:.0f} 秒后进入说话窗口，届时请对麦克风重复说唤醒词 5 次以上。\n")
    time.sleep(1.0)
    return probe(args.seconds, args.device, thresholds, keywords, args.engine)


if __name__ == "__main__":
    sys.exit(main())
