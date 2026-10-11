"""「按我这台电脑给一套建议」背后的三件事：读机器、算规则、问模型。

为什么要两条路：让模型来定这几个旋钮是用户要的体验，但**它可能压根没连上**（没配 Key、
自带模型又被这台机器拒了）。这时候按钮不能变成"点了没反应"或者"假装是 AI 给的"，
所以规则引擎是保底，并且结果里带着 `source` 告诉界面这次是谁说的。
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

import psutil

from jarvis.llm import local_server

logger = logging.getLogger(__name__)

#: 内存留多少余量才敢加载：光"装得下"不够，Windows 自己还要活着。
SAFETY_MARGIN: Final = 1.25
#: 空闲关机的取整档（和界面上那五格一致）。
IDLE_ROOMY: Final = 30
IDLE_NORMAL: Final = 10
IDLE_TIGHT: Final = 2


@dataclass(frozen=True)
class Machine:
    """What this box actually is, as far as the dials care."""

    cpu: str
    cores: int
    total_mb: int
    free_mb: int
    disk_free_mb: int
    gpu: str
    vram_mb: int

    def budget_mb(self) -> int:
        """How much we would dare hand the model, right now, with margin kept."""
        return int(min(self.free_mb, self.total_mb * 0.6) / SAFETY_MARGIN)

    def facts(self) -> str:
        gpu = self.gpu or "没查到独立显卡"
        vram = f"，显存约 {self.vram_mb / 1024:.1f} GB" if self.vram_mb else ""
        return (
            f"CPU：{self.cpu or '未知'}（{self.cores} 逻辑核）；"
            f"内存：共 {self.total_mb / 1024:.1f} GB，现在空闲 {self.free_mb / 1024:.1f} GB；"
            f"显卡：{gpu}{vram}；系统盘之外剩余 {self.disk_free_mb / 1024:.0f} GB"
        )


def _powershell(script: str) -> str:
    try:
        done = subprocess.run(
            ["powershell", "-NoProfile", "-Command", script],
            capture_output=True,
            text=True,
            timeout=20,
        )
        return (done.stdout or "").strip()
    except (OSError, subprocess.SubprocessError) as err:
        logger.info("读机器信息失败（%s），这一项就当不知道", err)
        return ""


def _registry_hardware() -> tuple[str, str, int]:
    """机器档案里的硬件那三项：转读 llm 层那一份，别在这里再实现一遍。

    门槛（`local_server.verdict_for`）和这套建议问的是同一块机器的同一件事；两处各写一份
    注册表读法，迟早一个读得到显卡、另一个读不到，然后屏幕上就会出现两个互相矛盾的答案。
    """
    return local_server.registry_hardware()


def _video_card() -> tuple[str, int]:
    raw = _powershell(
        "$c = Get-CimInstance -ClassName Win32_VideoController | "
        "Sort-Object -Property AdapterRAM -Descending | Select-Object -First 1; "
        'if ($c) { "" + $c.Name + "|" + $c.AdapterRAM }'
    )
    name, _, size = raw.rpartition("|")
    try:
        return name.strip(), int(float(size)) // 2**20
    except ValueError:
        return raw.strip(), 0


def read_machine() -> Machine:
    """Everything the recommendation needs, from the running OS -- not from a guess."""
    virtual = psutil.virtual_memory()
    cpu_name, gpu_name, vram = _registry_hardware()
    if not cpu_name:
        cpu_name = _powershell(
            "(Get-CimInstance -ClassName Win32_Processor | Select-Object -First 1).Name"
        )
    if not gpu_name:
        gpu_name, vram = _video_card()
    from jarvis.config.paths import default_data_dir  # 局部导入：只有按按钮时才要这条路径

    try:
        disk = psutil.disk_usage(str(default_data_dir())).free
    except OSError:
        disk = psutil.disk_usage("C:\\").free
    return Machine(
        cpu=cpu_name,
        cores=int(psutil.cpu_count(logical=True) or 0),
        total_mb=int(virtual.total / 2**20),
        free_mb=int(virtual.available / 2**20),
        disk_free_mb=int(disk / 2**20),
        gpu=gpu_name,
        vram_mb=vram,
    )


@dataclass(frozen=True)
class Advice:
    """A set of dials, plus who produced it and why."""

    model: str
    context: int
    idle_minutes: int
    eager: bool
    reason: str
    source: str  # "ai" | "rule"

    def as_row(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "context": self.context,
            "idle_minutes": self.idle_minutes,
            "eager": self.eager,
            "reason": self.reason,
            "source": self.source,
        }


def _rows(catalog: Sequence[Mapping[str, object]]) -> list[Mapping[str, object]]:
    return [row for row in catalog if row.get("present")]


def _tiers(row: Mapping[str, object]) -> list[Mapping[str, object]]:
    raw = row.get("tiers")
    return list(raw) if isinstance(raw, list) else []


def rule_advice(machine: Machine, catalog: Sequence[Mapping[str, object]]) -> Advice:
    """Deterministic and explainable: biggest file that fits, window that fits beside it."""
    budget = machine.budget_mb()
    options = sorted(_rows(catalog), key=lambda row: -int(str(row["size_mb"])))
    for row in options:
        gate = int(str(row["gate_mb"]))
        if gate > budget:
            continue
        context = local_server.DEFAULT_CONTEXT
        room = budget - gate
        # 用目录那一行自带的档位：界面显示什么，规则就按什么算，不去查另一张表
        wide = next((tier for tier in _tiers(row) if int(str(tier["context"])) >= 8192), None)
        can_act = bool(row["acts_verified"])
        if wide is not None and can_act and int(str(wide["needs_free_mb"])) <= budget:
            context = int(str(wide["context"]))
        minutes = IDLE_ROOMY if room >= gate else (IDLE_TIGHT if room < 1024 else IDLE_NORMAL)
        label = str(row["label"])
        return Advice(
            model=str(row["file"]),
            context=context,
            idle_minutes=minutes,
            eager=False,
            reason=(
                f"这台机器现在空闲 {machine.free_mb / 1024:.1f} GB，"
                f"留了 {SAFETY_MARGIN:.2f} 倍余量之后，"
                f"{label}（4096 档要约 {gate / 1024:.1f} GB）装得下；"
                + (
                    "8K 那一档也装得下，实测见过它调成工具，所以开大让它能动手。"
                    if context == 8192
                    else "再开大窗口就要挤掉余量，先只回答、不动手。"
                )
                + f"问完闲 {minutes} 分钟自动关掉还内存。"
            ),
            source="rule",
        )
    smallest = options[-1] if options else None
    return Advice(
        model=local_server.MODEL_NONE,
        context=local_server.DEFAULT_CONTEXT,
        idle_minutes=IDLE_TIGHT,
        eager=False,
        reason=(
            f"空闲只有 {machine.free_mb / 1024:.1f} GB，"
            + (
                f"连最小的 {smallest['label']}（要约 {smallest['gate_mb']} MB）都放不下，"
                if smallest
                else "包里也没有能加载的权重，"
            )
            + "这一台建议直接用云端模型（设置里配一个 Key），"
            "离线那条路先别开。"
        ),
        source="rule",
    )


PROMPT_HEAD: Final = (
    "你是这台 Windows 机器的配置助手。根据下面的机器实况和可选范围，"
    "**只回一个 JSON 对象**，不要解释文字、不要代码块、不要 markdown 围栏。"
)
FIELDS: Final = ("model", "context", "idle_minutes", "eager")


def build_prompt(machine: Machine, catalog: Sequence[Mapping[str, object]]) -> str:
    lines = [
        PROMPT_HEAD,
        machine.facts(),
        "可选离线模型（只有列出来的才是这台机器上真有的）：",
    ]
    for row in _rows(catalog):
        seen = "实测见过它带 34 个工具调成一次" if row["acts_verified"] else "没实测过它能不能动手"
        lines.append(
            f"- {row['file']}｜{row['label']}｜{int(str(row['size_mb'])) / 1024:.1f} GB｜"
            f"4096 档要约 {row['gate_mb']} MB 空闲内存才加载｜{seen}"
        )
    lines += [
        "上下文档位只能是："
        + "、".join(str(tier) for tier in local_server.CONTEXT_TIERS)
        + "（token）；",
        "空闲自动关只能是："
        + "、".join(str(tier) for tier in local_server.IDLE_TIERS)
        + " 分钟（0 = 不自动关）；",
        "eager 只能是 true 或 false（true = 内存不够也硬加载，一般不给）。",
        'JSON 字段：{"model": 上面那个文件名或 "none", "context": 数字,',
        '  "idle_minutes": 数字, "eager": 布尔,',
        '  "reason": 不超过 80 个汉字的中文，说清为什么这么选}',
        "只回一个 JSON 对象：不要 markdown 围栏、不要多余文字。",
        "取舍：给内存留至少 1.25 倍余量；要动手才需要 8192 以上；内存紧张就选小模型或 none。",
    ]
    return "\n".join(lines)


_JSON = re.compile(r"\{.*\}", re.S)


def parse_advice(text: str, catalog: Sequence[Mapping[str, object]]) -> Advice:
    """Accept a JSON object and nothing looser: every value must be one the UI can apply."""
    match = _JSON.search(text or "")
    if not match:
        raise ValueError("回答里找不到 JSON 对象")
    try:
        data: Mapping[str, Any] = json.loads(match.group(0))
    except ValueError as err:
        raise ValueError(f"JSON 读不通：{err}") from err
    missing = [key for key in FIELDS if key not in data]
    if missing:
        raise ValueError(f"少了字段：{'、'.join(missing)}")
    allowed = {str(row["file"]) for row in _rows(catalog)}
    model = str(data["model"] or "").strip()
    if model not in allowed and model not in (local_server.MODEL_AUTO, local_server.MODEL_NONE):
        raise ValueError(f"它给的模型这台机器上没有：{model}")
    context = int(str(data["context"]))
    if context not in local_server.CONTEXT_TIERS:
        raise ValueError(f"它给的上下文不在档位里：{context}")
    minutes = int(str(data["idle_minutes"]))
    if minutes not in local_server.IDLE_TIERS:
        raise ValueError(f"它给的空闲分钟数不在档位里：{minutes}")
    eager = bool(data["eager"])
    reason = str(data.get("reason") or "").strip()[:200] or "（它没给理由）"
    return Advice(
        model=model, context=context, idle_minutes=minutes, eager=eager, reason=reason, source="ai"
    )


def ai_advice(
    machine: Machine,
    catalog: Sequence[Mapping[str, object]],
    ask: Callable[[str], str],
) -> Advice:
    """Ask, then validate. A model that answers off-list is a failed answer, not a config."""
    text = ask(build_prompt(machine, catalog))
    if not (text or "").strip():
        # 空回答先说"没答上"：没配 Key 和没网都会走到这里，报"找不到 JSON"会把人引偏。
        raise ValueError("她没答上这一句（当前在用的模型没连上或没配 Key）")
    return parse_advice(text, catalog)
