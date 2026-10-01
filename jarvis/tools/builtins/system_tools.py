"""System and disk tools.

Read-only wrappers over the two L2 modules that already existed
(:mod:`jarvis.tools.monitor`, :mod:`jarvis.tools.disk_cleaner`), so the model can
answer "磁盘还剩多少" and "能清出多少空间" without a new implementation and
without gaining the ability to delete anything. The delete path stays in the
HUD, where a human ticks the boxes.

Both tools take no arguments on purpose. "How many processes should I list" is
not a decision the model is better at than the operator, and exposing it would
just be one more way for a prompt to change what the answer looks like.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from typing import Final

from jarvis.tools.disk_cleaner import DiskCleaner
from jarvis.tools.monitor import SystemMonitor
from jarvis.tools.types import ToolHandler, ToolSpec, object_schema

logger = logging.getLogger("jarvis.tools.builtins.system_tools")

MAX_LISTED_DISKS: Final[int] = 12
"""How many mount points the report includes.

Structural bound: a machine with a dozen drives exists, one with a hundred does
not, and the whole point of the summary is that a person can read it.
"""

MAX_LISTED_JUNK_GROUPS: Final[int] = 10
"""How many junk categories the scan summary lists, largest first."""


def _format_size(size: int) -> str:
    """Human-readable size; a person reads "1.2 GB" faster than ten digits."""
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"


_SYSTEM_REPORT = ToolSpec(
    name="system_report",
    description=(
        "读取本机当前状态：CPU 占用、内存与交换分区、各磁盘剩余空间、"
        "开机时长、占用最高的进程。只读。"
    ),
    parameters=object_schema({}),
)

_DISK_SCAN = ToolSpec(
    name="disk_scan",
    description=(
        "扫描本机可清理的临时文件（缓存、临时目录等），只统计不删除。"
        "回答“能清出多少空间”时使用。"
    ),
    parameters=object_schema({}),
)


def _system_report(monitor_factory: Callable[[], SystemMonitor]) -> ToolHandler:
    def handler(arguments: Mapping[str, object]) -> str:
        del arguments  # the tool takes none; declared so the signature is uniform
        payload = monitor_factory().snapshot().to_dict()
        lines = [f"CPU：{payload.get('cpu_percent')}%（{payload.get('cpu_count')} 核）"]
        memory = payload.get("memory")
        if isinstance(memory, Mapping):
            lines.append(
                f"内存：{_format_size(int(memory.get('used_bytes', 0)))} / "
                f"{_format_size(int(memory.get('total_bytes', 0)))}"
                f"（{memory.get('percent')}%）"
            )
        uptime = payload.get("uptime_seconds")
        if isinstance(uptime, (int, float)):
            lines.append(f"开机时长：{uptime / 3600:.1f} 小时")
        disks = payload.get("disks")
        if isinstance(disks, list) and disks:
            lines.append("磁盘：")
            for disk in disks[:MAX_LISTED_DISKS]:
                if not isinstance(disk, Mapping):
                    continue
                lines.append(
                    f"  {disk.get('mountpoint')} 剩余 "
                    f"{_format_size(int(disk.get('free_bytes', 0)))} / "
                    f"{_format_size(int(disk.get('total_bytes', 0)))}"
                    f"（已用 {disk.get('percent')}%）"
                )
        processes = payload.get("processes")
        if isinstance(processes, list) and processes:
            lines.append("占用最高的进程：")
            for process in processes:
                if not isinstance(process, Mapping):
                    continue
                lines.append(
                    f"  {process.get('name')}（pid {process.get('pid')}）"
                    f" CPU {process.get('cpu_percent')}%"
                )
        warnings = payload.get("warnings")
        if isinstance(warnings, list) and warnings:
            lines.append("部分指标读取失败：" + "；".join(str(item) for item in warnings))
        return "\n".join(lines)

    return handler


def _disk_scan(cleaner_factory: Callable[[], DiskCleaner]) -> ToolHandler:
    def handler(arguments: Mapping[str, object]) -> str:
        del arguments  # the tool takes none
        report = cleaner_factory().scan()
        if report.total_bytes <= 0:
            return "没有扫描到可清理的临时文件。"
        lines = [
            f"可清理合计：{_format_size(report.total_bytes)}"
            f"（扫描到 {len(report.groups)} 类，受保护路径跳过 {report.skipped_protected} 个）"
        ]
        ranked = sorted(report.groups, key=lambda group: group.total_bytes, reverse=True)
        for group in ranked[:MAX_LISTED_JUNK_GROUPS]:
            lines.append(
                f"  {group.category}：{_format_size(group.total_bytes)}"
                f"（{len(group.items)} 项）"
            )
        if report.truncated:
            lines.append("  注意：扫描结果被截断，实际可清理量可能更大。")
        lines.append("以上只是统计。要真正删除，请在桌面端「磁盘清理」里勾选后确认。")
        return "\n".join(lines)

    return handler


def build(
    monitor_factory: Callable[[], SystemMonitor],
    cleaner_factory: Callable[[], DiskCleaner],
) -> list[tuple[ToolSpec, ToolHandler]]:
    """Return the tools this module contributes.

    Args:
        monitor_factory: Builds the telemetry reader. A factory, not an
            instance: ``psutil`` may be absent, and the failure belongs at
            invocation time with a readable message rather than at registration.
        cleaner_factory: Builds the junk scanner (read-only usage here).
    """
    return [
        (_SYSTEM_REPORT, _system_report(monitor_factory)),
        (_DISK_SCAN, _disk_scan(cleaner_factory)),
    ]


__all__ = ["build"]
