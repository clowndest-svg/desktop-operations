"""The local model that ships with the program.

Started when she needs it, stopped when she has been idle.

Why a subprocess
----------------
llama.cpp is a C++ server, not a Python library. Loading the weights inside this process
would mean either shipping a torch/onnx dependency tree for inference we do not do here,
or embedding a loader that dies with the whole app on a native fault. A child process
also gives the one thing an in-process model cannot: it can be **thrown away** when the
operator walks away from the local model, and the 5 GB of RAM it holds goes back to the
machine without restarting 小夜.

The shape of this module is deliberately the same as :mod:`jarvis.tts.sidecar` (the
CosyVoice worker): plan-before-start, drain the child's pipes, graceful stop then kill,
``atexit``, and a phase the UI can poll. Two heavy subprocesses with two different
lifecycles would be the kind of thing somebody debugs for an evening each time.

What is *not* here
------------------
No retry storm and no "keep it up forever". If the model cannot start, the answer is a
reason in Chinese and the cloud provider stays selected; a pet that silently falls back
to a different brain is worse than one that says it could not load.
"""

from __future__ import annotations

import atexit
import io
import logging
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from _thread import RLock as ResourceLock
from collections import deque
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Final

import psutil

from jarvis.config.paths import default_data_dir

logger = logging.getLogger(__name__)

SERVER_EXE: Final = "llama-server.exe"
MODEL_DIRNAME: Final = "模型"
RUNTIME_DIRNAME: Final = "llama"
MODEL_ALIAS: Final = "qwen2.5-7b"
#: The key under ``llm.providers`` this whole module exists to serve.
LOCAL_PROVIDER: Final = "local"
#: Not 28080 -- that is the port the standalone ``qwen2.5-7B\\start.ps1`` deployment
#: uses, and the two must be able to run side by side without either "failing to load".
DEFAULT_PORT: Final = 28181
LOAD_TIMEOUT: Final = 300.0
HEALTH_PATH: Final = "/health"

#: Context windows the operator may pick. Measured on this machine (i7-10750H, 16 GB,
#: GTX 1650 4 GB) with the app's own args -- resident ``llama-server`` is 5034 / 5061 /
#: 5182 / 5226 MB at 2048 / 4096 / 8192 / 16384. ``--fit`` trades model layers in VRAM for
#: the KV cache, so on *this* box a bigger window is nearly free. The gate below does not
#: assume that: it uses the CPU-only measurement, where the same climb costs +837 MB.
CONTEXT_TIERS: Final[tuple[int, ...]] = (2048, 4096, 8192, 16384)
DEFAULT_CONTEXT: Final = 4096
#: Minutes of silence before the model is thrown away. ``0`` means keep it up.
IDLE_TIERS: Final[tuple[int, ...]] = (0, 2, 5, 10, 30)
DEFAULT_IDLE_MINUTES: Final = 10
#: Resident needed for the default window: this machine's load measured 5061 MB, the gate
#: rounds up to 5.2 GB. Smaller windows are allowed to want less, bigger ones must want more.
NEEDED_FREE_MB: Final = 5200
#: KV growth per extra 1k of window: CPU-only 2048->16384 measured +837 MB (~58 MB/1k),
#: rounded up. The CPU-only figure is what a buyer without a discrete GPU pays.
KV_MB_PER_1K: Final = 64
#: Never ask for less than this: CPU-only load at 2048 measured 4639 MB resident.
FREE_FLOOR_MB: Final = 4700
#: The prompt the desktop assistant actually sends with its tool table: 4639 tokens of
#: tools, 4702 with the turn around it (measured r63). A window under this cannot host
#: the tools, so she answers instead of acting -- which is a fact to state, not a bug.
TOOL_TABLE_TOKENS: Final = 4702

#: The largest *capability pack* the chat service sends, plus the same turn
#: overhead the figure above includes. Measured r88 with the shipped tokenizer: the
#: whole 35-tool table is 3945 tokens, and the biggest single pack (memory: 6 tools)
#: is 849. Routing the table down to what a sentence could need is what makes a
#: 4096-token local model able to act at all, so "can she use tools" is judged
#: against this number from r88 on, not against the full table.
PACK_TABLE_TOKENS: Final = 1606


#: Resident ``llama-server`` per tier, measured on this machine: first number with the
#: app's own Vulkan ``--fit`` args, second forced CPU-only (``-ngl 0``). The settings row
#: quotes these so the operator is choosing against a number, not a vibe.
MEASURED_RESIDENT_MB: Final[dict[int, tuple[int, int]]] = {
    2048: (5034, 4639),
    4096: (5061, 4758),
    8192: (5182, 4998),
    16384: (5226, 5476),
}


@dataclass(frozen=True)
class ModelCard:
    """One weights file: what it costs on this machine, and what it was seen to do.

    Every number here is measured by really starting ``llama-server`` with
    :func:`build_command`'s own arguments (``.r77/measure_models2.py``) -- not a reference
    implementation written to match a guess. ``tiers`` holds the peak resident size at each
    context window, as a pair (this machine with its Vulkan GPU, and forced CPU-only with
    ``-ngl 0``), because a buyer without a GPU cannot make the trade ``--fit`` makes.
    """

    file: str
    label: str
    alias: str
    license: str
    #: 4096 那一档"要约这么多空闲内存才肯加载"（实测往上取整）。
    gate_base_mb: int
    #: 每多 1K 上下文多占多少；只用来外推没实测到的档。
    kv_mb_per_1k: int
    #: 上下文 -> (本机 Vulkan 常驻, 纯 CPU 常驻)，MB。
    tiers: dict[int, tuple[int, int]]
    #: 带那 34 个工具的真清单，实测见过它调成一次工具。"窗口装得下 4702 token"是算术，
    #: 这一格是目击 —— 两者差一轮就是 r63 到 r76 一直挂着的那条"没验到"。
    acts_verified: bool
    #: 默认开思考的模型（Qwen3 全系）。不显式关掉，回答会看着是空的：实测 0.6B 在
    #: 40 个 token 的预算里把话全写进思考块，``content`` 是空串、``tool_calls`` 也是空。
    thinking: bool = False
    measured: bool = True
    #: 这一档只够验证"能不能连通"，不能当对话模型卖。判据是 2026-10-18 实拍：0.6B 在
    #: ``temperature: 0.0``（贪心）下把「现在你是我女朋友」「现在玩角色扮演游戏，你是我老婆」
    #: 两条新问题答成上一轮那句原话 —— 三条回答 md5 完全相同（``a4e4e974``）。
    #: 接线没错（库里三条提问各自在档、``tokens_in`` 逐轮 +30/+34 证明历史在累积），
    #: 是它把上文当成示例照抄。界面上这一格必须说得出"别拿它聊天"。
    smoke_only: bool = False

    def needed(self, context: int) -> int:
        """How much free memory this window may load with."""
        steps = (context - DEFAULT_CONTEXT) // 1024
        return max(256, self.gate_base_mb + int(steps) * self.kv_mb_per_1k)

    def resident(self, context: int) -> tuple[int, int]:
        """Peak resident for this window: measured when we have it, walked when we do not."""
        got = self.tiers.get(context)
        if got is not None:
            return got
        base = self.tiers.get(DEFAULT_CONTEXT, (self.gate_base_mb, self.gate_base_mb))
        steps = (context - DEFAULT_CONTEXT) // 1024
        return (
            max(256, base[0] + int(steps) * self.kv_mb_per_1k),
            max(256, base[1] + int(steps) * self.kv_mb_per_1k),
        )


#: 表里没有的文件（买家自己扔进 ``模型`` 目录的那种）走 :func:`estimate_card`，
#: 界面上标"未实测"，门槛按文件大小估。
MODEL_CARDS: Final[dict[str, ModelCard]] = {
    "Qwen2.5-7B-Instruct.Q4_K_M.gguf": ModelCard(
        "Qwen2.5-7B-Instruct.Q4_K_M.gguf",
        "Qwen2.5-7B",
        "qwen2.5-7b",
        "Apache-2.0",
        5200,
        64,
        # r73 那一批的实测；r77 重测同一台机器是 4828/4433（4096 档）—— 差 200~350 MB，
        # 出字速度两次差一倍（7.8 与 3.0 tok/s），取决于当时显卡忙不忙。这里保留高的那个内存数。
        {2048: (5034, 4639), 4096: (5061, 4758), 8192: (5182, 4998), 16384: (5226, 5476)},
        True,
    ),
    "Qwen3-4B.Q4_K_M.gguf": ModelCard(
        "Qwen3-4B.Q4_K_M.gguf",
        "Qwen3-4B",
        "qwen3-4b",
        "Apache-2.0",
        3200,
        80,
        {2048: (2826, 2672), 4096: (2828, 2827), 8192: (2961, 3137), 16384: (3242, 3758)},
        True,
        True,
    ),
    "Qwen3-1.7B-Q8_0.gguf": ModelCard(
        "Qwen3-1.7B-Q8_0.gguf",
        "Qwen3-1.7B",
        "qwen3-1.7b",
        "Apache-2.0",
        2500,
        60,
        {2048: (2199, 2000), 4096: (2201, 2121), 8192: (2205, 2364), 16384: (2214, 2848)},
        True,
        True,
    ),
    "Qwen3-0.6B-Q8_0.gguf": ModelCard(
        "Qwen3-0.6B-Q8_0.gguf",
        "Qwen3-0.6B",
        "qwen3-0.6b",
        "Apache-2.0",
        1200,
        60,
        {2048: (894, 853), 4096: (895, 974), 8192: (900, 1215), 16384: (908, 1700)},
        True,
        True,
        smoke_only=True,
    ),
}

#: ``llm.bundled_model`` 的两个特殊值：空 = 自动（包里最大的那个），``none`` = 不用离线模型。
MODEL_AUTO: Final = ""
MODEL_NONE: Final = "none"
#: 一个权重都没有时的兜底：没人看这一格的数字，只保证读它不炸。
NO_MODEL_CARD: Final = ModelCard(
    "", "没有权重文件", "none", "?", NEEDED_FREE_MB, KV_MB_PER_1K, {}, False
)


def estimate_card(path: Path) -> ModelCard:
    """A file we never measured: price it by size and say out loud that it is an estimate."""
    try:
        size_mb = max(1, path.stat().st_size // 2**20)
    except OSError:
        size_mb = 1
    gate = int(size_mb * 1.15) + 512
    return ModelCard(
        path.name,
        path.name.replace(".gguf", ""),
        path.name.split(".")[0].lower(),
        "未知（自己核对再商用）",
        gate,
        max(8, int(size_mb * 0.012)),
        {},
        False,
        measured=False,
    )


def card_for(path: Path | None) -> ModelCard:
    """The card for a weights file: measured when we know it, estimated when we do not."""
    if path is None:
        return NO_MODEL_CARD
    return MODEL_CARDS.get(path.name) or estimate_card(path)


def selected_card() -> ModelCard:
    """The card for whatever the dial currently points at (auto included)."""
    return card_for(model_file())


def gate_mb(card: ModelCard, context: int) -> int:
    """How much must be free to dare load *this* model at *this* window."""
    return card.needed(context)


def bundled_catalog() -> list[dict[str, object]]:
    """Every weights file on disk, plus the ones we ship but the buyer deleted.

    The picker lists this so a choice is made against numbers -- size, what each window
    costs here, whether this model was *seen* calling a tool -- instead of a model name on
    a poster. Reading it starts nothing.
    """
    wanted = tuning().model
    options = installed_models()
    # "自动"实际选中的就是最大的那个，界面要照着这个真相打勾，不能空着说"自动"
    auto = options[0].name if options else ""
    present = {path.name: path for path in options}
    rows = [
        _catalog_row(card, present.pop(card.file, None), wanted, auto)
        for card in MODEL_CARDS.values()
    ]
    for path in present.values():  # 买家自己扔进来的那种
        rows.append(_catalog_row(estimate_card(path), path, wanted, auto))
    rows.sort(key=lambda row: -int(str(row["size_mb"]) or 0))
    return rows


def _size_mb(path: Path | None) -> int:
    """0 when the file vanished between listing it and asking -- the row must still render."""
    if path is None:
        return 0
    try:
        return round(path.stat().st_size / 2**20)
    except OSError:
        return 0


def _catalog_row(card: ModelCard, path: Path | None, wanted: str, auto: str) -> dict[str, object]:
    gpu, cpu = card.resident(DEFAULT_CONTEXT)
    return {
        "file": card.file,
        "label": card.label,
        "size_mb": _size_mb(path),
        "present": path is not None,
        "selected": wanted == card.file or (wanted == MODEL_AUTO and card.file == auto),
        "license": card.license,
        "measured": card.measured,
        "thinking": card.thinking,
        "acts_verified": card.acts_verified,
        "smoke_only": card.smoke_only,
        "gate_mb": card.needed(DEFAULT_CONTEXT),
        "resident_mb": gpu,
        "cpu_mb": cpu,
        # 每档的门槛和常驻都跟着这个模型走，界面切档时不再引用 7B 的那一组
        "tiers": [
            {
                "context": tier,
                "needs_free_mb": card.needed(tier),
                "resident_mb": card.resident(tier)[0],
            }
            for tier in CONTEXT_TIERS
        ],
    }


@dataclass(frozen=True)
class Tuning:
    """What the operator dialed for the bundled model. Pushed in by the app layer.

    :mod:`jarvis.llm` must not read ``jarvis.app.preferences`` (the layer guard says so),
    so the values arrive here through :func:`configure` instead of being pulled.
    """

    context: int = DEFAULT_CONTEXT
    idle_seconds: float = DEFAULT_IDLE_MINUTES * 60.0
    eager: bool = False
    #: 选中的权重文件名；``""`` = 自动（包里最大的那个），``"none"`` = 明确不用离线模型。
    model: str = MODEL_AUTO

    @property
    def needed_free_mb(self) -> int:
        """How much must be free before we dare load this window, for the chosen model."""
        return gate_mb(card_for(model_file()), self.context)


_TUNING = Tuning()
_RESOURCE_LOCK = ResourceLock()


def resource_lock() -> ResourceLock:
    return _RESOURCE_LOCK


@contextmanager
def media_session() -> Iterator[None]:
    """Release only our idle model and serialize local inference with media work."""
    with _RESOURCE_LOCK:
        server = shared_server()
        with server._lock:
            idle = server._busy == 0
        if idle:
            server.stop(reason="为本地图片/视频释放自有聊天模型资源")
        yield


def configure(next_tuning: Tuning) -> None:
    """Set the dial the next :func:`plan` and the next spawn will honour."""
    global _TUNING
    if next_tuning != _TUNING:
        logger.info(
            "自带模型的口径变了：上下文 %s token、空闲 %s 秒自动关、内存不足%s",
            next_tuning.context,
            int(next_tuning.idle_seconds),
            "也照样加载" if next_tuning.eager else "就不加载",
        )
        _TUNING = next_tuning


def tuning() -> Tuning:
    return _TUNING


def endpoint() -> str:
    """The URL to hand the client.

    While our own child is up that is the port it bound. Before it is up we point at
    :data:`DEFAULT_PORT` anyway, so an operator who already runs llama.cpp there gets
    the bundled provider working without this code starting a second copy.
    """
    return shared_server().endpoint() or f"http://127.0.0.1:{DEFAULT_PORT}/v1"


class ModelPhase(StrEnum):
    """What the bundled model is doing, in the words the settings row shows."""

    OFF = "off"
    LOADING = "loading"
    RUNNING = "running"
    FAILED = "failed"


def _package_root() -> Path:
    return Path(__file__).resolve().parent.parent


def runtime_dir() -> Path | None:
    """Where ``llama-server.exe`` lives, or ``None`` when the runtime was not bundled.

    ``vendor/llama`` inside the package is the same convention the CosyVoice sidecar
    uses, so a frozen build finds it under ``_internal/jarvis/vendor/llama``.
    """
    root = _package_root()
    value = os.environ.get("JARVIS_LLAMA_DIR", "")
    candidates = [Path(part) for part in value.split(os.pathsep) if part]
    candidates += [root / "vendor" / RUNTIME_DIRNAME, root / "_vendor" / RUNTIME_DIRNAME]
    for candidate in candidates:
        if (candidate / SERVER_EXE).is_file():
            return candidate
    return None


def _install_dir() -> Path:
    """The folder the program was started from: the exe's own, or the repo root from source."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return _package_root().parent


def model_file() -> Path | None:
    """The weights file the dial asks for, out of the places a buyer may have put one.

    Empty dial means "auto", which is still the biggest file -- a directory holding both
    a 7B and a 1.5B is somebody's deliberate choice, and the bigger one is what the
    install instructions had them drop in. ``none`` means "do not load one at all".

    The install directory is searched on its own, not only through the data root: an
    upgrade that still has ``%LOCALAPPDATA%\\Jarvis`` from before the data-root hook keeps
    its data there, and looking only beside the data root made the weights we shipped
    invisible -- the settings row said 没找到权重 next to a 4.36 GB file.
    """
    return selected_model(tuning().model)


def _model_roots() -> list[Path]:
    """Where weights may live: env override, beside the exe, beside the data root."""
    roots: list[Path] = []
    for variable in ("JARVIS_MODEL_DIR",):
        value = os.environ.get(variable, "")
        if value:
            roots.extend(Path(part) for part in value.split(os.pathsep) if part)
    roots.extend(
        [
            # <安装目录>/模型 —— 打包版就是这个位置，数据根挪到哪都不该影响它
            _install_dir() / MODEL_DIRNAME,
            # 数据根旁边：把「数据」整个搬走的人，权重跟着搬
            default_data_dir().parent / MODEL_DIRNAME,
        ]
    )
    return roots


def installed_models() -> list[Path]:
    """Every weights file we can see, biggest first -- the picker lists these.

    Reading this must never start anything: the settings panel calls it on every open.
    """
    found: dict[str, Path] = {}
    for root in _model_roots():
        try:
            for path in root.glob("*.gguf"):
                if path.is_file():
                    found.setdefault(path.name, path)
        except OSError:
            continue
    return sorted(found.values(), key=lambda path: path.stat().st_size, reverse=True)


def selected_model(wanted: str) -> Path | None:
    """The weights file the dial asks for. ``none`` means "do not load one at all"."""
    if wanted == MODEL_NONE:
        return None
    options = installed_models()
    if not options:
        return None
    if wanted:
        return next((path for path in options if path.name == wanted), None)
    from jarvis import is_buyer

    if is_buyer():
        current = globals().get("_SERVER")
        if isinstance(current, LocalServer) and current.phase in (
            ModelPhase.RUNNING,
            ModelPhase.LOADING,
        ):
            loaded = next((path for path in options if path.name == current._model), None)
            if loaded is not None:
                return loaded
        available = free_memory_mb()
        for path in options:
            if gate_mb(card_for(path), DEFAULT_CONTEXT) + 1024 <= available:
                return path
        return options[-1]
    return options[0]


@dataclass(frozen=True)
class Plan:
    """Whether the bundled model *can* run here, and the sentence that says why not."""

    can_run: bool
    reason: str
    exe: Path | None = None
    model: Path | None = None
    free_mb: int = 0
    context: int = DEFAULT_CONTEXT
    needed_mb: int = NEEDED_FREE_MB
    #: False means the window cannot hold even the smallest capability pack, so she will
    #: only answer. Measured against ``PACK_TABLE_TOKENS`` since r88: the full table is
    #: 3945 tokens and does not fit at 4096, but a routed pack does.
    acts: bool = False
    #: True = 这条连「内存看着不够也照样加载」都越不过：文件比总内存还大，不是余量问题。
    hard: bool = False


def free_memory_mb() -> int:
    return int(psutil.virtual_memory().available / (1024 * 1024))


def total_memory_mb() -> int:
    return int(psutil.virtual_memory().total / (1024 * 1024))


#: 显卡类注册表键：每个显示适配器一个 ``0000``/``0001`` 子键。
GPU_CLASS_KEY: Final = (
    r"SYSTEM\CurrentControlSet\Control\Class\{4d36e968-e325-11ce-bfc1-08002be10318}"
)
_HARDWARE: tuple[str, str, int] | None = None


def registry_hardware() -> tuple[str, str, int]:
    """(CPU 名, 显卡名, 显存 MB) —— 读注册表，一个进程都不起，读一次缓存住。

    为什么不起 PowerShell 问 CIM：这台机器上实测 ``Get-CimInstance`` 要 12 秒以上，第一次
    按「给一套建议」就是这么把显卡问成"不知道"的，而门槛和规则引擎拿不到显存就会往下按
    最保守的一档算，屏幕上还看不出错了。读不到（非 Windows、键被改）就返回空，让调用方决定
    要不要退回别的办法。

    放在 llm 层是因为门槛和「一键建议」要问同一件事，而 ``jarvis.llm`` 不许 import
    ``jarvis.app``（层级守卫）—— 反过来 app 可以读这里。
    """
    global _HARDWARE
    if _HARDWARE is not None:
        return _HARDWARE
    _HARDWARE = _read_registry_hardware()
    return _HARDWARE


def _read_registry_hardware() -> tuple[str, str, int]:
    try:
        import winreg
    except ImportError:  # 非 Windows：让调用方走它自己的兜底
        return "", "", 0
    cpu = ""
    try:
        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DESCRIPTION\System\CentralProcessor\0"
        ) as key:
            cpu = str(winreg.QueryValueEx(key, "ProcessorNameString")[0]).strip()
    except OSError:
        cpu = ""
    name, size = "", 0
    try:
        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE, GPU_CLASS_KEY, 0, winreg.KEY_READ | winreg.KEY_WOW64_64KEY
        ) as root:
            for index in range(winreg.QueryInfoKey(root)[0]):
                slot = winreg.EnumKey(root, index)
                if not slot.isdigit():
                    continue
                try:
                    with winreg.OpenKey(root, slot) as card:
                        desc = str(winreg.QueryValueEx(card, "DriverDesc")[0]).strip()
                        try:
                            # 有的驱动把它写成 REG_BINARY（8 字节小端），两种都要认。
                            raw, kind = winreg.QueryValueEx(
                                card, "HardwareInformation.qwMemorySize"
                            )
                            memory = (
                                int.from_bytes(raw, "little")
                                if kind == winreg.REG_BINARY
                                else int(raw)
                            )
                        except OSError:
                            memory = 0
                except OSError:
                    continue
                # 只有名字没有显存也要报出来：看的是"这台有没有独立显卡"这件事。
                if desc and (memory > size or (memory == size and not name)):
                    name, size = desc, memory
    except OSError:
        pass
    return cpu, name, size // 2**20


def vram_mb() -> int:
    """最大那块显存的 MB；0 = 读不到（非 Windows、键被改、纯 CPU 机器）。"""
    return registry_hardware()[2]


def disk_free_mb(where: Path | None = None) -> int:
    """权重所在那个盘的剩余空间：45 GB 的文件要连"放得下"一起说。"""
    import shutil

    target = where if where is not None else _install_dir()
    try:
        return int(shutil.disk_usage(str(target)).free / 2**20)
    except OSError:
        return 0


def verdict_for(wanted: str, context: int, eager: bool) -> Plan:
    """ "要是用这个模型、开这一档，这台机器接不接得住" —— 纯查，一个进程都不起。

    `plan()` 和界面上的"选中那一刻先查一次"必须走同一段代码。之前判定长在 `plan()` 里，
    下拉框要提前拦就得自己再算一遍，两处一不同步就会出现"屏上说能跑、真起来卡死"。
    """
    exe_dir = runtime_dir()
    model = selected_model(wanted)
    free = free_memory_mb()
    server = exe_dir / SERVER_EXE if exe_dir else None
    card = card_for(model)
    need = gate_mb(card, context)
    shape = (context, need, context >= PACK_TABLE_TOKENS)
    if exe_dir is None:
        return Plan(
            False, "程序里没带 llama.cpp 运行时（vendor/llama 缺失）", None, model, free, *shape
        )
    if wanted == MODEL_NONE:
        # An explicit choice, not a failure: she runs on the cloud provider instead.
        return Plan(False, "设置里选了「不用离线模型」", server, None, free, *shape)
    if model is None:
        where = (
            f"选的那个模型的文件不在包里（{wanted}），把 .gguf 放进「{MODEL_DIRNAME}」文件夹"
            if wanted
            else f"没找到权重，把 .gguf 放进「{MODEL_DIRNAME}」文件夹"
        )
        return Plan(False, where, server, None, free, *shape)
    file_mb = max(1, model.stat().st_size // 2**20) if model.exists() else 0
    total = total_memory_mb()
    if file_mb and file_mb > total:
        # 这一条 eager 也越不过：不是余量够不够的问题，是这么大的权重进不了这么大的内存。
        gpu, vram = registry_hardware()[1], vram_mb()
        return Plan(
            False,
            f"这个模型文件有 {file_mb / 1024:.1f} GB，比这台机器的总内存 {total / 1024:.0f} GB 还大"
            + (f"（显卡 {gpu}，显存只有 {vram / 1024:.1f} GB）" if vram else "（没查到独立显卡）")
            + "。这不是「再冒险一点」能解决的，换一个小的或走云端。",
            server,
            model,
            free,
            *shape,
            True,
        )
    if free < need and not eager:
        gap = (
            f"内存只剩 {free // 1024} GB，这一档要约 {need // 1024} GB；"
            "先关点别的、把上下文开小一档，或换云端模型"
        )
        return Plan(False, gap, server, model, free, *shape)
    return Plan(True, "", server, model, free, *shape)


def plan() -> Plan:
    """A pure decision -- nothing is started. The settings row calls this on every read."""
    dial = tuning()
    return verdict_for(dial.model, dial.context, dial.eager)


def preflight(wanted: str, context: int) -> dict[str, object]:
    """给界面的一句话答案：选这个行不行、为什么、这台机器手上有什么。

    读的是 `verdict_for`，所以预览说的和真加载时拦的是同一件事。
    """
    verdict = verdict_for(wanted, context, tuning().eager)
    file_mb = (
        max(1, verdict.model.stat().st_size // 2**20)
        if verdict.model is not None and verdict.model.exists()
        else 0
    )
    return {
        "ok": verdict.can_run,
        "reason": verdict.reason,
        "free_mb": verdict.free_mb,
        "total_mb": total_memory_mb(),
        "need_mb": verdict.needed_mb,
        "file_mb": file_mb,
        "vram_mb": vram_mb(),
        "gpu": registry_hardware()[1],
        "disk_free_mb": disk_free_mb(verdict.model),
        "model_label": card_for(verdict.model).label,
        "hard": verdict.hard,
    }


def build_command(verdict: Plan, port: int) -> list[str]:
    """The llama.cpp arguments, mirrors of the standalone ``start.ps1`` these were measured on.

    ``-ngl`` is left unset on purpose so llama.cpp's own ``--fit`` decides how many layers
    fit in VRAM -- forcing it makes ``--fit`` abort -- and the q8_0 KV cache is what kept a
    4096 window inside this machine's memory.
    """
    assert verdict.exe is not None and verdict.model is not None
    return [
        str(verdict.exe),
        "-m",
        str(verdict.model),
        "-c",
        str(verdict.context),
        "-np",
        "1",
        "-t",
        str(max(1, (psutil.cpu_count(logical=True) or 8) // 2)),
        "--cache-type-k",
        "q8_0",
        "--cache-type-v",
        "q8_0",
        "--flash-attn",
        "on",
        "--fit-target",
        "512",
        "--fit-ctx",
        "2048",
        "--jinja",
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
        "--alias",
        card_for(verdict.model).alias,
    ]


def _free_port() -> int:
    """Our preferred port when nothing else holds it, otherwise one the OS picks.

    Binding-then-closing then handing the number to a child is a race in theory; in
    practice the child starts within a second and ``_wait_ready`` verifies the port
    actually answers ``/health`` before anything trusts it.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        try:
            probe.bind(("127.0.0.1", DEFAULT_PORT))
            return DEFAULT_PORT
        except OSError:
            pass
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _healthy(port: int, timeout: float = 2.0) -> bool:
    # No proxy: the system proxy eats 127.0.0.1 and reports it as the model being down
    # (the same trap r64 fixed for the user-configured local endpoints).
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(f"http://127.0.0.1:{port}{HEALTH_PATH}", timeout=timeout) as answer:
            return int(answer.status) == 200
    except (urllib.error.URLError, OSError, ValueError):
        return False


class LocalServer:
    """One llama-server child, started on demand and stopped when it has been idle."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._proc: subprocess.Popen[bytes] | None = None
        self._phase: ModelPhase = ModelPhase.OFF
        self._detail = ""
        self._port = 0
        self._model = ""
        self._spawned_ctx = 0
        self._load_ms = 0
        self._last_used = 0.0
        self._tail: deque[str] = deque(maxlen=40)
        self._idle_timer: threading.Timer | None = None
        # 有几条问话正在飞。看门狗只看这个数：上一轮就是它把答到一半的 llama-server 收了
        # （用户机器 19:47:12 发出请求 → 19:49:12「空闲 2 分钟」→ 屏幕上只剩 10054）。
        self._busy = 0

    # -- reads -------------------------------------------------------------

    @property
    def phase(self) -> ModelPhase:
        return self._phase

    def endpoint(self) -> str | None:
        """The OpenAI-compatible base URL, only while the server is actually up."""
        if self._phase is ModelPhase.RUNNING and self._port:
            return f"http://127.0.0.1:{self._port}/v1"
        return None

    def status(self) -> dict[str, object]:
        current = plan()
        # ``plan()`` answers "would a cold start fit". While the child is up it has already
        # answered that, and its own ~5 GB is precisely why the free-memory line now reads
        # "not enough" -- reporting can_run=False there made the settings row say 在跑 and the
        # sentence under it say 内存只剩 2 GB at the same time.
        busy = self._phase in (ModelPhase.RUNNING, ModelPhase.LOADING)
        return {
            "phase": self._phase.value,
            "detail": self._detail,
            "can_run": True if busy else current.can_run,
            "reason": "" if busy else current.reason,
            "model": self._model or (current.model.name if current.model else ""),
            "model_label": card_for(current.model).label,
            # What the dial asks for: "" = auto, "none" = 不用离线模型, else a file name.
            "model_selected": tuning().model,
            "models": bundled_catalog(),
            "port": self._port if self._phase is ModelPhase.RUNNING else 0,
            # The window the running child was actually given -- a dial moved while she is
            # up does not resize a loaded model, and the screen must not pretend otherwise.
            "context": self._spawned_ctx or current.context,
            "dialed_context": current.context,
            "acts": (self._spawned_ctx or current.context) >= PACK_TABLE_TOKENS,
            "needed_mb": current.needed_mb,
            "eager": tuning().eager,
            "idle_seconds": tuning().idle_seconds,
            "load_ms": self._load_ms,
            "free_mb": current.free_mb,
        }

    # -- writes ------------------------------------------------------------

    def ensure_running(self, wait_seconds: float = LOAD_TIMEOUT) -> tuple[bool, str]:
        with _RESOURCE_LOCK:
            return self._ensure_running(wait_seconds)

    def _ensure_running(self, wait_seconds: float = LOAD_TIMEOUT) -> tuple[bool, str]:
        """Bring the server up, blocking at most ``wait_seconds``.

        Returns ``(True, "")`` or ``(False, reason)`` -- the caller puts that reason on
        the screen. Called from the chat worker, so it must never raise into the turn.
        """
        stale: subprocess.Popen[bytes] | None = None
        with self._lock:
            up = self._proc is not None and self._proc.poll() is None
            if up and self._phase is ModelPhase.RUNNING:
                self._note_used_locked()
                return True, ""
            if up and self._phase is ModelPhase.LOADING:
                # 已经有人在加载了：等**同一个**子进程就绪，别再起一个。第二个会把
                # self._proc 顶掉，第一个就成了没人管的孤儿 —— 白吃几 GB 内存和一个端口，
                # 而看门狗只认 self._proc 指向的那一个（r86 审计抓到的并发洞）。
                loading = True
            else:
                loading = False
                verdict = plan()
                if not verdict.can_run or verdict.exe is None or verdict.model is None:
                    self._phase = ModelPhase.FAILED
                    self._detail = verdict.reason
                    return False, verdict.reason
                if up:
                    # 进程还活着但状态不是 RUNNING/LOADING（上一次加载失败却没死透）：
                    # 不收掉就换，等于把几 GB 留在后台直到注销。
                    stale, self._proc = self._proc, None
                    self._port = 0
                    self._spawned_ctx = 0
                try:
                    self._spawn_locked(verdict)
                except OSError as exc:  # the exe refused to start at all
                    self._phase = ModelPhase.FAILED
                    self._detail = f"起不来：{exc}"
                    logger.warning("local model could not start: %s", exc)
                    return False, self._detail
                self._phase = ModelPhase.LOADING
                self._detail = f"正在加载 {verdict.model.name}（第一次会慢，几十秒）"
        if stale is not None:
            _terminate(stale)
        if loading:
            logger.info("local model already loading; this caller waits for the same child")
        # The wait happens *outside* the lock so a second caller sees LOADING and the
        # status poll is never blocked behind a 300-second health loop.
        return self._wait_ready(wait_seconds)

    def stop(self, *, reason: str = "手动停止") -> None:
        with self._lock:
            proc, self._proc = self._proc, None
            self._port = 0
            self._spawned_ctx = 0
            self._cancel_idle_timer_locked()
        if proc is None:
            self._phase = ModelPhase.OFF
            self._detail = ""
            return
        logger.info("stopping the local model (%s)", reason)
        _terminate(proc)
        self._phase = ModelPhase.OFF
        self._detail = reason

    def note_used(self) -> None:
        """Call after a request finishes: it is what the idle countdown measures from."""
        with self._lock:
            self._note_used_locked()

    def begin_request(self) -> None:
        """一条问话开始了：这段时间既不算空闲，也不许被空闲关掉。"""
        with self._lock:
            self._busy += 1
            self._note_used_locked()

    def end_request(self) -> None:
        """这条问话打完（或被掐掉）了 —— 倒计时从这一刻重新数起。"""
        with self._lock:
            self._busy = max(0, self._busy - 1)
            self._note_used_locked()

    def rearm_idle(self) -> None:
        """Re-arm the countdown without pretending she was just spoken to.

        The operator moved the 空闲自动关 dial while the model was up. The silence already
        elapsed still counts, so the new wait is applied to it rather than restarted --
        and switching to 不自动关 cancels the timer outright.
        """
        with self._lock:
            if self._proc is None:
                return
            wait = tuning().idle_seconds
            self._cancel_idle_timer_locked()
            if wait <= 0:
                return
            remaining = max(1.0, wait - (time.monotonic() - self._last_used))
            timer = threading.Timer(remaining, self._stop_if_idle)
            timer.daemon = True
            self._idle_timer = timer
            timer.start()

    # -- internals ---------------------------------------------------------

    def _spawn_locked(self, verdict: Plan) -> None:
        assert verdict.exe is not None and verdict.model is not None
        self._port = _free_port()
        self._model = verdict.model.name
        self._spawned_ctx = verdict.context
        self._tail.clear()
        command = build_command(verdict, self._port)
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        self._proc = subprocess.Popen(
            command,
            cwd=str(verdict.exe.parent),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=flags,
        )
        _drain(self._proc, self._tail)
        logger.info("local model starting on 127.0.0.1:%d (%s)", self._port, verdict.model.name)

    def _wait_ready(self, wait_seconds: float) -> tuple[bool, str]:
        started = time.perf_counter()
        deadline = started + wait_seconds
        while time.perf_counter() < deadline:
            proc = self._proc
            if proc is not None and proc.poll() is not None:
                return self._failed(f"模型进程退出（{proc.returncode}）：{_last_lines(self._tail)}")
            if self._port and _healthy(self._port):
                self._load_ms = int((time.perf_counter() - started) * 1000)
                self._phase = ModelPhase.RUNNING
                self._detail = ""
                with self._lock:
                    self._note_used_locked()
                logger.info("local model ready in %d ms", self._load_ms)
                return True, ""
            time.sleep(1.0)
        return self._failed(f"等了 {int(wait_seconds)} 秒还没就绪：{_last_lines(self._tail)}")

    def _failed(self, detail: str) -> tuple[bool, str]:
        self._phase = ModelPhase.FAILED
        self._detail = detail
        logger.warning("local model unavailable: %s", detail)
        with self._lock:
            proc, self._proc = self._proc, None
            self._port = 0
            self._spawned_ctx = 0
        if proc is not None:
            _terminate(proc)
        return False, detail

    def _note_used_locked(self) -> None:
        self._last_used = time.monotonic()
        self._cancel_idle_timer_locked()
        wait = tuning().idle_seconds
        if wait <= 0:
            return  # 0 分钟 = 一直挂着，不自动关
        timer = threading.Timer(wait, self._stop_if_idle)
        timer.daemon = True
        self._idle_timer = timer
        timer.start()

    def _cancel_idle_timer_locked(self) -> None:
        if self._idle_timer is not None:
            self._idle_timer.cancel()
            self._idle_timer = None

    def _stop_if_idle(self) -> None:
        wait = tuning().idle_seconds
        with self._lock:
            if self._busy:
                # 还在答话：重新装一表就走。把正在回答的那一轮掐掉，屏幕上只剩一个连接被重置
                self._note_used_locked()
                return
            idle = time.monotonic() - self._last_used
            if idle < wait - 1:
                return
        self.stop(reason=f"空闲 {int(wait // 60)} 分钟，已释放内存")


def _drain(proc: subprocess.Popen[bytes], tail: deque[str]) -> None:
    """Pump both pipes into a ring buffer on daemon threads.

    A full pipe is how a hidden-window child process hangs forever on Windows, and the
    tail is the only thing that turns "it did not come up" into a sentence with a cause.
    """

    def pump(handle: io.BufferedReader | None) -> None:
        if handle is None:
            return
        for raw in handle:
            tail.append(raw.decode("utf-8", "replace").rstrip()[:300])

    for handle in (proc.stderr, proc.stdout):
        threading.Thread(target=pump, args=(handle,), daemon=True).start()


def _last_lines(tail: deque[str], limit: int = 3) -> str:
    lines = [line for line in list(tail)[-12:] if line.strip()]
    return " / ".join(lines[-limit:]) or "没有任何输出"


def _terminate(proc: subprocess.Popen[bytes], grace: float = 5.0) -> None:
    if proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(grace)
        return
    except subprocess.TimeoutExpired:
        logger.warning("local model ignored the stop request; killing it")
        proc.kill()
        try:
            proc.wait(2.0)
        except subprocess.TimeoutExpired:  # pragma: no cover - a wedged child, nothing left to do
            logger.error("local model could not be killed; it stays running until logout")


_SERVER = LocalServer()


def shared_server() -> LocalServer:
    return _SERVER


def shutdown() -> None:
    """No 5 GB child may outlive the process that started it."""
    _SERVER.stop(reason="程序退出")


atexit.register(shutdown)
