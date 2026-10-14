"""Exercise the desktop and chat media doors against a real local engine."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from jarvis.app.image_gen import ImageService
from jarvis.app.preferences import (
    IMAGE_COMFY_DIR,
    IMAGE_COMFY_URL,
    IMAGE_COUNT,
    IMAGE_ENGINE,
    Preferences,
)
from jarvis.app.video_gen import VideoService
from jarvis.config.paths import AppPaths
from jarvis.tools.builtins import image_tools, video_tools
from jarvis.ui.desktop import HudBridge


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--engine-url", default="http://127.0.0.1:8190")
    parser.add_argument("--engine-dir", default="D:/AI/XiaoYe/engine/ComfyUI")
    parser.add_argument("--output", type=Path, default=Path("D:/AI/XiaoYe/qa"))
    args = parser.parse_args()
    prefs = Preferences(args.output / "preferences.json")
    prefs.set(IMAGE_COMFY_URL, args.engine_url)
    prefs.set(IMAGE_COMFY_DIR, args.engine_dir)
    prefs.set(IMAGE_ENGINE, "local")
    prefs.set(IMAGE_COUNT, 1)
    paths = AppPaths.resolve({"JARVIS_HOME": str(args.output)})
    images = ImageService(prefs, paths)
    videos = VideoService(images, prefs, paths)
    images.start()
    videos.start()
    bridge = HudBridge.__new__(HudBridge)
    bridge._images, bridge._videos, bridge._paths = images, videos, paths
    results: dict[str, object] = {}
    results["image_popup"] = bridge.image_generate("一只戴着蓝色围巾的橘猫在雪地里跑步", "512x512")
    print(json.dumps(results["image_popup"], ensure_ascii=False, default=str)[:500], flush=True)
    image_handler = {spec.name: handler for spec, handler in image_tools.build(images)}[
        "generate_image"
    ]
    results["image_chat"] = image_handler(
        {"prompt": "Un chat orange portant une écharpe bleue dans la neige", "count": 1}
    )
    print(results["image_chat"], flush=True)
    results["video_popup"] = bridge.video_generate("一只橘猫在草地上散步，动画风格", 16, 16)
    print(json.dumps(results["video_popup"], ensure_ascii=False), flush=True)
    if not results["video_popup"].get("ok"):
        args.output.mkdir(parents=True, exist_ok=True)
        (args.output / "media-results.json").write_text(
            json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return 1
    video_handler = {spec.name: handler for spec, handler in video_tools.build(videos)}[
        "generate_video"
    ]
    results["video_chat"] = video_handler(
        {
            "prompt": "Un chat orange marche dans un jardin, style animation",
            "frames": 16,
            "steps": 16,
        }
    )
    print(results["video_chat"], flush=True)
    args.output.mkdir(parents=True, exist_ok=True)
    results["image_popup"].pop("preview", None)
    (args.output / "media-results.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return 0 if results["image_popup"].get("ok") and "存在：" in str(results["video_chat"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
