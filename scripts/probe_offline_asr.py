"""Recognize recorded QA speech with shipped weights and networking refused."""

from __future__ import annotations

import argparse
import json
import sys
import time
import wave
from collections.abc import Mapping
from pathlib import Path
from typing import cast

import numpy as np
import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from jarvis.asr.engines import SenseVoiceAsrEngine
from jarvis.config.loader import load_defaults
from jarvis.config.schema import AsrSection


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--app-dir", type=Path, required=True)
    parser.add_argument("--audio", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    import funasr  # noqa: F401
    import torch  # noqa: F401

    attempts: list[str] = []

    def refuse(*values: object, **keywords: object) -> None:
        attempts.append("blocked")
        raise AssertionError("Offline ASR must not contact a server")

    requests.sessions.Session.request = refuse  # type: ignore[assignment]
    sys.executable = str(args.app_dir.resolve() / "小夜.exe")
    with wave.open(str(args.audio), "rb") as source:
        rate = source.getframerate()
        pcm = np.frombuffer(source.readframes(source.getnframes()), dtype=np.int16)
        if source.getnchannels() > 1:
            pcm = pcm.reshape(-1, source.getnchannels()).mean(axis=1)
    if rate != 16000:
        pcm = np.interp(np.arange(0, len(pcm), rate / 16000), np.arange(len(pcm)), pcm).astype(
            np.int16
        )
    started = time.monotonic()
    engine = SenseVoiceAsrEngine(
        AsrSection.from_mapping(cast(Mapping[str, object], load_defaults()["asr"]))
    )
    loaded = time.monotonic()
    text = engine.recognize(pcm.tobytes()).text
    report = {
        "ok": bool(text) and not attempts,
        "text": text,
        "load_seconds": round(loaded - started, 2),
        "network_attempts": len(attempts),
        "microphone_opened": False,
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))
    if not report["ok"]:
        raise RuntimeError("Offline ASR verification failed")


if __name__ == "__main__":
    main()
