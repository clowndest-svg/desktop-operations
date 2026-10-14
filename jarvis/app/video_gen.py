"""Local AnimateDiff short videos on the same ComfyUI/GPU as image generation."""

from __future__ import annotations

import random
import threading
import time
import urllib.parse
from pathlib import Path
from typing import Any

from jarvis.app.image_gen import ImageService, _EngineError, _status_words
from jarvis.app.media_prompt import TranslationError
from jarvis.config.paths import AppPaths

MOTION_MODEL = "mm_sd15_v3.safetensors"
VIDEO_DEADLINE = 1800.0


def build_video_workflow(
    checkpoint: str, prompt: str, seed: int, frames: int = 16, steps: int = 16
) -> dict[str, Any]:
    """256px + eight-frame contexts verified on the author's 4GB GPU."""
    return {
        "1": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": checkpoint}},
        "2": {"class_type": "CLIPTextEncode", "inputs": {"text": prompt, "clip": ["1", 1]}},
        "3": {"class_type": "CLIPTextEncode", "inputs": {"text": "", "clip": ["1", 1]}},
        "4": {
            "class_type": "EmptyLatentImage",
            "inputs": {"width": 256, "height": 256, "batch_size": frames},
        },
        "9": {
            "class_type": "ADE_StandardUniformContextOptions",
            "inputs": {
                "context_length": 8,
                "context_stride": 1,
                "context_overlap": 2,
                "fuse_method": "pyramid",
                "use_on_equal_length": False,
                "start_percent": 0.0,
                "guarantee_steps": 1,
            },
        },
        "10": {
            "class_type": "ADE_AnimateDiffLoaderGen1",
            "inputs": {
                "model": ["1", 0],
                "model_name": MOTION_MODEL,
                "beta_schedule": "sqrt_linear (AnimateDiff)",
                "context_options": ["9", 0],
            },
        },
        "5": {
            "class_type": "KSampler",
            "inputs": {
                "model": ["10", 0],
                "positive": ["2", 0],
                "negative": ["3", 0],
                "latent_image": ["4", 0],
                "seed": seed,
                "steps": steps,
                "cfg": 7.5,
                "sampler_name": "euler",
                "scheduler": "normal",
                "denoise": 1.0,
            },
        },
        "6": {
            "class_type": "VAEDecode",
            "inputs": {"samples": ["5", 0], "vae": ["1", 2]},
        },
        "7": {
            "class_type": "VHS_VideoCombine",
            "inputs": {
                "images": ["6", 0],
                "frame_rate": 8,
                "loop_count": 0,
                "filename_prefix": f"xiaoye_video_{seed}",
                "format": "video/h264-mp4",
                "pingpong": False,
                "save_output": True,
                "pix_fmt": "yuv420p",
                "crf": 19,
                "save_metadata": True,
                "trim_to_audio": False,
            },
        },
    }


class VideoService:
    name = "video_gen"

    def __init__(self, images: ImageService, paths: AppPaths) -> None:
        self._images = images
        self._paths = paths
        self._lock = threading.RLock()
        self._state: dict[str, Any] = {
            "phase": "idle",
            "detail": "本地 AnimateDiff · 256×256 · 8 帧/秒",
            "last_path": "",
            "last_seconds": 0.0,
            "error": "",
            "frames": 16,
            "steps": 16,
            "model": MOTION_MODEL,
        }

    def start(self) -> None:
        self._paths.videos_dir.mkdir(parents=True, exist_ok=True)

    def stop(self) -> None:
        pass  # ImageService owns and stops the shared engine.

    def available(self) -> bool:
        return True

    def status(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._state)

    def capabilities(self) -> str:
        return (
            "本地视频：AnimateDiff，256×256，8 帧/秒，16/24/32 帧（2/3/4 秒），"
            "使用与生图相同的 ComfyUI。任意语言描述先整句联网翻成英文。"
            "4GB 显卡使用小尺寸短片，生成耗时以实际结果为准。"
        )

    def _fail(self, error: str, source: str = "") -> dict[str, Any]:
        with self._lock:
            self._state.update(phase="failed", error=error, detail=error)
        return {
            "ok": False,
            "error": error,
            "path": "",
            "prompt_input": source,
            "prompt": "",
            "prompt_note": "",
            "preview": "",
            "seconds": 0.0,
        }

    def generate(
        self, prompt: str, *, frames: object = None, steps: object = None
    ) -> dict[str, Any]:
        source = str(prompt or "").strip()
        count = frames if type(frames) is int and frames in (16, 24, 32) else 16
        iterations = steps if type(steps) is int and 12 <= steps <= 30 else 16
        if not self._images.generation_lock.acquire(blocking=False):
            return {"ok": False, "error": "图片或视频正在生成，请完成后再试。", "path": ""}
        started = time.monotonic()
        try:
            with self._lock:
                self._state.update(phase="translating", detail="正在整句翻译为英文…", error="")
            translated = self._images.translator.translate(source)
            with self._lock:
                self._state.update(phase="generating", detail="正在本地生成短视频…")
            probe = self._images._find_engine()
            if not probe["ok"]:
                raise _EngineError(probe["detail"])
            base = probe["url"]
            info = self._images._get_json(f"{base}/object_info", 15)
            if not isinstance(info, dict) or any(
                node not in info
                for node in (
                    "ADE_AnimateDiffLoaderGen1",
                    "ADE_StandardUniformContextOptions",
                    "VHS_VideoCombine",
                )
            ):
                raise _EngineError(
                    "当前 ComfyUI 未安装视频组件。请使用附带的本地视频引擎，"
                    "或运行 scripts/setup_local_video.ps1 后在生图设置中指定该引擎。"
                )
            if not probe["checkpoints"]:
                raise _EngineError("本地引擎没有 SD1.5 图像基础模型。")
            graph = build_video_workflow(
                probe["checkpoints"][0],
                translated.english,
                random.randrange(1, 2**62),
                count,
                iterations,
            )
            pid = self._images._submit(base, graph)
            filename, blob = self._wait(base, pid)
            directory = self._paths.videos_dir
            directory.mkdir(parents=True, exist_ok=True)
            destination = directory / Path(filename.replace("\\", "/")).name
            if destination.suffix.lower() != ".mp4" or len(blob) < 32 or blob[4:8] != b"ftyp":
                raise _EngineError("引擎没有返回有效的 MP4 视频。")
            destination.write_bytes(blob)
            seconds = round(time.monotonic() - started, 1)
            with self._lock:
                self._state.update(
                    phase="ready",
                    detail=f"视频已生成，用了 {seconds} 秒。",
                    last_path=str(destination),
                    last_seconds=seconds,
                    error="",
                )
            return {
                "ok": True,
                "path": str(destination),
                "error": "",
                "prompt_input": source,
                "prompt": translated.english,
                "prompt_note": translated.note,
                "seconds": seconds,
                "frames": count,
                "fps": 8,
                "size": "256x256",
                "steps": iterations,
                "duration": count / 8,
            }
        except (TranslationError, _EngineError, OSError) as exc:
            return self._fail(str(exc), source)
        finally:
            self._images.generation_lock.release()

    def _wait(self, base: str, pid: str) -> tuple[str, bytes]:
        deadline = time.monotonic() + VIDEO_DEADLINE
        while time.monotonic() < deadline:
            history = self._images._get_json(f"{base}/history/{pid}", 15)
            entry = history.get(pid) if isinstance(history, dict) else None
            if isinstance(entry, dict):
                if entry.get("status", {}).get("status_str") == "error":
                    raise _EngineError(_status_words(entry))
                for output in entry.get("outputs", {}).values():
                    for item in output.get("gifs", []) + output.get("videos", []):
                        if str(item.get("filename", "")).lower().endswith(".mp4"):
                            query = urllib.parse.urlencode(
                                {
                                    key: item.get(key, "")
                                    for key in ("filename", "subfolder", "type")
                                }
                            )
                            return str(item["filename"]), self._images._fetch(
                                f"{base}/view?{query}"
                            )
                if entry.get("status", {}).get("completed"):
                    raise _EngineError("引擎运行结束但没有输出 MP4。")
            time.sleep(1)
        raise _EngineError("等待本地视频超过 30 分钟，请查看引擎日志后重试。")
