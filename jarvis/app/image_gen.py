"""生图：一句话 -> 一张落在磁盘上的 PNG，两条引擎走同一个接口。

为什么是这个形状（和第 97 轮真机量到的事实直接相关）：

* **本地那条只能"驱动"一个已经在跑的 ComfyUI，不能假设自己带了一个。** 作者这台机器上
  ComfyUI 0.39 + AnythingV3 正在 8188 上服务，显存 4096 MB 里它占 2238 MB。把同一套东西
  复制一份再起第二个引擎，实测它能启动、能认 CUDA 设备、能在 22.6 秒后答出 /system_stats，
  然后在 ``load_checkpoint`` 里直接崩 —— 一张 4 GB 的卡放不下两个引擎。所以这里的顺序是
  **先找一个已经在答话的**，找不到、且用户明确指了目录，才去起一个。
* **云端那条是开箱即用的另一半天**：OpenAI 兼容的 ``/images/generations``，地址和模型可配，
  钥匙只进环境变量（见 :data:`IMAGE_KEY_ENV`）。没有钥匙就说没有，不去猜一个地址。
* **`status()` 一个网络请求都不发**。面板每 1.5 秒读它一次，和本机 adb 那条轮询规矩一样
  （见 :meth:`jarvis.app.screen_mirror.ScreenMirrorService.status`）：探测结果记下来带时间戳，
  点「检测引擎」那颗按钮才真去问。

量到的数（作者机器，GTX 1650 4 GB）：512x512 / 20 步 = **22.1 秒**，1024x1024 没量过所以不吹。
"""

from __future__ import annotations

import base64
import contextlib
import dataclasses
import io
import json
import logging
import os
import random
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from enum import StrEnum
from pathlib import Path
from typing import Any, Final, Protocol, TypedDict

from jarvis.app.media_prompt import PromptTranslator, TranslationError, Translator
from jarvis.app.preferences import (
    IMAGE_CLOUD_BASE,
    IMAGE_CLOUD_MODEL,
    IMAGE_COMFY_DIR,
    IMAGE_COMFY_URL,
    IMAGE_COUNT,
    IMAGE_ENGINE,
    IMAGE_KEEP_OUTPUT,
    IMAGE_SIZE,
    IMAGE_STEPS,
    Preferences,
)
from jarvis.config.paths import AppPaths
from jarvis.llm.transport import open_request

logger = logging.getLogger("jarvis.app.image_gen")

IMAGE_KEY_ENV: Final = "XIAOYE_IMAGE_API_KEY"
"""云端生图钥匙住在哪个环境变量里。和 ``DASHSCOPE_API_KEY`` 同一套规矩：只存名字，值不落库。"""

DEFAULT_CLOUD_BASE: Final = ""
"""云端那一格默认是空的。填一个公网默认地址等于把买家的提示词发去一个他没选过的服务。"""

COMFY_PORTS: Final[tuple[int, ...]] = (8188, 8189, 8187, 8182, 8183, 8184, 8185, 8186)
"""「自动找」要试的那些端口，按 ComfyUI 自己会用的顺序排（默认 8188，被占了往后加）。"""

PROBE_TIMEOUT_SECONDS: Final[float] = 1.5
"""探一个端口值多少时间。本机回环答不上来就是没在跑，多等只会让那颗按钮像卡住。"""

SUBMIT_TIMEOUT_SECONDS: Final[float] = 15.0
POLL_TIMEOUT_SECONDS: Final[float] = 10.0
GENERATE_DEADLINE_SECONDS: Final[float] = 900.0
CLOUD_TIMEOUT_SECONDS: Final[float] = 120.0
ENGINE_START_TIMEOUT_SECONDS: Final[float] = 180.0

SIZE_CHOICES: Final[tuple[str, ...]] = ("512x512", "768x768", "1024x1024")
DEFAULT_SIZE: Final = "512x512"
DEFAULT_STEPS: Final = 20
MIN_STEPS: Final = 4
MAX_STEPS: Final = 60

COUNT_CHOICES: Final[tuple[int, ...]] = (1, 2, 4)
"""一次画几张。**只对本地引擎生效** —— 云端那条是 ``n: 1`` 写死的
（见 :meth:`ImageService._generate_cloud`）。"""

DEFAULT_COUNT: Final = 4
"""默认一次画 4 张，量出来的。

为什么不是 1：anythingV3 画「兽头人身的人形」这类分布外的东西，**换种子就翻面**。
r112 照出厂配置量过（一炉四张、8 个种子、64 张逐张看过）：出厂这一串 32 张里
24 张画成猪头人身、**3 张翻成"兽耳少女"**；换成它之前那一串是 6 对 21 错
（`reports/r112-cmp4-tally.md` 是逐张判读表）。比例不高，但**换一个种子就可能撞上** ——
所以一张保证不了就别只给一张：一次四张他自己挑，八炉里**炉炉都至少有一张对的**。

代价是实话：**4 张 83.6 秒，1 张 22.4 秒**（512x512、20 步，GTX 1650 4 GB）。要快就把
「一次画几张」调回 1。"""
MAX_PREVIEW_EDGE: Final = 768
"""回给界面当预览的那张图最长边。原图按原样落盘，预览只是为了让弹窗立刻看得见东西。"""


class Engine(StrEnum):
    """这一格「用哪条引擎」能选的三个值。"""

    AUTO = "auto"
    LOCAL = "local"
    CLOUD = "cloud"


@dataclasses.dataclass(frozen=True, slots=True)
class EngineOption:
    id: str
    label: str
    note: str


ENGINES: Final[tuple[EngineOption, ...]] = (
    EngineOption(
        Engine.AUTO.value,
        "自动（本地优先）",
        "本机有 ComfyUI 在跑就用它（作者这台量到 512x512 用 22 秒）；没有就走云端。"
        "云端没配钥匙时会说清是哪一边缺了，不会悄悄换一条路。",
    ),
    EngineOption(
        Engine.LOCAL.value,
        "只用本地 ComfyUI",
        "图不出这台机器。要先把 ComfyUI 开着（默认端口 8188），或在下面指一个 ComfyUI 的目录"
        "让它没开的时候由本软件来起 —— 注意显存：一张 4 GB 的卡上开两个引擎，"
        "第二个会当场崩在装模型那一步（实测过）。",
    ),
    EngineOption(
        Engine.CLOUD.value,
        "只用云端 API",
        "OpenAI 兼容的 ``/images/generations``（自家网关、硅基流动、魔搭这类都算）。"
        "要填服务地址和钥匙；钥匙只写进环境变量，不进配置文件、也不回显。",
    ),
)


class ImageProbe(TypedDict):
    """一次探测的结果 —— 「这一条路现在能不能走」，不是「刚才有没有走成」。"""

    kind: str
    url: str
    ok: bool
    detail: str
    version: str
    checkpoints: list[str]
    probed_at: float
    vram_total_mb: float
    """GPU 显存总量（MB）。0.0＝设备没报（CPU 引擎、或旧版 ComfyUI 没这个字段）。"""

    vram_free_mb: float
    """当前空闲显存（MB）。视频默认档据此选：4 GB 卡只能给 256×256 短片。"""


class ImageRow(TypedDict):
    id: str
    label: str
    note: str


class ImageSizeRow(TypedDict):
    size: str
    note: str


class ImageCountRow(TypedDict):
    count: int
    note: str


class ImageStatus(TypedDict):
    """面板那一格读的快照。轮询安全：不发网络请求、不起进程。"""

    engine: str
    engines: list[ImageRow]
    sizes: list[ImageSizeRow]
    size: str
    steps: int
    count: int
    counts: list[ImageCountRow]
    comfy_url: str
    comfy_dir: str
    cloud_base: str
    cloud_model: str
    cloud_key_set: bool
    can_start_local: bool
    vendored_engine: bool
    local: ImageProbe | None
    phase: str
    detail: str
    last_path: str
    last_seconds: float
    tail: list[str]
    error: str


class ImageAnswer(TypedDict):
    """一次生成的答复。``error`` 非空时 ``path`` 一定是空 —— 不许半句话都不说就回空串。"""

    ok: bool
    path: str
    """第一张的路径。**不是"就这一张"** —— 一次画几张看 :data:`COUNT_CHOICES`，
    全部的在 ``paths`` 里。留着它是为了让老调用方（面板的预览、``last_path``）不用改。"""

    paths: list[str]
    """这一次落盘的全部图，按引擎给的顺序。回话里要把每一条都写出来 ——
    前端是从回话文本里扫图片路径的，写几条它就画几张。"""

    engine: str
    prompt: str
    prompt_input: str
    """用户/模型**原来说的那句**。中文那句会展开成英文标签（``prompt``），两个都留着，
    面板才能说清"你写的"和"引擎听见的"是两回事 —— 悄悄改写就是假能力的一种。"""

    prompt_note: str
    """Automatically detected language and whole-sentence translation provider."""

    size: str
    steps: int
    seed: int
    seconds: float
    preview: str
    error: str


class Opener(Protocol):
    """``urllib.request.urlopen`` 的形状，测试注进来就能不碰网络。"""

    def __call__(self, request: urllib.request.Request, timeout: float) -> Any: ...


Spawn = Callable[[list[str], Path], subprocess.Popen[bytes]]
"""怎么起那个引擎子进程（测试接缝）。"""


def parse_size(text: object, fallback: str = DEFAULT_SIZE) -> tuple[int, int]:
    """``\"768x512\"`` -> ``(768, 512)``。认不了就退回，不猜。

    两边都得是 8 的倍数：SD 的 VAE 按 8x 下采样，写个 500 进去 ComfyUI 只会回一句
    ``VALUE_ERROR``，而那句话在面板上看起来像"软件坏了"。
    """
    raw = str(text or "").strip().lower().replace("×", "x")
    match = re.fullmatch(r"(\d{3,4})\s*x\s*(\d{3,4})", raw)
    if match is None:
        raw = fallback
        match = re.fullmatch(r"(\d{3,4})\s*x\s*(\d{3,4})", raw)
        if match is None:
            return 512, 512
    width, height = int(match.group(1)), int(match.group(2))
    # 对齐到 8，并把过大的边退回可选表里最大那一档：4 GB 显存画 2048 是会崩的。
    cap = max(int(edge) for edge in SIZE_CHOICES[-1].split("x"))
    return _align8(width, cap), _align8(height, cap)


def _align8(value: int, cap: int) -> int:
    clipped = max(256, min(cap, value))
    return clipped - clipped % 8


def cloud_size(width: int, height: int) -> str:
    """云端那一格要的尺寸串。多数兼容端点只吃固定几档，所以退到最近的一档而不是原样发。"""
    supported = (256, 512, 768, 1024)
    edge = max(width, height)
    best = min(supported, key=lambda item: (abs(item - edge), item))
    return f"{best}x{best}"


def build_workflow(
    checkpoint: str,
    prompt: str,
    negative: str,
    width: int,
    height: int,
    steps: int,
    seed: int,
    filename_prefix: str,
    batch: int = 1,
) -> dict[str, object]:
    """ComfyUI 的图。一张 SD1.5 的标准图，节点 id 用字符串数字（它要的那样）。

    写在这里而不是抄一份 JSON 文件：随包的 ComfyUI 换了节点名，单元测试会指着这一行说
    "positive 不是 ['2', 0] 了"，而埋在数据文件里的那份只会在用户按下去的时候崩。

    ``batch`` 是一次采样出几张。**同一个种子出的是不同的图** —— SD1.5 的噪声张量是
    ``(B, C, H, W)``，一炉里的每一张各拿一份噪声。r112 实测：4 张一炉 83.6 秒
    （每张 20.9 秒），比跑四遍省，而且在 4 GB 卡上装得下（引擎占 2117 MiB、空闲 1979 MiB）。
    """
    return {
        "1": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": checkpoint}},
        "2": {"class_type": "CLIPTextEncode", "inputs": {"text": prompt, "clip": ["1", 1]}},
        "3": {"class_type": "CLIPTextEncode", "inputs": {"text": negative, "clip": ["1", 1]}},
        "4": {
            "class_type": "EmptyLatentImage",
            "inputs": {"width": width, "height": height, "batch_size": max(1, batch)},
        },
        "5": {
            "class_type": "KSampler",
            "inputs": {
                "seed": seed,
                "steps": steps,
                "cfg": 7.0,
                "sampler_name": "euler",
                "scheduler": "normal",
                "denoise": 1.0,
                "model": ["1", 0],
                "positive": ["2", 0],
                "negative": ["3", 0],
                "latent_image": ["4", 0],
            },
        },
        "6": {"class_type": "VAEDecode", "inputs": {"samples": ["5", 0], "vae": ["1", 2]}},
        "7": {
            "class_type": "SaveImage",
            "inputs": {"filename_prefix": filename_prefix, "images": ["6", 0]},
        },
    }


def parse_cloud_answer(payload: object) -> tuple[bytes, str]:
    """云端返回的 JSON -> ``(PNG 字节, 错话)``。两种形状都要认：``b64_json`` 和 ``url``。

    只回一句"没图"是不够的：兼容端点最常见的那次失败是把错误塞在 ``error.message`` 里，
    或者 ``data`` 是空数组，那两种情况用户需要看到的是它自己说的那句话。
    """
    if not isinstance(payload, Mapping):
        return b"", f"返回的不是 JSON 对象，是 {type(payload).__name__}"
    error = payload.get("error")
    if isinstance(error, Mapping):
        return b"", str(error.get("message") or error.get("code") or "云端给了一个 error 字段")
    if isinstance(error, str) and error:
        return b"", error
    items = payload.get("data")
    if not isinstance(items, list) or not items:
        return b"", "云端回了空的 data（提示词被过滤了？还是这个模型不接单张？）"
    first = items[0]
    if not isinstance(first, Mapping):
        return b"", "云端回的 data[0] 不是对象"
    blob = first.get("b64_json")
    if isinstance(blob, str) and blob:
        try:
            return base64.b64decode(blob), ""
        except Exception as exc:
            return b"", f"云端回的 base64 解不开：{type(exc).__name__}"
    url = first.get("url")
    if isinstance(url, str) and url:
        return b"", f"NEED_FETCH:{url}"
    return b"", "云端既没给 b64_json 也没给 url"


class ImageService:
    """生图这一层：两条引擎、一个接口、一份状态。唯一持有引擎子进程的地方。"""

    name = "image_gen"

    def __init__(
        self,
        prefs: Preferences,
        paths: AppPaths,
        *,
        environ: Mapping[str, str] | None = None,
        opener: Opener | None = None,
        spawn: Spawn | None = None,
        sleeper: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        translator: Translator | None = None,
        client_provider: Callable[[], Any] | None = None,
    ) -> None:
        self._prefs = prefs
        self._paths = paths
        self._environ = environ if environ is not None else os.environ
        # 本机地址绕开系统代理，和 LLM 那一侧同一个做法
        # （:func:`jarvis.llm.transport.open_request`）：用户机器上挂着代理时，
        # 一句「连不上 127.0.0.1:8188」会被读成 ComfyUI 坏了。
        self._opener: Opener = opener or as_opener(open_request)
        self._spawn = spawn or _default_spawn
        self._sleep = sleeper
        self._clock = clock
        self.translator: Translator = translator or PromptTranslator(
            environ=self._environ, client_provider=client_provider
        )
        # Serialize GPU jobs shared by image and video entry points.
        self.generation_lock = threading.Lock()
        self._lock = threading.RLock()
        self._proc: subprocess.Popen[bytes] | None = None
        self._phase = "off"
        self._detail = ""
        self._note = ""
        self._probe: ImageProbe | None = None
        self._checkpoint = ""
        self._last_path = ""
        self._last_seconds = 0.0
        self._tail: list[str] = []
        self._reader: threading.Thread | None = None
        # 停止生成用的共享取消位 + 当前在跑的引擎地址（生图和生视频共用一把引擎，
        # 所以这一个 Event 同时服务两条路；轮询循环每轮查它，被置位就 /interrupt 中止）。
        self._cancel = threading.Event()
        self._active_base = ""

    # -- 生命周期 ----------------------------------------------------------

    def start(self) -> None:
        self._paths.images_dir.mkdir(parents=True, exist_ok=True)

    def stop(self) -> None:
        """退出时把起的引擎收掉。只收自己起的那个 —— 用户自己开的 ComfyUI 不许动。"""
        with self._lock:
            proc = self._proc
            self._proc = None
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:  # pragma: no cover - 收不掉也要走
                proc.kill()
        self._phase = "off"

    # -- 停止 / 重启引擎 ---------------------------------------------------

    def cancel(self) -> str:
        """叫停在跑的这一张图/这一段视频：置取消位 + 让引擎中断当前任务。

        返回一句给界面显示的话。取消位被生图/生视频的轮询循环下一轮读到就中止；
        /interrupt 让 ComfyUI 真的停下算，不再白烧显卡。没有任务在跑时置位也无害
        （下一次 generate 开头会 clear）。
        """
        running = self.generation_lock.locked()
        self._cancel.set()
        base = self._active_base
        if base:
            try:
                request = urllib.request.Request(
                    base + "/interrupt",
                    data=b"{}",
                    headers={"Content-Type": "application/json", "User-Agent": _user_agent()},
                    method="POST",
                )
                self._opener(request, 5.0).close()
            except Exception as exc:  # 中断失败也要中止本地等待，别把"停不下来"再抛给用户
                logger.warning("向 %s 发 /interrupt 失败：%s", base, exc)
        return "已请求停止" if running else "当前没有正在生成的任务"

    def restart_engine(self) -> ImageStatus:
        """重启本软件起的本地引擎：先收掉自己起的那个（绝不动用户自己开的 ComfyUI），
        再按指过的目录重新起一个并等它答话。没指目录又没在跑时给一句实话。
        """
        self.cancel()
        self.stop()
        if not self._can_start_local():
            with self._lock:
                self._detail = "没指定引擎目录，无法由软件重启；请自己开着 ComfyUI 或在设置里指目录"
                self._phase = "off"
            return self.status()
        self._try_start_engine(None)
        return self.status()

    def available(self) -> bool:
        """有没有至少一条引擎可能走得通 —— 工具要不要广告出去就看这个。

        本地那一条不问探测结果：探测要按按钮才会做，而"还没探测过"不等于"这台没有引擎"。
        只有档位被钉死在云端、而云端又没配齐的时候，这里才是 False。
        """
        return self._engine_choice() != Engine.CLOUD.value or self._cloud_ready()

    # -- 读 ---------------------------------------------------------------

    def status(self) -> ImageStatus:
        with self._lock:
            return {
                "engine": self._engine_choice(),
                "engines": [
                    {"id": row.id, "label": row.label, "note": row.note} for row in ENGINES
                ],
                "sizes": [{"size": item, "note": _size_note(item)} for item in SIZE_CHOICES],
                "size": self._size(),
                "steps": self._steps(),
                "count": self._count(),
                "counts": [{"count": item, "note": _count_note(item)} for item in COUNT_CHOICES],
                "comfy_url": self._comfy_url(),
                "comfy_dir": self._comfy_dir(),
                "cloud_base": self._cloud_base(),
                "cloud_model": self._cloud_model(),
                "cloud_key_set": self._cloud_key_ready(),
                "can_start_local": self._can_start_local(),
                "vendored_engine": vendored_engine_dir() is not None,
                "local": self._probe,
                "phase": self._phase,
                "detail": self._detail,
                "last_path": self._last_path,
                "last_seconds": self._last_seconds,
                "tail": list(self._tail),
                "error": self._note,
            }

    def probe(self) -> ImageProbe:
        """真去问一次本地引擎在不在。按「检测引擎」和她念状态时走这条路。"""
        found = self._find_engine()
        with self._lock:
            self._probe = found
        return found

    def capabilities(self) -> str:
        """给模型的一段状态话：能做什么、缺什么，一句一句说清。"""
        choice = self._engine_choice()
        lines = [f"生图引擎档位：{self._engine_label(choice)}"]
        local = self._probe
        if local is None:
            lines.append("本地引擎还没检测过（面板上「检测引擎」按一次，或直接试着生成）。")
        else:
            where = local["detail"] or (f"在 {local['url']} 上" if local["ok"] else "没找到")
            lines.append(f"本地引擎：{where}")
        if self._cloud_ready():
            model = self._cloud_model() or "（用端点默认）"
            lines.append(f"云端：{self._cloud_base()}，模型 {model}，钥匙已设")
        else:
            missing = "没配地址" if not self._cloud_base() else f"没配钥匙（{IMAGE_KEY_ENV}）"
            lines.append(f"云端：{missing}")
        lines.append(
            f"尺寸 {self._size()}，步数 {self._steps()}，"
            f"一次画 {self._count()} 张（只对本地引擎）。"
        )
        return "\n".join(lines)

    # -- 写 ---------------------------------------------------------------

    def set_choice(self, value: object) -> str:
        text = str(value or "").strip()
        if text not in {row.id for row in ENGINES}:
            return f"没有「{text}」这一档引擎"
        self._prefs.set(IMAGE_ENGINE, text)
        return ""

    def set_size(self, value: object) -> str:
        """那一格只认表上的尺寸，写歪的不当垃圾收下。

        面板的下拉给不出 ``3000x3000``，能给出来的只有手填或模型；静悄悄存成 1024 就等于
        "我明明选了别的，屏上又弹回来"。生成那一路是另一回事：:func:`parse_size` 会夹到
        最近的一档，并把**实际用的那个数**回在话里。
        """
        text = str(value or "").strip().lower().replace("×", "x").replace(" ", "")
        if text not in SIZE_CHOICES:
            return f"尺寸只能是：{'、'.join(SIZE_CHOICES)}"
        self._prefs.set(IMAGE_SIZE, text)
        return ""

    def set_steps(self, value: object) -> str:
        try:
            steps = int(str(value or "").strip())
        except ValueError:
            return "步数要一个整数"
        if not MIN_STEPS <= steps <= MAX_STEPS:
            return f"步数只能在 {MIN_STEPS} 到 {MAX_STEPS} 之间"
        self._prefs.set(IMAGE_STEPS, steps)
        return ""

    def set_count(self, value: object) -> str:
        # 这一格的规矩和另外三格不同：**留空＝不动**。另外三格留空会因为 int("") 报一句
        # "要一个整数"，而这一格是后加的，老调用方（页面、脚本）不传它 —— 那种时候该
        # 保持原样，不是把已有的张数清掉、也不是回一句没人看得懂的错。
        if value is None or str(value).strip() == "":
            return ""
        try:
            count = int(str(value).strip())
        except ValueError:
            return "张数要一个整数"
        if count not in COUNT_CHOICES:
            return f"张数只能是：{'、'.join(str(row) for row in COUNT_CHOICES)}"
        self._prefs.set(IMAGE_COUNT, count)
        return ""

    def set_comfy_url(self, value: object) -> str:
        text = str(value or "").strip()
        if text and not text.lower().startswith(("http://", "https://")):
            # 手填最常见的形状是 127.0.0.1:8188 —— 替他补上协议，比拒绝友好，且不会补错方向。
            text = f"http://{text}"
        self._prefs.set(IMAGE_COMFY_URL, text)
        with self._lock:
            self._probe = None
        return ""

    def set_comfy_dir(self, value: object) -> str:
        text = str(value or "").strip()
        if text and not (Path(text) / "main.py").is_file():
            return f"{text} 里没有 main.py，那不是一个 ComfyUI 的目录"
        self._prefs.set(IMAGE_COMFY_DIR, text)
        return ""

    def set_cloud(self, base: object, model: object) -> str:
        text = str(base or "").strip().rstrip("/")
        if text and not text.lower().startswith(("http://", "https://")):
            return "服务地址要以 http:// 或 https:// 开头"
        if text.endswith("/images/generations"):
            # 常见的粘贴错误：把完整端点粘进了"基础地址"那一格，于是真发出去的是
            # …/images/generations/images/generations。
            text = text[: -len("/images/generations")].rstrip("/")
        self._prefs.set(IMAGE_CLOUD_BASE, text)
        self._prefs.set(IMAGE_CLOUD_MODEL, str(model or "").strip())
        return ""

    def generate(
        self,
        prompt: str,
        *,
        zh: object = None,
        size: object = None,
        steps: object = None,
        count: object = None,
    ) -> ImageAnswer:
        """Translate the complete original description for both UI and chat.

        ``zh`` remains a compatibility alias for the original in any language.
        Never merge an LLM rewrite into it or discard unknown words.
        """
        raw = str(prompt or "").strip()
        native = str(zh or "").strip()
        self._cancel.clear()
        if not self.generation_lock.acquire(blocking=False):
            return self._fail("本地生成任务正在运行，请等图片或视频完成后再试。")
        try:
            try:
                plan = self.translator.translate(native or raw)
            except TranslationError as exc:
                answer = self._fail(str(exc))
                answer["prompt_input"] = native or raw
                return answer
            answer = self._run(plan.english, size=size, steps=steps, count=count)
        finally:
            with self._lock:
                self._active_base = ""
            self.generation_lock.release()
        answer["prompt_input"] = native or raw
        answer["prompt_note"] = plan.note
        return answer

    def _run(
        self,
        prompt: str,
        *,
        size: object = None,
        steps: object = None,
        count: object = None,
    ) -> ImageAnswer:
        """出一张图。两条引擎都从这里出去，失败一定带一句能照着做的话。"""
        text = str(prompt or "").strip()
        if not text:
            return self._fail("要一句画什么的描述，不能空着。")
        width, height = parse_size(size if size is not None else self._size())
        edge_size = f"{width}x{height}"
        step_count = self._parse_steps(steps)
        # 张数只喂给本地那条：云端是 `n: 1` 写死的（见 `_generate_cloud`），
        # 在这儿解析出来却只传半边，是为了让"要 4 张"在最上面那一层就被收下，
        # 而不是等落到云端才发现自己白说了。
        draw_count = self._parse_count(count)
        chosen = self._engine_choice()
        started = self._clock()
        if chosen in (Engine.AUTO.value, Engine.LOCAL.value):
            try:
                answer = self._generate_local(
                    text, edge_size, width, height, step_count, draw_count, started
                )
            except _EngineError as exc:
                # 逃出来的一句人话也要变成"没画成 + 那句话"，不是把栈抛给面板：
                # 落盘失败（盘满、目录被删、路径过长）走的正是这条路，而那次的图是真的画好了。
                return self._fail(str(exc))
            if answer["ok"] or chosen == Engine.LOCAL.value:
                return answer
            with self._lock:
                local_error = answer["error"]
            if not self._cloud_ready():
                return self._fail(
                    f"本地没走通：{local_error}；云端这边也没配好"
                    f"（地址或 {IMAGE_KEY_ENV} 缺一个），所以没有第二条路可退。"
                )
        if chosen in (Engine.AUTO.value, Engine.CLOUD.value):
            if not self._cloud_ready():
                gap = "服务地址" if not self._cloud_base() else f"钥匙 {IMAGE_KEY_ENV}"
                return self._fail(
                    f"云端这一条还差{gap}。在「设置 → 生图」里填上，或者改用本地那条。"
                )
            try:
                return self._generate_cloud(
                    text, edge_size, width, height, step_count, cloud_size(width, height), started
                )
            except _EngineError as exc:
                return self._fail(str(exc))
        return self._fail("不知道要用哪条引擎。")

    # -- 本地：ComfyUI -----------------------------------------------------

    def _generate_local(
        self,
        prompt: str,
        size: str,
        width: int,
        height: int,
        steps: int,
        count: int,
        started: float,
    ) -> ImageAnswer:
        probe = self._find_engine()
        with self._lock:
            self._probe = probe
        if not probe["ok"]:
            return self._fail(probe["detail"] or "本机没有答话的 ComfyUI。")
        checkpoint = probe["checkpoints"][0] if probe["checkpoints"] else self._checkpoint
        if not checkpoint:
            return self._fail(f"{probe['url']} 答了话，但没报出任何一个权重文件名。")
        seed = random.randrange(1, 2**62)
        prefix = f"xiaoye_{time.strftime('%Y%m%d-%H%M%S')}"
        # 负面词留空是**量出来的**，不是没做：r105 试过 SD1.5 那套惯例
        # （masterpiece/best quality 前置 + worst quality/low quality 负面），
        # 同一台引擎同一权重同一尺寸下"绿色占比"从 0.099 掉到 0.007，前置更惨（0.0003）。
        # 颜色都保不住，谈不上"质量"。要再动这两个参数，先按 N>=4 个种子重测。
        graph = build_workflow(checkpoint, prompt, "", width, height, steps, seed, prefix, count)
        try:
            with self._lock:
                self._active_base = probe["url"]
            pid = self._submit(probe["url"], graph)
            drawn = self._wait_for_images(probe["url"], pid)
        except _EngineError as exc:
            return self._fail(str(exc))
        if not drawn:
            return self._fail(f"{probe['url']} 那一边跑完了却没给出图片。")
        # 一炉几张由引擎说了算：要了 4 张它给回 3 张时，如实存 3 张、如实回 3 条路径，
        # 不补一个空位假装是 4 张。文件名是它自己编的（`…_00001_` 到 `…_00004_`），不撞。
        paths: list[Path] = []
        for filename, blob in drawn:
            try:
                paths.append(self._save(filename, blob))
            except _EngineError as exc:
                return self._fail(str(exc))
        return self._ok(paths, "local", prompt, size, steps, seed, started, drawn[0][1])

    def _find_engine(self) -> ImageProbe:
        """找一个在答话的 ComfyUI。指了地址就只问那个，没指就按端口扫一遍。"""
        explicit = self._comfy_url()
        candidates = (
            [explicit] if explicit else [f"http://127.0.0.1:{port}" for port in COMFY_PORTS]
        )
        last: ImageProbe | None = None
        for base in candidates:
            probe = self._ask_engine(base)
            if probe["ok"]:
                if not explicit and self._prefs.get(IMAGE_COMFY_URL) != base:
                    # 自动找到的地址记下来：下一次只问这一个，别每回从头扫八个端口。
                    self._prefs.set(IMAGE_COMFY_URL, base)
                return probe
            last = probe
        return self._try_start_engine(last)

    def _try_start_engine(self, last: ImageProbe | None) -> ImageProbe:
        """一个都没答话时，只有用户明确指过目录才起一个引擎。

        默认不起是量出来的结论：那张 4 GB 的卡上第二个引擎会在装权重那一步崩，用户看到的
        是"软件一按生图就报错"，而真相是他自己那个 ComfyUI 已经把显存占了。
        """
        reason = last["detail"] if last is not None else "本机端口上没有答话的 ComfyUI"
        if self._proc is not None and self._proc.poll() is None:
            return _failed_probe("", "本软件起的引擎进程还活着，但它没答话（下面有它的输出）")
        if not self._can_start_local():
            return _failed_probe(
                "",
                reason + "。要么自己把 ComfyUI 开着（默认 http://127.0.0.1:8188），"
                "要么在「设置 → 生图」里指一个 ComfyUI 的目录，让本软件在没开的时候替你起。",
            )
        base = f"http://127.0.0.1:{self._engine_port()}"
        if not self._start_engine():
            with self._lock:
                detail = self._detail
            return _failed_probe(base, detail)
        deadline = self._clock() + ENGINE_START_TIMEOUT_SECONDS
        while self._clock() < deadline:
            self._sleep(2.0)
            fresh = self._ask_engine(base)
            if fresh["ok"]:
                with self._lock:
                    self._phase = "idle"
                    self._detail = f"引擎已起在 {base}"
                return fresh
            if self._proc is None or self._proc.poll() is not None:
                break
        with self._lock:
            tail = " | ".join(self._tail[-3:])
            self._phase = "failed"
            self._detail = "起来的引擎没答话"
        return _failed_probe(base, f"按你指的目录起了引擎，它没能答话。{tail}")

    def _ask_engine(self, base: str) -> ImageProbe:
        """问一次 ``/system_stats`` + ``/object_info``，把版本和权重读回来。"""
        try:
            stats = self._get_json(base + "/system_stats", PROBE_TIMEOUT_SECONDS)
        except _EngineError as exc:
            return _failed_probe(base, str(exc))
        version = ""
        kind = ""
        vram_total = 0.0
        vram_free = 0.0
        if isinstance(stats, Mapping):
            system = stats.get("system")
            if isinstance(system, Mapping):
                version = str(system.get("comfyui_version", ""))
            devices = stats.get("devices")
            if isinstance(devices, list) and devices and isinstance(devices[0], Mapping):
                kind = str(devices[0].get("type", ""))
                # 显存字节数：视频默认档要按它选（4 GB 卡只给 256×256 短片）。
                vram_total = _as_mb(devices[0].get("vram_total"))
                vram_free = _as_mb(devices[0].get("vram_free"))
        try:
            info = self._get_json(
                base + "/object_info/CheckpointLoaderSimple", PROBE_TIMEOUT_SECONDS
            )
        except _EngineError as exc:
            return _failed_probe(base, f"{base} 答了版本却没让读权重：{exc}")
        names = _checkpoint_names(info)
        with self._lock:
            self._checkpoint = names[0] if names else ""
        return {
            "kind": "comfyui",
            "url": base,
            "ok": True,
            "detail": (
                f"{base}（ComfyUI {version or '版本未报'}，"
                f"{'GPU ' + kind if kind else '设备未报'}"
                f"{'，显存 ' + str(int(vram_total)) + ' MB' if vram_total else ''}，"
                f"权重 {len(names)} 个）"
            ),
            "version": version,
            "checkpoints": names,
            "probed_at": self._clock(),
            "vram_total_mb": vram_total,
            "vram_free_mb": vram_free,
        }

    def _submit(self, base: str, graph: dict[str, object]) -> str:
        payload = {"prompt": graph, "client_id": "xiaoye"}
        try:
            answer = self._post_json(base + "/prompt", payload, SUBMIT_TIMEOUT_SECONDS)
        except _EngineError as exc:
            raise _EngineError(f"{base} 不肯接这张图：{exc}") from exc
        pid = answer.get("prompt_id") if isinstance(answer, Mapping) else None
        if not isinstance(pid, str) or not pid:
            raise _EngineError(f"{base} 收了图但没给 prompt_id：{str(answer)[:200]}")
        return pid

    def _wait_for_images(self, base: str, pid: str) -> list[tuple[str, bytes]]:
        """轮询 ``/history/<id>`` 直到那条进了历史，然后把这一炉**每一张**都取回来。

        进历史就意味着跑完了（成功或失败都在 ``status`` 里）。中途崩的引擎不给历史，
        所以这里必须有一条 900 秒的截止 —— 音色试听那回没有截止，界面就永远转圈。

        为什么要取全部而不是第一张：一次采样出几张是这一版的事（:data:`COUNT_CHOICES`），
        "只取第一张"会让那一炉白算 —— 用户付了 83.6 秒却只拿到一张。
        """
        deadline = self._clock() + GENERATE_DEADLINE_SECONDS
        with self._lock:
            self._phase = "running"
            self._detail = "引擎在画"
        while self._clock() < deadline:
            if self._cancel.is_set():
                raise _EngineError("已停止生成")
            history = self._get_json(
                base + "/history/" + urllib.parse.quote(pid), POLL_TIMEOUT_SECONDS
            )
            entry = history.get(pid) if isinstance(history, Mapping) else None
            if isinstance(entry, Mapping):
                images = _history_images(entry)
                if not images:
                    raise _EngineError(f"引擎说这一张跑完了，却没出图：{_status_words(entry)}")
                out: list[tuple[str, bytes]] = []
                for image in images:
                    blob = self._fetch(
                        base
                        + "/view?"
                        + urllib.parse.urlencode(
                            {
                                "filename": str(image.get("filename", "")),
                                "subfolder": str(image.get("subfolder", "")),
                                "type": str(image.get("type", "output")),
                            }
                        )
                    )
                    out.append((str(image.get("filename", "image.png")), blob))
                return out
            if self._proc is not None and self._proc.poll() is not None:
                raise _EngineError(
                    f"本软件起的引擎退出了（退出码 {self._proc.poll()}）。"
                    + " | ".join(self._tail[-3:])
                )
            self._sleep(2.0)
        raise _EngineError(f"等了 {int(GENERATE_DEADLINE_SECONDS)} 秒，{base} 还没把这一张做完。")

    def _start_engine(self) -> bool:
        """按用户指的目录起一个 ComfyUI。条件不齐就回 False，并把原因留在 ``_detail``。"""
        root = Path(self._comfy_dir())
        python = _engine_python(root, self._environ)
        if python is None:
            with self._lock:
                self._phase = "failed"
                self._detail = (
                    f"{root} 旁边找不到 python.exe（试了 venv\\Scripts、python_embeded、.venv）。"
                    "ComfyUI 是用什么 python 跑的，就得把那个 python 留在它旁边。"
                )
            return False
        if not (root / "main.py").is_file():
            with self._lock:
                self._phase = "failed"
                self._detail = f"{root} 里没有 main.py，那不是一个 ComfyUI 的目录"
            return False
        port = self._engine_port()
        argv = [str(python), "main.py", "--port", str(port), "--listen", "127.0.0.1"]
        if (root / "custom_nodes" / "ComfyUI-AnimateDiff-Evolved").is_dir():
            argv += ["--lowvram", "--preview-method", "none"]
        with self._lock:
            self._phase = "starting"
            self._detail = f"在起引擎（{root}，端口 {port}）"
            self._tail = []
        logger.info("starting image engine: %s", " ".join(argv))
        try:
            proc = self._spawn(argv, root)
        except OSError as exc:
            with self._lock:
                self._phase = "failed"
                self._detail = f"起不起来：{type(exc).__name__}: {exc}"
            return False
        with self._lock:
            self._proc = proc
        self._reader = threading.Thread(
            target=self._pump, args=(proc,), daemon=True, name="image-engine-log"
        )
        self._reader.start()
        return True

    def _pump(self, proc: subprocess.Popen[bytes]) -> None:
        """把引擎的输出读进尾巴缓冲。不读的话它的管道会写满，然后它就卡在那儿不吭声。"""
        stream = proc.stdout
        if stream is None:  # pragma: no cover - 起的时候总要管道
            return
        for raw in iter(stream.readline, b""):
            line = raw.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            with self._lock:
                self._tail = [*self._tail, line][-40:]
        proc.wait()

    def _engine_port(self) -> int:
        url = self._comfy_url()
        match = re.search(r":(\d{2,5})\b", url) if url else None
        return int(match.group(1)) if match else COMFY_PORTS[0]

    # -- 云端 -------------------------------------------------------------

    def _generate_cloud(
        self,
        prompt: str,
        size: str,
        width: int,
        height: int,
        steps: int,
        api_size: str,
        started: float,
    ) -> ImageAnswer:
        key = self._cloud_key()
        if not key.isascii():
            # HTTP 头只能是 latin-1：钥匙里混进中文时 urllib 会在底层抛 UnicodeEncodeError，
            # 那句话落在面板上就是"软件崩了"。它其实是一句能照着做的提示。
            return self._fail(
                f"{IMAGE_KEY_ENV} 里有非 ASCII 字符（HTTP 头装不下）。"
                "多半是粘贴时带进了中文标点 —— 重新复制一次纯英文的钥匙。"
            )
        base = self._cloud_base().rstrip("/")
        body: dict[str, object] = {"prompt": prompt, "size": api_size, "n": 1}
        model = self._cloud_model()
        if model:
            body["model"] = model
        seed = random.randrange(1, 2**62)
        try:
            answer = self._post_json(
                base + "/images/generations", body, CLOUD_TIMEOUT_SECONDS, bearer=key
            )
        except _EngineError as exc:
            return self._fail(f"云端（{base}）：{exc}")
        blob, note = parse_cloud_answer(answer)
        if blob:
            path = self._save(f"xiaoye_cloud_{time.strftime('%Y%m%d-%H%M%S')}.png", blob)
            # 云端这条**只回一张**（`n: 1` 写死，见上面 body 那一行），
            # 所以这里给 `_ok` 的是只有一项的列表 —— 面板那格「一次画几张」对它不生效。
            return self._ok([path], "cloud", prompt, size, steps, seed, started, blob)
        if note.startswith("NEED_FETCH:"):
            try:
                blob = self._fetch(note[len("NEED_FETCH:") :], timeout=CLOUD_TIMEOUT_SECONDS)
            except _EngineError as exc:
                return self._fail(f"云端给了图片地址却取不下来：{exc}")
            path = self._save(f"xiaoye_cloud_{time.strftime('%Y%m%d-%H%M%S')}.png", blob)
            return self._ok([path], "cloud", prompt, size, steps, seed, started, blob)
        return self._fail(f"云端（{base}）：{note}")

    # -- 落盘与回话 --------------------------------------------------------

    def _save(self, filename: str, blob: bytes) -> Path:
        safe = Path(filename).name.replace("..", "_")
        directory = self._paths.images_dir
        try:
            # 目录可能被人在软件外面删了 —— 那时候该把图存进去，而不是抛一句"存不下"。
            directory.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise _EngineError(f"生图目录建不出来（{directory}）：{type(exc).__name__}") from exc
        path = directory / safe
        try:
            path.write_bytes(blob)
        except OSError as exc:
            logger.warning("生图落盘失败: %s", exc)
            raise _EngineError(f"图画出来了但存不下（{path}）：{type(exc).__name__}") from exc
        return path

    def _ok(
        self,
        paths: list[Path],
        engine: str,
        prompt: str,
        size: str,
        steps: int,
        seed: int,
        started: float,
        blob: bytes,
    ) -> ImageAnswer:
        """``paths`` 是这一次落盘的全部图，``blob`` 是第一张的字节（面板那张小预览用）。"""
        first = paths[0]
        seconds = round(self._clock() - started, 1)
        with self._lock:
            self._phase = "idle"
            self._detail = f"存到 {first.name}" if len(paths) == 1 else f"存了 {len(paths)} 张"
            self._last_path = str(first)
            self._last_seconds = seconds
            self._note = ""
        if not self._prefs.flag(IMAGE_KEEP_OUTPUT, default=True):
            # 勾了"不留文件"：预览还是要给的，落盘那几份删掉，别在机器上留一堆图。
            for path in paths:
                with contextlib.suppress(OSError):
                    path.unlink(missing_ok=True)
        return {
            "ok": True,
            "path": str(first),
            "paths": [str(path) for path in paths],
            "engine": engine,
            "prompt": prompt,
            "prompt_input": "",
            "prompt_note": "",
            "size": size,
            "steps": steps,
            "seed": seed,
            "seconds": seconds,
            "preview": preview_data_url(blob),
            "error": "",
        }

    def _fail(self, message: str) -> ImageAnswer:
        with self._lock:
            self._phase = "failed"
            self._detail = message
            self._note = message
        logger.info("生图失败: %s", message)
        return {
            "ok": False,
            "path": "",
            "paths": [],
            "engine": "",
            "prompt": "",
            "prompt_input": "",
            "prompt_note": "",
            "size": "",
            "steps": 0,
            "seed": 0,
            "seconds": 0.0,
            "preview": "",
            "error": message,
        }

    # -- 那一格读的配置 ---------------------------------------------------

    def _engine_choice(self) -> str:
        value = str(self._prefs.text(IMAGE_ENGINE) or Engine.AUTO.value)
        return value if value in {row.id for row in ENGINES} else Engine.AUTO.value

    def _engine_label(self, choice: str) -> str:
        return next((row.label for row in ENGINES if row.id == choice), choice)

    def _size(self) -> str:
        return self._prefs.text(IMAGE_SIZE) or DEFAULT_SIZE

    def _steps(self) -> int:
        steps = self._prefs.number(IMAGE_STEPS)
        return steps if MIN_STEPS <= steps <= MAX_STEPS else DEFAULT_STEPS

    def _count(self) -> int:
        pref = self._prefs.number(IMAGE_COUNT)
        return _snap_count(pref, DEFAULT_COUNT) if pref else DEFAULT_COUNT

    def _parse_count(self, value: object) -> int:
        """工具传来的张数。留空＝用偏好那一档，给了数就收到最近的档位上。"""
        if value is None or str(value).strip() == "":
            return self._count()
        return _snap_count(value, self._count())

    def _parse_steps(self, value: object) -> int:
        if value is None or str(value).strip() == "":
            return self._steps()
        try:
            steps = int(str(value).strip())
        except ValueError:
            return self._steps()
        return max(MIN_STEPS, min(MAX_STEPS, steps))

    def _comfy_url(self) -> str:
        return self._prefs.text(IMAGE_COMFY_URL).strip()

    def _comfy_dir(self) -> str:
        """用户指过的目录优先，没指就用随包那一份（没有随包就是空字符串）。"""
        return self._prefs.text(IMAGE_COMFY_DIR).strip() or _vendored_path()

    def _cloud_base(self) -> str:
        return self._prefs.text(IMAGE_CLOUD_BASE).strip() or DEFAULT_CLOUD_BASE

    def _cloud_model(self) -> str:
        return self._prefs.text(IMAGE_CLOUD_MODEL).strip()

    def _cloud_key(self) -> str:
        return str(self._environ.get(IMAGE_KEY_ENV, "")).strip()

    def _cloud_key_ready(self) -> bool:
        return bool(self._cloud_key())

    def _cloud_ready(self) -> bool:
        return bool(self._cloud_base()) and self._cloud_key_ready()

    def _can_start_local(self) -> bool:
        return bool(self._comfy_dir())

    # -- 网络小工具 -------------------------------------------------------

    def _get_json(self, url: str, timeout: float) -> object:
        return self._request(
            urllib.request.Request(url, headers={"User-Agent": _user_agent()}), timeout
        )

    def _post_json(
        self, url: str, payload: Mapping[str, object], timeout: float, *, bearer: str = ""
    ) -> object:
        headers = {"Content-Type": "application/json", "User-Agent": _user_agent()}
        if bearer:
            headers["Authorization"] = f"Bearer {bearer}"
        request = urllib.request.Request(
            url, data=json.dumps(dict(payload)).encode("utf-8"), headers=headers, method="POST"
        )
        return self._request(request, timeout)

    def _fetch(self, url: str, timeout: float = 30.0) -> bytes:
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme not in ("http", "https"):
            raise _EngineError(f"图片地址不是 http(s)：{url[:80]}")
        try:
            with self._opener(
                urllib.request.Request(url, headers={"User-Agent": _user_agent()}), timeout
            ) as reply:
                blob = bytes(reply.read(40 * 1024 * 1024))
        except urllib.error.HTTPError as exc:
            raise _EngineError(f"取 {url[:80]} 回了 HTTP {exc.code}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise _EngineError(_network_words(url, exc)) from exc
        if not blob:
            raise _EngineError(f"{url[:80]} 回了 0 字节")
        return blob

    def _request(self, request: urllib.request.Request, timeout: float) -> object:
        url = request.full_url
        try:
            with self._opener(request, timeout) as reply:
                raw = bytes(reply.read(20 * 1024 * 1024))
        except urllib.error.HTTPError as exc:
            raw = exc.read(4000) if hasattr(exc, "read") else b""
            body = _body_words(raw) or raw.decode("utf-8", errors="replace")[:400]
            raise _EngineError(_http_words(url, exc.code, body)) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise _EngineError(_network_words(url, exc)) from exc
        try:
            return json.loads(raw.decode("utf-8", errors="replace"))
        except json.JSONDecodeError as exc:
            raise _EngineError(f"{url} 回的不是 JSON：{raw[:160]!r}") from exc


class _EngineError(RuntimeError):
    """引擎那一侧说的一句人话。往外抛之前已经拼好，所以调用方直接回显。"""


def _vendored_path() -> str:
    """随包引擎的路径串；没带就是空串。每问一次都重新找：它只取决于盘上有什么。"""
    found = vendored_engine_dir()
    return str(found) if found is not None else ""


def _body_words(raw: bytes) -> str:
    """错误返回体里 ``{"error": {"type": …, "message": …}}`` 那一句。

    ComfyUI 拒绝一张图时说的是人话（比如权重里没有这个节点），扔掉它就只剩
    "HTTP 400" —— 用户拿着那四个字来问，谁也帮不了他。
    """
    try:
        payload = json.loads(raw.decode("utf-8", errors="replace"))
    except json.JSONDecodeError:
        return ""
    if not isinstance(payload, Mapping):
        return ""
    error = payload.get("error")
    if isinstance(error, Mapping):
        node_errors = error.get("node_errors")
        bits = [str(error.get(key) or "") for key in ("type", "message", "detail")]
        if isinstance(node_errors, Mapping) and node_errors:
            first = next(iter(node_errors.values()))
            if isinstance(first, Mapping):
                errors = first.get("errors")
                if isinstance(errors, list) and errors and isinstance(errors[0], Mapping):
                    bits.append(str(errors[0].get("message") or errors[0].get("detail") or ""))
        return "：".join(bit for bit in bits if bit)[:400]
    if isinstance(error, str):
        return error[:400]
    return ""


def _status_words(entry: Mapping[str, object]) -> str:
    """``/history`` 那条记录里引擎自己说的最后一句话。"""
    status = entry.get("status")
    if not isinstance(status, Mapping):
        return "它没给状态"
    messages = status.get("messages")
    if isinstance(messages, list) and messages:
        last = messages[-1]
        if isinstance(last, (list, tuple)) and len(last) >= 2 and isinstance(last[1], Mapping):
            detail = last[1]
            text = str(detail.get("exception_message") or detail.get("error") or "")
            if text:
                return text[:400]
        elif isinstance(last, (list, tuple)) and last:
            return str(last[0])[:120]
    return str(status.get("status_str") or "没状态")[:120]


def _history_images(entry: Mapping[str, object]) -> list[Mapping[str, object]]:
    """``/history`` 那条记录里所有 ``SaveImage`` 出来的图。

    一层层 ``isinstance`` 而不是直接 ``.get(...).values()``：ComfyUI 崩在半路时这里给的是
    ``{"outputs": {}}`` 或整个节点缺掉，写成下标的形式只会让服务在解析回包时抛
    ``AttributeError``，而那句话在面板上长得和"软件坏了"一模一样。
    """
    outputs = entry.get("outputs")
    if not isinstance(outputs, Mapping):
        return []
    images: list[Mapping[str, object]] = []
    for node in outputs.values():
        if not isinstance(node, Mapping):
            continue
        got = node.get("images")
        if isinstance(got, list):
            images.extend(item for item in got if isinstance(item, Mapping))
    return images


def _checkpoint_names(info: object) -> list[str]:
    """从 ``/object_info/CheckpointLoaderSimple`` 里抠出权重文件名。"""
    if not isinstance(info, Mapping):
        return []
    node = info.get("CheckpointLoaderSimple")
    if not isinstance(node, Mapping):
        return []
    inputs = node.get("input")
    required = inputs.get("required") if isinstance(inputs, Mapping) else None
    field = required.get("ckpt_name") if isinstance(required, Mapping) else None
    if isinstance(field, list) and field and isinstance(field[0], list):
        return [str(item) for item in field[0] if isinstance(item, str)]
    if isinstance(field, list):
        return [str(item) for item in field if isinstance(item, str)]
    return []


ENGINE_VENDOR_DIRNAME: Final = "comfyui"
"""随包引擎在 ``vendor`` 下的目录名。和 ``scrcpy`` / ``llama`` 同一套约定。"""


def vendored_engine_dir() -> Path | None:
    """随包的本地引擎目录，或者 ``None``（= 这个包只支持"连你自己开着的引擎"）。

    和 :func:`jarvis.app.screen_mirror.runtime_dir` 同一条查找顺序：``JARVIS_COMFYUI_DIR``
    覆盖（开发时用），然后是包内的 ``vendor/comfyui``。判据是 **文件在不在**，不看打包日志 ——
    spec 少一条 datas 时的表现代码是"退出码 0、产物里没那个目录、界面一按就说没带"。
    """
    root = Path(__file__).resolve().parent.parent
    override = os.environ.get("JARVIS_COMFYUI_DIR", "")
    candidates = [Path(part) for part in override.split(os.pathsep) if part]
    candidates += [
        Path(sys.executable).resolve().parent / "本地引擎" / "ComfyUI",
        root / "vendor" / ENGINE_VENDOR_DIRNAME,
        root / "_vendor" / ENGINE_VENDOR_DIRNAME,
    ]
    for candidate in candidates:
        if (candidate / "main.py").is_file() and _engine_python(candidate, os.environ) is not None:
            return candidate
    return None


def _engine_python(root: Path, environ: Mapping[str, str]) -> Path | None:
    """ComfyUI 目录旁边那个 python。找不到就回 None，由界面说"指个目录"。"""
    for candidate in (
        root.parent / "venv" / "Scripts" / "python.exe",
        root / "venv" / "Scripts" / "python.exe",
        root / "python_embeded" / "python.exe",
        root / ".venv" / "Scripts" / "python.exe",
    ):
        if candidate.is_file():
            return candidate
    env = environ.get("JARVIS_COMFY_PYTHON", "")
    return Path(env) if env and Path(env).is_file() else None


def _default_spawn(argv: list[str], cwd: Path) -> subprocess.Popen[bytes]:
    cache = cwd.parent / "cache"
    temporary = cwd.parent / "tmp"
    temporary.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    portable_python = cwd.parent / "python" / "python.exe"
    portable_packages = cwd.parent / "venv" / "Lib" / "site-packages"
    if portable_python.is_file() and portable_packages.is_dir():
        argv = [str(portable_python), *argv[1:]]
        env["PYTHONPATH"] = str(portable_packages)
        env["PYTHONNOUSERSITE"] = "1"
        env["PYTHONDONTWRITEBYTECODE"] = "1"
    env.update(
        TEMP=str(temporary),
        TMP=str(temporary),
        HF_HOME=str(cache / "huggingface"),
        TORCH_HOME=str(cache / "torch"),
        PIP_CACHE_DIR=str(cache / "pip"),
        CUDA_CACHE_PATH=str(cache / "cuda"),
        TRITON_CACHE_DIR=str(cache / "triton"),
        XDG_CACHE_HOME=str(cache),
        NUMBA_CACHE_DIR=str(cache / "numba"),
        MPLCONFIGDIR=str(cache / "matplotlib"),
    )
    return subprocess.Popen(
        argv,
        cwd=str(cwd),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        shell=False,
        env=env,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def as_opener(func: Callable[..., Any]) -> Opener:
    """把 ``urllib.request.urlopen`` 收成 :class:`Opener` 那个形状（mypy 需要一个明确的边界）。"""

    def call(request: urllib.request.Request, timeout: float) -> Any:
        return func(request, timeout=timeout)

    return call


def preview_data_url(blob: bytes) -> str:
    """把图缩成一条 data URL 给界面。缩不了就回空串 —— 预览是锦上添花，不是功能。"""
    try:
        from PIL import Image

        with Image.open(io.BytesIO(blob)) as opened:
            image = opened.convert("RGB")
        edge = max(image.size)
        if edge > MAX_PREVIEW_EDGE:
            scale = MAX_PREVIEW_EDGE / edge
            image = image.resize((int(image.width * scale), int(image.height * scale)))
        buffer = io.BytesIO()
        image.save(buffer, format="JPEG", quality=82)
        return "data:image/jpeg;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")
    except Exception as exc:
        logger.info("生图预览缩略失败：%s", type(exc).__name__)
        return ""


def _network_words(url: str, exc: BaseException) -> str:
    """连不上的那句话要点名地址 —— 投屏那边「连不上 127.0.0.1:5555」同样的写法。"""
    host = urllib.parse.urlsplit(url).netloc or url
    if isinstance(exc, TimeoutError):
        return f"连 {host} 没等到回音（超时）"
    return f"连不上 {host}：{getattr(exc, 'reason', exc)}"


def _http_words(url: str, code: int, body: str) -> str:
    host = urllib.parse.urlsplit(url).netloc or url
    detail = re.sub(r"\s+", " ", body).strip()[:200]
    if code == 400 and detail:
        return f"{host} 拒绝了这次请求（HTTP 400）：{detail}"
    if code in (401, 403):
        return f"{host} 说钥匙不对或不够（HTTP {code}）。检查 {IMAGE_KEY_ENV} 和那个模型名"
    if code == 404:
        return (
            f"{host} 上没有这个端点（HTTP 404）—— 那一格填的是服务的基础地址，"
            "不要连着 /images/generations 一起粘进来"
        )
    return f"{host} 回了 HTTP {code}：{detail or '它没多说什么'}"


def _size_note(size: str) -> str:
    notes = {
        "512x512": "最快的一档。作者这台 GTX 1650 实测 22 秒一张。",
        "768x768": "比 512 慢约两倍多，细节多一截。",
        "1024x1024": "4 GB 显存上是勉强的：慢，且和别的引擎抢显存时容易崩。",
    }
    return notes.get(size, "")


def _snap_count(value: object, fallback: int) -> int:
    """把任意输入收到档位上。转不成整数就回退，能转就取**最近**的一档。

    为什么不夹到区间里：档位是 1/2/4，夹的话 3 会被抬成 4 —— 而 4 是八十多秒。
    并列时取小的那一档（3 → 2），便宜优先：多花一倍的等待换"多一张挑"这件事，
    得是用户自己点出来的，不是软件替他猜出来的。
    """
    try:
        count = int(str(value).strip())
    except (TypeError, ValueError):
        return fallback
    return min(COUNT_CHOICES, key=lambda row: (abs(row - count), row))


def _count_note(count: int) -> str:
    """这一档的说明。量过的写「实测」，没单独量的按两头推、并在字面上写明是推的。"""
    notes = {
        1: "最快：512x512 二十步实测 22.4 秒。代价是——模型换个种子就会翻面，"
        "一次只给一张，等于把那张错的直接交出去。",
        2: "这一档没有单独量过：两头是 1 张 22.4 秒、4 张一炉 83.6 秒，"
        "2 张按这两头推在 40~45 秒之间。比 1 张稳，比 4 张快。",
        4: "实测 83.6 秒（每张 20.9 秒，一炉出的比跑四遍省），4 GB 卡上装得下。"
        "一次给四张让你挑 —— 这是把「换种子就翻面」变成「挑一张」的办法。",
    }
    return notes.get(count, "")


def _as_mb(value: object) -> float:
    """``/system_stats`` 的显存字段按字节给；CPU 引擎那格根本没有。"""
    if isinstance(value, (int, float)) and value > 0:
        return round(float(value) / (1024 * 1024), 1)
    return 0.0


def _failed_probe(url: str, detail: str) -> ImageProbe:
    return {
        "kind": "comfyui",
        "url": url,
        "ok": False,
        "detail": detail,
        "version": "",
        "checkpoints": [],
        "probed_at": 0.0,
        "vram_total_mb": 0.0,
        "vram_free_mb": 0.0,
    }


def _user_agent() -> str:
    from jarvis.core.constants import USER_AGENT

    return USER_AGENT


__all__ = [
    "COUNT_CHOICES",
    "ENGINES",
    "IMAGE_KEY_ENV",
    "SIZE_CHOICES",
    "Engine",
    "ImageAnswer",
    "ImageProbe",
    "ImageService",
    "ImageStatus",
    "build_workflow",
    "parse_cloud_answer",
    "parse_size",
    "preview_data_url",
]
