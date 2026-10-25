"""Exercise actual application image/video services against a relocated engine."""

from __future__ import annotations

import argparse
import json
import socket
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from jarvis.app.image_gen import ImageService
from jarvis.app.preferences import (
    IMAGE_COMFY_DIR,
    IMAGE_COMFY_URL,
    IMAGE_ENGINE,
    Preferences,
)
from jarvis.app.video_gen import VideoService
from jarvis.config.paths import AppPaths


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--engine", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    paths = AppPaths.from_data_dir(args.output.resolve() / "data")
    paths.ensure()
    prefs = Preferences(paths.preferences_file)
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    prefs.set(IMAGE_ENGINE, "local")
    prefs.set(IMAGE_COMFY_DIR, str(args.engine.resolve() / "ComfyUI"))
    prefs.set(IMAGE_COMFY_URL, f"http://127.0.0.1:{port}")
    images = ImageService(prefs, paths)
    video = VideoService(images, prefs, paths)
    images.start()
    video.start()
    report: dict[str, object] = {"engine": str(args.engine.resolve()), "ok": False}
    try:
        print("Generating actual local PNG", flush=True)
        report["image"] = image = images.generate(
            "A small red bird sitting on a branch, green leaves, daylight", size="256x256", steps=8
        )
        (args.output / "media.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        if not image["ok"]:
            raise RuntimeError(image["error"])
        image["preview"] = ""  # reports need evidence paths, not embedded image data
        print("Generating actual local MP4", flush=True)
        report["video"] = clip = video.generate(
            "A small red bird moving on a branch, gentle breeze, green leaves",
            size="256x256",
            fps=8,
            seconds=2,
            steps=8,
        )
        if not clip["ok"]:
            raise RuntimeError(clip.get("error"))
        report["ok"] = True
    except Exception as error:
        report["error"] = str(error)
    finally:
        images.stop()
        (args.output / "media.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(json.dumps(report, ensure_ascii=False), flush=True)
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
