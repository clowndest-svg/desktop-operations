"""Place a portable, clean image/video engine beside the desktop executable."""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
from pathlib import Path

MODELS = {
    "ComfyUI/models/animatediff_models/mm_sd15_v3.safetensors": (
        "a8ca0eadb18c6ad6652bca344a458f31780901a87f765bf95a81ef9c5be55f29"
    ),
}


def digest(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def place(source: Path, destination: Path) -> None:
    for relative, expected in MODELS.items():
        path = source / relative
        if not path.is_file() or digest(path) != expected:
            raise ValueError(f"视频模型不完整或校验失败：{path}")
    for relative in (
        "python/python.exe",
        "venv/Lib/site-packages/torch/__init__.py",
        "ComfyUI/main.py",
        "ComfyUI/models/checkpoints/anythingV3_fp16.safetensors",
    ):
        if not (source / relative).is_file():
            raise ValueError(f"本地引擎缺少 {relative}")
    destination.mkdir(parents=True, exist_ok=True)
    excluded = {".git", "__pycache__"}
    runtime_dirs = {"input", "output", "temp", "tmp", "cache", "user"}
    for component in ("python", "venv", "ComfyUI"):
        for path in (source / component).rglob("*"):
            relative = path.relative_to(source)
            if any(part in excluded for part in relative.parts) or not path.is_file():
                continue
            if (
                relative.parts[0] == "ComfyUI"
                and len(relative.parts) > 1
                and relative.parts[1] in runtime_dirs
            ):
                continue
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            if not target.exists():
                try:
                    os.link(path, target)
                except OSError:
                    shutil.copy2(path, target)
    print(f"本地图片/视频引擎就绪：{destination}", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--engine-root", type=Path, default=Path("D:/AI/XiaoYe/engine"))
    parser.add_argument("--build-root", type=Path, default=Path("D:/AI/XiaoYe/build"))
    parser.add_argument("--round", default="r114")
    args = parser.parse_args()
    if not args.round.startswith("r") or not args.round[1:].isdigit():
        raise ValueError("轮次必须为 r 加数字")
    target = args.build_root / args.round / "dist" / "小夜" / "本地引擎"
    place(args.engine_root, target)


if __name__ == "__main__":
    main()
