"""Place a portable, clean image/video engine beside the desktop executable."""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
from pathlib import Path

MODELS = {
    "ComfyUI/models/checkpoints/v1-5-pruned-emaonly-fp16.safetensors": (
        "e9476a13728cd75d8279f6ec8bad753a66a1957ca375a1464dc63b37db6e3916"
    ),
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
            if relative.parts[0] == "venv" and (
                len(relative.parts) < 2 or relative.parts[1] != "Lib"
            ):
                continue  # virtualenv launchers/config retain the author's absolute paths
            if relative.as_posix() == "ComfyUI/extra_model_paths.yaml":
                continue  # never inherit an author's external model locations
            if relative.parts[:2] == ("ComfyUI", "models") and relative.as_posix() not in MODELS:
                continue  # ship the verified model set, not unrelated author experiments
            if (
                relative.parts[0] == "ComfyUI"
                and len(relative.parts) > 1
                and relative.parts[1] in runtime_dirs
            ):
                continue
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            if not target.exists():
                # Mutable Python/runtime files must not share an inode with the
                # author's working engine. Only immutable model weights may link.
                if path.suffix.lower() in {".safetensors", ".ckpt", ".gguf"}:
                    try:
                        os.link(path, target)
                        continue
                    except OSError:
                        pass
                shutil.copy2(path, target)
    print(f"本地图片/视频引擎就绪：{destination}", flush=True)
    # Native libraries used by the relocated Python must not depend on an
    # installed Visual C++ runtime on the buyer's machine.
    for name in (
        "msvcp140.dll",
        "MSVCP140_1.dll",
        "MSVCP140_ATOMIC_WAIT.dll",
        "vcruntime140.dll",
        "vcruntime140_1.dll",
        "VCOMP140.DLL",
    ):
        runtime = destination.parent / "_internal" / name
        if runtime.is_file():
            shutil.copy2(runtime, destination / "python" / name)


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
