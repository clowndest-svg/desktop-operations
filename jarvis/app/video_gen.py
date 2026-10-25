"""Local AnimateDiff videos on the same ComfyUI/GPU as image generation.

分辨率/帧率/时长从 r114 起可调，但**上限不是拍脑袋给的**：

* 默认档是「自动」—— 按 ``/system_stats`` 报回来的显存总量选（4 GB 档给
  256×256·8fps·2 秒，这一组合是 r113 实测过的）；没检测过时按 4 GB 保守处理并写明。
* 手动档超出的部分按显存外推的帧数上限截断，截断这件事写在返回的 ``note`` 里 ——
  悄悄把用户选的 32 秒变成 2 秒就是骗。
* 外推公式只有一个实测锚点（4 GB 空闲 1979 MB 装得下 32 帧@256×256），
  界面上必须写明"上限是外推的，崩了往小调"。
"""

from __future__ import annotations

import json
import logging
import random
import threading
import time
import urllib.parse
from collections.abc import Mapping
from contextlib import ExitStack
from pathlib import Path
from typing import Any, TypedDict

from jarvis.app.image_gen import ImageService, _EngineError, _status_words
from jarvis.app.media_prompt import TranslationError
from jarvis.app.preferences import (
    VIDEO_CLOUD_BODY,
    VIDEO_CLOUD_FAILED,
    VIDEO_CLOUD_HEADERS,
    VIDEO_CLOUD_KEY_ENV,
    VIDEO_CLOUD_MODEL,
    VIDEO_CLOUD_POLL_URL,
    VIDEO_CLOUD_RUNNING,
    VIDEO_CLOUD_STATUS_PATH,
    VIDEO_CLOUD_SUBMIT,
    VIDEO_CLOUD_SUCCEEDED,
    VIDEO_CLOUD_TASK_PATH,
    VIDEO_CLOUD_VIDEO_PATH,
    VIDEO_ENGINE,
    VIDEO_FPS,
    VIDEO_SECONDS,
    VIDEO_SIZE,
    VIDEO_STEPS,
    Preferences,
)
from jarvis.app.video_cloud import (
    DEFAULT_BODY,
    DEFAULT_FAILED,
    DEFAULT_HEADERS,
    DEFAULT_KEY_ENV,
    DEFAULT_MODEL,
    DEFAULT_POLL_URL,
    DEFAULT_RUNNING,
    DEFAULT_STATUS_PATH,
    DEFAULT_SUBMIT_URL,
    DEFAULT_SUCCEEDED,
    DEFAULT_TASK_PATH,
    DEFAULT_VIDEO_PATH,
    CloudVideoClient,
    CloudVideoConfig,
    render_json_body,
)
from jarvis.config.paths import AppPaths
from jarvis.llm import local_server

logger = logging.getLogger("jarvis.app.video_gen")

MOTION_MODEL = "mm_sd15_v3.safetensors"
VIDEO_DEADLINE = 1800.0

ENGINE_CHOICES: tuple[str, ...] = ("auto", "local", "cloud")
"""三档引擎。默认落 local（见 :data:`jarvis.app.preferences.VIDEO_ENGINE`）。"""

# 云端各格子的出厂默认（通义万相一家，能直接跑通）。用户改了就覆盖，没改沿用。
_CLOUD_DEFAULTS: dict[str, str] = {
    VIDEO_CLOUD_SUBMIT: DEFAULT_SUBMIT_URL,
    VIDEO_CLOUD_KEY_ENV: DEFAULT_KEY_ENV,
    VIDEO_CLOUD_MODEL: DEFAULT_MODEL,
    VIDEO_CLOUD_BODY: DEFAULT_BODY,
    VIDEO_CLOUD_HEADERS: DEFAULT_HEADERS,
    VIDEO_CLOUD_TASK_PATH: DEFAULT_TASK_PATH,
    VIDEO_CLOUD_POLL_URL: DEFAULT_POLL_URL,
    VIDEO_CLOUD_VIDEO_PATH: DEFAULT_VIDEO_PATH,
    VIDEO_CLOUD_STATUS_PATH: DEFAULT_STATUS_PATH,
    VIDEO_CLOUD_SUCCEEDED: DEFAULT_SUCCEEDED,
    VIDEO_CLOUD_RUNNING: DEFAULT_RUNNING,
    VIDEO_CLOUD_FAILED: DEFAULT_FAILED,
}

SIZE_CHOICES: tuple[str, ...] = ("256x256", "384x384", "512x512")
FPS_CHOICES: tuple[int, ...] = (8, 12, 16, 24)
SECOND_CHOICES: tuple[int, ...] = (2, 3, 4, 8, 16, 32)
MIN_STEPS, MAX_STEPS = 12, 30
MAX_FRAMES = 256
"""绝对帧数上限：32 秒@8fps。再长 SD1.5 AnimateDiff 会开始漂脸，且一次采样装不下。"""

FALLBACK_VRAM_MB = 4096.0
"""没探测过引擎时按 4 GB 保守处理 —— 宁可少给，不要让用户按一次崩一次。"""


class VideoPlan(TypedDict):
    """一次生成实际会用到的参数。``capped`` 为真时 ``note`` 必须解释截了什么。"""

    size: str
    width: int
    height: int
    fps: int
    seconds: int
    frames: int
    steps: int
    explicit: bool
    """True＝这一档是用户/模型点过名的（偏好或传参）；False＝纯按本机自动。"""
    vram_total_mb: float
    max_frames: int
    capped: bool
    note: str


class CloudPlan(TypedDict):
    """云端那一侧的档位：同样的分层选择，但**不按本机显存截帧**。"""

    size: str
    width: int
    height: int
    fps: int
    seconds: int
    frames: int
    steps: int


def _parse_size(text: object) -> tuple[int, int]:
    raw = str(text or "").strip().lower().replace("×", "x").replace(" ", "")
    width, _, height = raw.partition("x")
    try:
        return int(width), int(height)
    except ValueError:
        return 256, 256


def auto_profile(vram_mb: float) -> tuple[str, int, int]:
    """自动档：显存 → (尺寸, 帧率, 秒数)。都是"短视频"，大卡只是给得宽一点。"""
    if vram_mb >= 10000:
        return "512x512", 16, 8
    if vram_mb >= 6000:
        return "384x384", 12, 4
    return "256x256", 8, 2


def max_frames_for(vram_mb: float, width: int, height: int) -> int:
    """显存能装下几帧。

    锚点是 r113 实测：4 GB 卡（预算按总显存一半 ≈ 2048 MB）32 帧@256×256 能过。
    反推每「百万像素·帧」吃 950 MB —— 这个数把采样注意力和 VAE 解码的账都摊进去了，
    **只有一根锚点，所以界面上要写明这是外推**，并按 4 帧对齐
    （AnimateDiff 的 8 帧上下文窗口按 4 整除最稳）。
    """
    budget = max(512.0, vram_mb * 0.5)
    per_frame_mb = (width * height) / 1_000_000 * 950.0
    frames = int(budget / per_frame_mb)
    frames -= frames % 4
    return max(8, min(MAX_FRAMES, frames))


def _pick_str(
    name: str,
    key: str,
    allowed: tuple[str, ...],
    auto: str,
    given: Mapping[str, object],
    prefs: Preferences,
) -> str:
    raw = given.get(name)
    if raw is not None and str(raw).strip() not in ("", "0", "auto"):
        text = str(raw).strip().lower().replace("×", "x").replace(" ", "")
    else:
        text = prefs.text(key) or auto
    return text if text in allowed else auto


def _pick_int(
    name: str,
    key: str,
    allowed: tuple[int, ...],
    auto: int,
    given: Mapping[str, object],
    prefs: Preferences,
) -> int:
    raw = given.get(name)
    try:
        number = int(str(raw)) if raw not in (None, "", "auto") else 0
    except ValueError:
        number = 0
    if not number:
        number = int(prefs.number(key) or 0)
    return number if number in allowed else auto


def resolve_plan(
    prefs: Preferences,
    vram_mb: float,
    explicit: Mapping[str, object] | None = None,
) -> VideoPlan:
    """把 传入 > 偏好 > 自动 三层合成一份实际计划，并按显存截帧数。

    ``explicit`` 的键是 size/fps/seconds/steps；None 或空串/0 都算「这一格没说话」，
    退回偏好档。数值键存的是 int，必须走 ``number()`` —— 用 ``text()`` 读回来永远是空串。
    """
    given = dict(explicit or {})
    auto_size, auto_fps, auto_seconds = auto_profile(vram_mb)
    chosen_size = _pick_str("size", VIDEO_SIZE, SIZE_CHOICES, auto_size, given, prefs)
    chosen_fps = _pick_int("fps", VIDEO_FPS, FPS_CHOICES, auto_fps, given, prefs)
    chosen_seconds = _pick_int("seconds", VIDEO_SECONDS, SECOND_CHOICES, auto_seconds, given, prefs)
    chosen_steps = _pick_int(
        "steps", VIDEO_STEPS, tuple(range(MIN_STEPS, MAX_STEPS + 1)), 16, given, prefs
    )
    width, height = _parse_size(chosen_size)
    cap = max_frames_for(vram_mb or FALLBACK_VRAM_MB, width, height)
    frames = chosen_seconds * chosen_fps
    capped = frames > cap
    if capped:
        frames = cap
    actual_seconds = round(frames / chosen_fps)
    explicit_used = any(
        given.get(name) not in (None, "", 0, "auto") for name in ("size", "fps", "seconds", "steps")
    ) or bool(
        prefs.text(VIDEO_SIZE)
        or prefs.number(VIDEO_FPS)
        or prefs.number(VIDEO_SECONDS)
        or prefs.number(VIDEO_STEPS)
    )
    note = ""
    if capped:
        note = (
            f"这台机器（显存 {int(vram_mb or FALLBACK_VRAM_MB)} MB，上限约 {cap} 帧）装不下 "
            f"{chosen_seconds} 秒 × {chosen_fps} 帧/秒 = {chosen_seconds * chosen_fps} 帧，"
            f"已截到 {frames} 帧（约 {actual_seconds} 秒）。"
        )
    if not vram_mb:
        note += "还没检测过本机引擎，按 4 GB 保守处理；按「检测本机引擎」可拿到真实显存。"
    return {
        "size": chosen_size,
        "width": width,
        "height": height,
        "fps": chosen_fps,
        "seconds": chosen_seconds,
        "frames": frames,
        "steps": chosen_steps,
        "explicit": explicit_used,
        "vram_total_mb": vram_mb,
        "max_frames": cap,
        "capped": capped,
        "note": note,
    }


def resolve_cloud_plan(
    prefs: Preferences, explicit: Mapping[str, object] | None = None
) -> CloudPlan:
    """云端那侧的时长/尺寸档：同样的 传入>偏好 分层，但**不按本机显存截**。

    云端跑不跑得起、上限多少是服务商的事，拿买家这块 4 GB 卡去截它的参数没道理。
    这里只把用户点过的档原样算成 size/fps/seconds/frames/steps，喂进请求体模板。
    """
    given = dict(explicit or {})
    chosen_size = _pick_str("size", VIDEO_SIZE, SIZE_CHOICES, SIZE_CHOICES[0], given, prefs)
    chosen_fps = _pick_int("fps", VIDEO_FPS, FPS_CHOICES, FPS_CHOICES[0], given, prefs)
    chosen_seconds = _pick_int(
        "seconds", VIDEO_SECONDS, SECOND_CHOICES, SECOND_CHOICES[0], given, prefs
    )
    chosen_steps = _pick_int(
        "steps", VIDEO_STEPS, tuple(range(MIN_STEPS, MAX_STEPS + 1)), 16, given, prefs
    )
    width, height = _parse_size(chosen_size)
    return {
        "size": chosen_size,
        "width": width,
        "height": height,
        "fps": chosen_fps,
        "seconds": chosen_seconds,
        "frames": chosen_seconds * chosen_fps,
        "steps": chosen_steps,
    }


def build_video_workflow(
    checkpoint: str,
    prompt: str,
    seed: int,
    frames: int = 16,
    steps: int = 16,
    *,
    width: int = 256,
    height: int = 256,
    fps: int = 8,
) -> dict[str, Any]:
    """先生成 512px 关键帧，再以低重绘强度生成动作；八帧上下文限制显存。"""
    return {
        "1": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": checkpoint}},
        "2": {"class_type": "CLIPTextEncode", "inputs": {"text": prompt, "clip": ["1", 1]}},
        "3": {"class_type": "CLIPTextEncode", "inputs": {"text": "", "clip": ["1", 1]}},
        "4": {
            "class_type": "EmptyLatentImage",
            "inputs": {"width": 512, "height": 512, "batch_size": 1},
        },
        "12": {
            "class_type": "KSampler",
            "inputs": {
                "model": ["1", 0],
                "positive": ["2", 0],
                "negative": ["3", 0],
                "latent_image": ["4", 0],
                "seed": seed,
                "steps": max(20, steps),
                "cfg": 7.0,
                "sampler_name": "euler",
                "scheduler": "normal",
                "denoise": 1.0,
            },
        },
        "16": {
            "class_type": "LatentUpscale",
            "inputs": {
                "samples": ["12", 0],
                "upscale_method": "bilinear",
                "width": width,
                "height": height,
                "crop": "disabled",
            },
        },
        "13": {
            "class_type": "RepeatLatentBatch",
            "inputs": {"samples": ["16", 0], "amount": frames},
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
                "beta_schedule": "linear (HotshotXL/default)",
                "context_options": ["9", 0],
            },
        },
        "5": {
            "class_type": "KSampler",
            "inputs": {
                "model": ["10", 0],
                "positive": ["2", 0],
                "negative": ["3", 0],
                "latent_image": ["13", 0],
                "seed": seed,
                "steps": steps,
                "cfg": 3.0,
                "sampler_name": "euler",
                "scheduler": "normal",
                "denoise": 0.35,
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
                "frame_rate": fps,
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

    def __init__(self, images: ImageService, prefs: Preferences, paths: AppPaths) -> None:
        self._images = images
        self._prefs = prefs
        self._paths = paths
        self._lock = threading.RLock()
        self._state: dict[str, Any] = {
            "phase": "idle",
            "detail": "本地 AnimateDiff · 默认档按本机显存自动选",
            "last_path": "",
            "last_seconds": 0.0,
            "error": "",
            "model": MOTION_MODEL,
        }

    def start(self) -> None:
        self._paths.videos_dir.mkdir(parents=True, exist_ok=True)

    def stop(self) -> None:
        pass  # ImageService owns and stops the shared engine.

    def available(self) -> bool:
        return True

    def probe(self) -> dict[str, Any]:
        """借生图那条路真问一次引擎（含显存），然后把新的视频状态还给面板。"""
        self._images.probe()
        return self.status()

    def cancel(self) -> str:
        """叫停在跑的视频：共用生图那条取消位 + 引擎中断。"""
        return self._images.cancel()

    def restart_engine(self) -> dict[str, Any]:
        """重启共享的本地引擎，然后把新的视频状态还给面板。"""
        self._images.restart_engine()
        return self.status()

    def _vram_mb(self) -> float:
        """最近一次探测读到的显存总量。没探测过＝0.0，调用方按保守档走。"""
        probe = self._images.status().get("local")
        if isinstance(probe, dict) and probe.get("ok"):
            return float(probe.get("vram_total_mb") or 0.0)
        return 0.0

    def status(self) -> dict[str, Any]:
        vram = self._vram_mb()
        plan = resolve_plan(self._prefs, vram)
        auto_size, auto_fps, auto_seconds = auto_profile(vram)
        with self._lock:
            snapshot = dict(self._state)
        snapshot.update(
            {
                "plan": plan,
                "choice": {
                    "size": self._prefs.text(VIDEO_SIZE),
                    "fps": int(self._prefs.number(VIDEO_FPS) or 0),
                    "seconds": int(self._prefs.number(VIDEO_SECONDS) or 0),
                    "steps": int(self._prefs.number(VIDEO_STEPS) or 0),
                },
                "options": {
                    "sizes": list(SIZE_CHOICES),
                    "fpss": list(FPS_CHOICES),
                    "seconds": list(SECOND_CHOICES),
                    "engines": list(ENGINE_CHOICES),
                    "auto_profile": {
                        "size": auto_size,
                        "fps": auto_fps,
                        "seconds": auto_seconds,
                        "vram_total_mb": vram,
                    },
                },
                "engine": self._engine_choice(),
                "cloud": self._cloud_view(),
                "cloud_defaults": self._cloud_defaults_view(),
            }
        )
        return snapshot

    def capabilities(self) -> str:
        vram = self._vram_mb()
        plan = resolve_plan(self._prefs, vram)
        engine = self._engine_choice()
        cloud_words = (
            "云端已配置" if self.cloud_ready() else "云端没配齐（缺建任务地址或视频地址取法）"
        )
        return (
            f"视频引擎档位：{engine}（默认本地）。本地：AnimateDiff，和生图共用 ComfyUI，"
            "分辨率 256/384/512 方图，帧率 8/12/16/24，时长 2~32 秒，默认按本机显存自动选短视频档；"
            f"当前本地计划：{plan['size']} · {plan['fps']}帧/秒 · {plan['frames']}帧"
            f"（约 {round(plan['frames'] / plan['fps'])} 秒）· {plan['steps']} 步。"
            + (plan["note"] or "")
            + " 本地描述先整句联网翻成英文；本地帧数上限按 4 GB 实测锚点外推，崩了往小调。"
            + " 云端：多家可自填端点与请求体（默认通义万相，复用 DASHSCOPE_API_KEY），"
            f"{cloud_words}；云端按原话直接发给服务商、不按本机显存截帧。"
        )

    # -- 设置（面板下拉走这四个 setter，返回错误话术，空串＝收下了） ----------

    def set_size(self, value: object) -> str:
        text = str(value or "").strip().lower().replace("×", "x").replace(" ", "")
        if text in ("", "auto", "0"):
            self._prefs.set(VIDEO_SIZE, "")
            return ""
        if text not in SIZE_CHOICES:
            return f"视频分辨率只能是：{'、'.join(SIZE_CHOICES)}（或自动）"
        self._prefs.set(VIDEO_SIZE, text)
        return ""

    def set_fps(self, value: object) -> str:
        return self._set_number(VIDEO_FPS, value, FPS_CHOICES, "帧率只能是 8/12/16/24（或自动）")

    def set_seconds(self, value: object) -> str:
        return self._set_number(
            VIDEO_SECONDS, value, SECOND_CHOICES, "时长只能是 2/3/4/8/16/32 秒（或自动）"
        )

    def set_steps(self, value: object) -> str:
        return self._set_number(
            VIDEO_STEPS,
            value,
            tuple(range(MIN_STEPS, MAX_STEPS + 1)),
            f"步数只能在 {MIN_STEPS}~{MAX_STEPS}",
        )

    def _set_number(self, key: str, value: object, allowed: tuple[int, ...], words: str) -> str:
        text = str(value if value is not None else "").strip()
        if text in ("", "0", "auto"):
            self._prefs.set(key, 0)
            return ""
        try:
            number = int(text)
        except ValueError:
            return words
        if number not in allowed:
            return words
        self._prefs.set(key, number)
        return ""

    # -- 引擎档位与云端配置 --------------------------------------------------

    def _engine_choice(self) -> str:
        raw = (self._prefs.text(VIDEO_ENGINE) or "local").strip().lower()
        return raw if raw in ENGINE_CHOICES else "local"

    def set_engine(self, value: object) -> str:
        """切本地/云端/自动。默认本地；只有这一档真存在才收下，别的退回并说明。"""
        raw = str(value if value is not None else "").strip().lower()
        if raw in ("", "0"):
            self._prefs.set(VIDEO_ENGINE, "local")
            return ""
        if raw not in ENGINE_CHOICES:
            return f"视频引擎只能是：{'、'.join(ENGINE_CHOICES)}"
        self._prefs.set(VIDEO_ENGINE, raw)
        return ""

    def reset_cloud(self) -> None:
        """Forget every cloud-video field; readers fall back to the factory default."""
        for key in _CLOUD_DEFAULTS:
            self._prefs.unset(key)

    def _cloud_config(self) -> CloudVideoConfig:
        """未保存的格子用出厂默认；显式空值可关闭轮询或额外请求头。"""

        def value(key: str) -> str:
            return self._prefs.text(key, default=_CLOUD_DEFAULTS[key]).strip()

        return CloudVideoConfig(
            submit_url=value(VIDEO_CLOUD_SUBMIT),
            key_env=value(VIDEO_CLOUD_KEY_ENV),
            model=value(VIDEO_CLOUD_MODEL),
            body=value(VIDEO_CLOUD_BODY),
            headers=value(VIDEO_CLOUD_HEADERS),
            task_path=value(VIDEO_CLOUD_TASK_PATH),
            poll_url=value(VIDEO_CLOUD_POLL_URL),
            video_path=value(VIDEO_CLOUD_VIDEO_PATH),
            status_path=value(VIDEO_CLOUD_STATUS_PATH),
            succeeded=value(VIDEO_CLOUD_SUCCEEDED),
            running=value(VIDEO_CLOUD_RUNNING),
            failed=value(VIDEO_CLOUD_FAILED),
        )

    def _cloud_view(self) -> dict[str, object]:
        """UI 读的那份：回显用户真正存下的值（没存过的格子回出厂默认）。"""
        config = self._cloud_config()
        return {
            "submit_url": config.submit_url,
            "key_env": config.key_env,
            "model": config.model,
            "body": config.body,
            "headers": config.headers,
            "task_path": config.task_path,
            "poll_url": config.poll_url,
            "video_path": config.video_path,
            "status_path": config.status_path,
            "succeeded": config.succeeded,
            "running": config.running,
            "failed": config.failed,
            "key_set": bool(str(self._images._environ.get(config.key_env, "")).strip()),
        }

    @staticmethod
    def _cloud_defaults_view() -> dict[str, str]:
        """出厂默认，用 UI 认得的字段名（不是偏好键名）给出去，当各格的占位提示。"""
        return {
            "submit_url": DEFAULT_SUBMIT_URL,
            "key_env": DEFAULT_KEY_ENV,
            "model": DEFAULT_MODEL,
            "body": DEFAULT_BODY,
            "headers": DEFAULT_HEADERS,
            "task_path": DEFAULT_TASK_PATH,
            "poll_url": DEFAULT_POLL_URL,
            "video_path": DEFAULT_VIDEO_PATH,
            "status_path": DEFAULT_STATUS_PATH,
            "succeeded": DEFAULT_SUCCEEDED,
            "running": DEFAULT_RUNNING,
            "failed": DEFAULT_FAILED,
        }

    def cloud_ready(self) -> bool:
        """够不够开工：端点与取视频路径齐、且钥匙环境变量真的设了。缺一都不算配好。"""
        config = self._cloud_config()
        return config.ready() and bool(str(self._images._environ.get(config.key_env, "")).strip())

    def set_cloud(self, fields: Mapping[str, object]) -> str:
        """存云端配置。只认白名单里的键；值一律按字符串存（钥匙值不落这里，只落它的环境变量名）。

        返回错误话术，空串＝收下了。请求体/额外头这两格先试解析，坏 JSON 当场拒，
        免得存进去、下次生成时才在云端报一句看不懂的话。
        """
        allowed = {
            "submit_url": VIDEO_CLOUD_SUBMIT,
            "key_env": VIDEO_CLOUD_KEY_ENV,
            "model": VIDEO_CLOUD_MODEL,
            "body": VIDEO_CLOUD_BODY,
            "headers": VIDEO_CLOUD_HEADERS,
            "task_path": VIDEO_CLOUD_TASK_PATH,
            "poll_url": VIDEO_CLOUD_POLL_URL,
            "video_path": VIDEO_CLOUD_VIDEO_PATH,
            "status_path": VIDEO_CLOUD_STATUS_PATH,
            "succeeded": VIDEO_CLOUD_SUCCEEDED,
            "running": VIDEO_CLOUD_RUNNING,
            "failed": VIDEO_CLOUD_FAILED,
        }
        errors: list[str] = []
        for raw_key, value in fields.items():
            key = allowed.get(str(raw_key))
            if key is None:
                continue
            text = str(value if value is not None else "").strip()
            if key in (VIDEO_CLOUD_BODY, VIDEO_CLOUD_HEADERS) and text:
                probe = (
                    render_json_body(
                        text,
                        {
                            "prompt": "x",
                            "model": "m",
                            "width": 1,
                            "height": 1,
                            "seconds": 1,
                            "fps": 1,
                            "frames": 1,
                        },
                    )
                    if key == VIDEO_CLOUD_BODY
                    else text
                )
                try:
                    json.loads(probe)
                except json.JSONDecodeError as exc:
                    errors.append("请求体/额外头不是合法 JSON（注意占位符外不要加引号）")
                    logger.warning("云端模板解析失败 %s: %s", key, exc)
                    continue
            self._prefs.set(key, text)
        return "；".join(errors)

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
        self,
        prompt: str,
        *,
        size: object = None,
        fps: object = None,
        seconds: object = None,
        steps: object = None,
        frames: object = None,
    ) -> dict[str, Any]:
        """按引擎档位把一次生成派给本地或云端。frames 是老调用方的兼容口。"""
        source = str(prompt or "").strip()
        given: dict[str, object] = {"size": size, "fps": fps, "seconds": seconds, "steps": steps}
        if frames is not None and seconds in (None, "", 0):
            # 老口径（r113 及以前：frames 16/24/32 @8fps）换算成最近的时长档，帧率同时钉 8。
            try:
                legacy = int(str(frames))
            except (TypeError, ValueError):
                legacy = 0
            if legacy:
                given["seconds"] = min(SECOND_CHOICES, key=lambda row: abs(row * 8 - legacy))
                if given.get("fps") in (None, "", 0):
                    given["fps"] = 8
        engine = self._engine_choice()
        if engine == "cloud":
            return self._generate_cloud(source, given)
        if engine == "auto":
            probe = self._images._find_engine()
            if probe.get("ok") and probe.get("checkpoints"):
                return self._generate_local(source, given)
            if self.cloud_ready():
                return self._generate_cloud(source, given)
            # 两边都不成：走本地，让本地那句"引擎没起来"的真话落在屏上，而不是含糊。
        return self._generate_local(source, given)

    def _generate_local(self, source: str, given: Mapping[str, object]) -> dict[str, Any]:
        vram = self._vram_mb()
        plan = resolve_plan(self._prefs, vram, given)
        if not self._images.generation_lock.acquire(blocking=False):
            return {"ok": False, "error": "图片或视频正在生成，请完成后再试。", "path": ""}
        self._images._cancel.clear()
        self._images._generation_owner = threading.get_ident()
        started = time.monotonic()
        resources = ExitStack()
        try:
            with self._lock:
                self._state.update(phase="translating", detail="正在整句翻译为英文…", error="")
            translated = self._images.translator.translate(source)
            if self._images._cancel.is_set():
                raise _EngineError("已停止生成")
            resources.enter_context(local_server.media_session())
            if self._images._cancel.is_set():
                raise _EngineError("已停止生成")
            probe = self._images._find_engine()
            problem = self._images.gpu_budget_error(probe)
            if problem:
                raise _EngineError(problem)
            if probe.get("ok"):
                fresh_vram = float(probe.get("vram_total_mb") or 0.0)
                if fresh_vram != vram:
                    plan = resolve_plan(self._prefs, fresh_vram, given)
            with self._lock:
                self._state.update(
                    phase="generating",
                    detail=(
                        f"正在本地生成视频：{plan['size']} · {plan['frames']} 帧 · "
                        f"{plan['steps']} 步…"
                    ),
                )
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
                plan["frames"],
                plan["steps"],
                width=plan["width"],
                height=plan["height"],
                fps=plan["fps"],
            )
            self._images._active_base = base
            pid = self._images._submit(base, graph)
            filename, blob = self._wait(base, pid)
            directory = self._paths.videos_dir
            directory.mkdir(parents=True, exist_ok=True)
            destination = directory / Path(filename.replace("\\", "/")).name
            if destination.suffix.lower() != ".mp4" or len(blob) < 32 or blob[4:8] != b"ftyp":
                raise _EngineError("引擎没有返回有效的 MP4 视频。")
            destination.write_bytes(blob)
            elapsed = round(time.monotonic() - started, 1)
            with self._lock:
                self._state.update(
                    phase="ready",
                    detail=f"视频已生成，用了 {elapsed} 秒。",
                    last_path=str(destination),
                    last_seconds=elapsed,
                    error="",
                )
            return {
                "ok": True,
                "engine": "local",
                "path": str(destination),
                "error": "",
                "prompt_input": source,
                "prompt": translated.english,
                "prompt_note": translated.note,
                "seconds": elapsed,
                "frames": plan["frames"],
                "fps": plan["fps"],
                "size": plan["size"],
                "steps": plan["steps"],
                "duration": round(plan["frames"] / plan["fps"], 2),
                "capped": plan["capped"],
                "note": plan["note"],
            }
        except (TranslationError, _EngineError, OSError) as exc:
            return self._fail(str(exc), source)
        finally:
            self._images.release_engine_models()
            resources.close()
            self._images._interrupt_done.wait(12.0)
            self._images._active_base = ""
            self._images._active_prompt = ""
            self._images._generation_owner = 0
            self._images.generation_lock.release()

    def _generate_cloud(self, source: str, given: Mapping[str, object]) -> dict[str, Any]:
        """云端那条：不翻译（服务商多为中文原生），不发本机显存上限，只按用户档位拼请求。"""
        config = self._cloud_config()
        if not config.ready():
            return self._fail(
                "云端视频还没配齐：至少要有「建任务地址」和「视频地址取法」两格。", source
            )
        if not str(self._images._environ.get(config.key_env, "")).strip():
            return self._fail(f"云端视频差钥匙：环境变量 {config.key_env} 没设。", source)
        plan = resolve_cloud_plan(self._prefs, given)
        if not self._images.generation_lock.acquire(blocking=False):
            return {"ok": False, "error": "图片或视频正在生成，请完成后再试。", "path": ""}
        self._images._cancel.clear()
        self._images._generation_owner = threading.get_ident()
        started = time.monotonic()
        try:
            with self._lock:
                self._state.update(
                    phase="generating",
                    detail=(
                        f"正在云端生成视频（{config.model or '默认模型'}）："
                        f"{plan['size']} · {plan['seconds']} 秒…"
                    ),
                    error="",
                )
            client = CloudVideoClient(config, self._images._environ)
            blob, _trace = client.generate(
                source,
                width=plan["width"],
                height=plan["height"],
                seconds=plan["seconds"],
                fps=plan["fps"],
                frames=plan["frames"],
                should_stop=self._images._cancel.is_set,
            )
            directory = self._paths.videos_dir
            directory.mkdir(parents=True, exist_ok=True)
            destination = directory / f"xiaoye_cloud_video_{time.strftime('%Y%m%d-%H%M%S')}.mp4"
            destination.write_bytes(blob)
            elapsed = round(time.monotonic() - started, 1)
            with self._lock:
                self._state.update(
                    phase="ready",
                    detail=f"云端视频已生成，用了 {elapsed} 秒。",
                    last_path=str(destination),
                    last_seconds=elapsed,
                    error="",
                )
            return {
                "ok": True,
                "engine": "cloud",
                "path": str(destination),
                "error": "",
                "prompt_input": source,
                "prompt": source,
                "prompt_note": f"云端（{config.model or '默认模型'}）按原话直发，未翻成英文。",
                "seconds": elapsed,
                "frames": plan["frames"],
                "fps": plan["fps"],
                "size": plan["size"],
                "steps": plan["steps"],
                "duration": round(plan["frames"] / plan["fps"], 2),
                "capped": False,
                "note": "",
            }
        except (_EngineError, OSError) as exc:
            return self._fail(str(exc), source)
        finally:
            self._images._interrupt_done.wait(12.0)
            self._images._generation_owner = 0
            self._images.generation_lock.release()

    def _wait(self, base: str, pid: str) -> tuple[str, bytes]:
        deadline = time.monotonic() + VIDEO_DEADLINE
        while time.monotonic() < deadline:
            if self._images._cancel.is_set():
                raise _EngineError("已停止生成")
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
