"""Copy only public ASR/VAD model weights/configuration, never an author's cache."""

from __future__ import annotations

import argparse
import hashlib
import shutil
from pathlib import Path


def place(source: Path, app: Path) -> None:
    for repo, name in (
        ("iic--SenseVoiceSmall", "SenseVoiceSmall"),
        ("iic--speech_fsmn_vad_zh-cn-16k-common-pytorch", "fsmn-vad"),
    ):
        snapshot = source / "models" / repo / "snapshots/master"
        if not (snapshot / "model.pt").is_file():
            raise ValueError(f"Missing model: {snapshot}")
        expected = {
            "SenseVoiceSmall": "833ca2dcfdf8ec91bd4f31cfac36d6124e0c459074d5e909aec9cabe6204a3ea",
            "fsmn-vad": "b3be75be477f0780277f3bae0fe489f48718f585f3a6e45d7dd1fbb1a4255fc5",
        }[name]
        with (snapshot / "model.pt").open("rb") as handle:
            if hashlib.file_digest(handle, "sha256").hexdigest() != expected:
                raise ValueError(f"Voice model checksum mismatch: {name}")
        target = app / "语音模型" / name
        target.mkdir(parents=True, exist_ok=True)
        for item in snapshot.iterdir():
            if item.is_file() and (
                item.suffix in {".pt", ".yaml", ".json", ".mvn", ".model"}
                or item.name in {"README.md", "LICENSE", "LICENSE.txt"}
            ):
                shutil.copy2(item, target / item.name)
        print(f"Public voice model copied: {name}", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--app-dir", type=Path, required=True)
    args = parser.parse_args()
    place(args.source, args.app_dir)
