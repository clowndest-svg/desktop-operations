"""Insight tools: the three panels the model could not see.

Why these exist
---------------
The HUD draws a load trend, a token ledger and a process ranking, and until now the
assistant could answer none of the questions those panels answer -- "刚才那十分钟卡不
卡", "这个月花了多少 token", "谁在吃内存". The data was already collected; there was
simply no wire from it to her.

Why they read through the *same* objects the panels read
--------------------------------------------------------
Not a second sampler. ``system_trend`` and ``list_processes`` take the one
:class:`~jarvis.tools.monitor.SystemMonitor` the window polls, and ``usage_stats`` takes
the one :class:`~jarvis.app.usage_service.UsageService`` the statistics popup aggregates
from. Two readers of one buffer is what makes "her number" and "the number on screen"
the same number; two samplers would be two machines.

All three are SAFE and take no arguments that change what is measured.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Protocol

from jarvis.core.exceptions import ToolError
from jarvis.tools.builtins.system_tools import format_size
from jarvis.tools.monitor import SystemMonitor, TrendPoint
from jarvis.tools.types import (
    RiskLevel,
    ToolHandler,
    ToolSpec,
    integer_property,
    object_schema,
    string_property,
)

TREND_METRICS: frozenset[str] = frozenset({"cpu", "memory", "net", "all"})
"""Which line of the trend panel to report. ``all`` is the default: three short lines
cost the model less than one wrong guess about which one it was asked for."""

MAX_LISTED_PROCESSES = 10
"""Structural cap. The panel shows ten rows and the monitor only caches ten, so a
larger number here would be a promise the sampler cannot keep."""

MAX_LISTED_DAYS = 14
"""How many per-day rows ``usage_stats`` prints -- two weeks of a daily series is what
a person can read in one tool result; the total covers whatever window was asked for."""


class UsageSummaryView(Protocol):
    """The ledger's aggregate, as far as this tool needs it."""

    def to_dict(self) -> dict[str, object]: ...


class UsageLedger(Protocol):
    """``UsageService`` narrowed to its read path.

    ``app`` is above ``tools`` in the dependency table, so this is a structural
    declaration rather than an import -- the same seam the desktop bridge uses for
    memory and knowledge.
    """

    def summary(self, days: int = ...) -> UsageSummaryView: ...

    def daily(self, days: int = ...) -> Sequence[Mapping[str, object]]: ...

    def by_provider(self, days: int = ...) -> Sequence[Mapping[str, object]]: ...


def _spec(
    name: str,
    description: str,
    parameters: Mapping[str, object] | None = None,
) -> ToolSpec:
    return ToolSpec(
        name=name,
        description=description,
        parameters=object_schema(dict(parameters or {})),
        risk=RiskLevel.SAFE,
    )


_SYSTEM_TREND = _spec(
    "system_trend",
    "读负载趋势面板的历史采样：过去一段时间 CPU、内存、网速的均值和峰值。"
    "回答「刚才卡不卡」「这十分钟负载高不高」「内存是不是一直涨」这类问题用它，"
    "当前的瞬时读数用 system_report。只读。"
    "注意：采样是界面每次刷新时记下的，历史最长只保留几十分钟，问得更久会明确告诉你覆盖到多久。",
    {
        "minutes": integer_property("往回看多少分钟，默认 10", minimum=1),
        "metric": string_property("cpu / memory / net / all，默认 all"),
    },
)

_USAGE_STATS = _spec(
    "usage_stats",
    "读 Token 用量统计面板：这段时间调用了多少次、输入输出多少 token、"
    "缓存命中率、平均耗时，并按模型拆开。回答「这个月花了多少」「哪个模型最费」"
    "「缓存还灵不灵」用它。只读，最长查 31 天。",
    {"days": integer_property("往回看多少天，默认 7，最多 31", minimum=1)},
)

_LIST_PROCESSES = _spec(
    "list_processes",
    "读进程排行面板：占用最高的几个进程是谁、各占多少。"
    "回答「谁在吃内存」「哪个进程最占 CPU」用它。只读。"
    "这份表是按内存取的前若干名，所以按 CPU 排时只能在这些进程里排，回答时别说成全盘第一。"
    "要结束进程只能提议，得人在界面上确认。",
    {
        "sort_by": string_property("memory（默认，和面板一致）或 cpu"),
        "limit": integer_property("返回几行，默认 10，最多 10", minimum=1),
    },
)


def build(
    *,
    monitor: SystemMonitor | None = None,
    usage: UsageLedger | None = None,
) -> list[tuple[ToolSpec, ToolHandler]]:
    """Assemble the tools whose backing reader exists.

    ``monitor`` is the one sampler the window is polling, not a factory for another
    one: a fresh ``SystemMonitor`` has no previous CPU sample, and a tool reading that
    would answer 0.0% about a machine the panel is showing at 80%. ``None`` -- psutil
    absent, telemetry unavailable -- leaves both machine tools out.
    """
    tools: list[tuple[ToolSpec, ToolHandler]] = []
    if monitor is not None:
        tools.append((_SYSTEM_TREND, _system_trend(monitor)))
        tools.append((_LIST_PROCESSES, _list_processes(monitor)))
    if usage is not None:
        tools.append((_USAGE_STATS, _usage_stats(usage)))
    return tools


def _int_of(arguments: Mapping[str, object], key: str, fallback: int) -> int:
    """A positive integer argument, or the default. bool is an int and is not an answer."""
    value = arguments.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return fallback
    return value


def _system_trend(monitor: SystemMonitor) -> ToolHandler:
    def handler(arguments: Mapping[str, object]) -> str:
        minutes = _int_of(arguments, "minutes", 10)
        metric = str(arguments.get("metric") or "all").strip().lower()
        if metric not in TREND_METRICS:
            raise ToolError(
                f"metric 只能是 {'、'.join(sorted(TREND_METRICS))} 之一，收到的是「{metric}」"
            )
        points = monitor.trend(minutes)
        if not points:
            return (
                f"最近 {minutes} 分钟内没有任何负载采样。"
                "采样是在界面刷新时记下的，刚启动或窗口一直收着的时候不会有。"
            )
        held = _held_minutes(points)
        lines = [
            f"负载趋势：要看 {minutes} 分钟，实际有 {len(points)} 个采样点"
            f"（覆盖 {_format_minutes(held)}）"
        ]
        if held < minutes - 1:
            lines.append("  注意：更早的采样没有被保留，下面只统计保留到的这一段。")
        if metric in ("cpu", "all"):
            lines.extend(_describe("CPU", points, lambda point: point.cpu_percent, "%"))
        if metric in ("memory", "all"):
            lines.extend(_describe("内存", points, lambda point: point.memory_percent, "%"))
        if metric in ("net", "all"):
            lines.extend(_describe_rate("网络", points))
        return "\n".join(lines)

    return handler


def _held_minutes(points: Sequence[TrendPoint]) -> float:
    """How far back the retained samples actually reach."""
    if not points:
        return 0.0
    return max(0.0, (points[-1].taken_at - points[0].taken_at) / 60.0)


def _describe(
    label: str,
    points: Sequence[TrendPoint],
    read: Callable[[TrendPoint], float | None],
    unit: str,
) -> list[str]:
    """Mean / peak / last for one series, skipping the points that were never measured."""
    values = [value for value in (read(point) for point in points) if value is not None]
    if not values:
        return [f"{label}：这一段里没有有效读数（首个采样没有可比较的前值）"]
    latest = values[-1]
    return [
        f"{label}：均值 {sum(values) / len(values):.1f}{unit}，"
        f"峰值 {max(values):.1f}{unit}，最低 {min(values):.1f}{unit}，最后 {latest:.1f}{unit}"
    ]


def _describe_rate(label: str, points: Sequence[TrendPoint]) -> list[str]:
    """Network rates are bytes/second, so they get their own formatting pass."""
    received = [
        value
        for value in (point.receive_bps for point in points)
        if value is not None and value > 0
    ]
    sent = [
        value for value in (point.send_bps for point in points) if value is not None and value > 0
    ]
    if not received and not sent:
        return [f"{label}：这一段没有读到非零速率"]
    lines = []
    for name, values in (("下行", received), ("上行", sent)):
        if values:
            lines.append(
                f"{label}{name}：峰值 {format_size(int(max(values)))}/s，"
                f"均值 {format_size(int(sum(values) / len(values)))}/s（{len(values)} 个点有流量）"
            )
    return lines


def _usage_stats(usage: UsageLedger) -> ToolHandler:
    def handler(arguments: Mapping[str, object]) -> str:
        days = min(_int_of(arguments, "days", 7), 31)
        summary = usage.summary(days).to_dict()
        calls = summary.get("calls", 0)
        window = f"{summary.get('since', '')} ~ {summary.get('until', '')}"
        if not isinstance(calls, int) or calls == 0:
            return f"最近 {days} 天（{window}）没有任何模型调用记录。"
        cache = summary.get("cache_hit_percent")
        lines = [
            f"Token 用量（最近 {days} 天，{window}）：",
            f"  调用 {calls} 次，输入 {summary.get('prompt_tokens')} / "
            f"输出 {summary.get('completion_tokens')} / 合计 {summary.get('total_tokens')} token",
            f"  平均单次耗时 {summary.get('avg_latency_ms')} 毫秒",
        ]
        if isinstance(cache, (int, float)):
            lines.append(
                f"  缓存命中 {cache:.1f}%（命中 {summary.get('cached_tokens')} / "
                f"输入 {summary.get('prompt_tokens')}）"
            )
        else:
            lines.append(
                f"  缓存命中率：无读数（{summary.get('calls_without_cache_data')} 次调用"
                "里 provider 没有回报缓存字段，不是 0%）"
            )
        rows = usage.by_provider(days)
        if rows:
            lines.append("  按模型：")
            for row in rows[:8]:
                hit = row.get("cache_hit_percent")
                tail = f"，命中 {hit:.1f}%" if isinstance(hit, (int, float)) else ""
                lines.append(
                    f"    {row.get('provider')} / {row.get('model')}："
                    f"{row.get('calls')} 次、{row.get('total_tokens')} token{tail}"
                )
        daily = usage.daily(days)[-MAX_LISTED_DAYS:]
        if daily:
            lines.append("  按天：")
            for row in daily:
                lines.append(
                    f"    {row.get('day')}：{row.get('calls')} 次、"
                    f"{row.get('prompt_tokens')} + {row.get('completion_tokens')} token"
                )
        return "\n".join(lines)

    return handler


def _list_processes(monitor: SystemMonitor) -> ToolHandler:
    def handler(arguments: Mapping[str, object]) -> str:
        sort_by = str(arguments.get("sort_by") or "memory").strip().lower()
        if sort_by not in ("memory", "cpu"):
            raise ToolError("sort_by 只能是 memory 或 cpu")
        limit = min(_int_of(arguments, "limit", MAX_LISTED_PROCESSES), MAX_LISTED_PROCESSES)
        snapshot = monitor.latest()
        if snapshot is None:
            # Nothing has polled the sampler yet -- a ``--ask`` run, or the first
            # question before the window's poll lands. Take one reading rather than
            # reporting an empty that is an artefact of nobody having asked yet.
            # ``system_trend`` deliberately does *not* do this: one point is not a trend.
            snapshot = monitor.snapshot()
        if not snapshot.top_processes:
            return "读不到进程列表（这台机器上一次进程遍历都没有成功）"
        ranked = sorted(
            snapshot.top_processes,
            key=(
                (lambda row: row.memory_bytes)
                if sort_by == "memory"
                else (lambda row: row.cpu_percent or 0.0)
            ),
            reverse=True,
        )
        label = "内存" if sort_by == "memory" else "CPU"
        lines = [
            f"进程排行（按{label}，取前 {limit} 行，与界面「进程」面板同一份数据；"
            f"CPU 是最近 10 秒的平均占用，一个核算 100%）："
        ]
        for row in ranked[:limit]:
            share = "尚未测到" if row.cpu_percent is None else f"{row.cpu_percent:.1f}%"
            lines.append(
                f"  {row.name}（pid {row.pid}）内存 {format_size(row.memory_bytes)}，CPU {share}"
            )
        if snapshot.warnings:
            lines.append("采样注意：" + "；".join(snapshot.warnings))
        return "\n".join(lines)

    return handler


def _format_minutes(minutes: float) -> str:
    return f"{minutes:.0f} 分钟" if minutes >= 1 else f"{minutes * 60:.0f} 秒"


__all__ = ["build"]
