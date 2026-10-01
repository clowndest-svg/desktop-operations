"""Clock and calendar tools.

Trivial on purpose: this is the pair that proves the whole tool path works —
schema, policy, argument validation, invocation, truncation — without touching
the filesystem or the network. ``jarvis --tools`` prints them as a smoke test.
"""

from __future__ import annotations

import datetime
from collections.abc import Mapping
from typing import Final

from jarvis.tools.types import (
    ToolHandler,
    ToolSpec,
    object_schema,
    string_property,
)

_NAME: Final[str] = "current_time"

_SPEC = ToolSpec(
    name=_NAME,
    description=(
        "返回当前日期与时间。当用户问“现在几点”“今天几号”“这周是第几周”时使用。"
        "可以指定时区偏移（例如 +08:00），默认使用本机时区。"
    ),
    parameters=object_schema(
        {
            "timezone_offset": string_property(
                "时区偏移，形如 +08:00 或 -05:00；不填则用本机时区。",
                default="",
            )
        }
    ),
)


def _current_time(arguments: Mapping[str, object]) -> str:
    """Render the current moment, locally and in UTC."""
    raw = arguments.get("timezone_offset")
    offset = raw.strip() if isinstance(raw, str) else ""
    now = datetime.datetime.now().astimezone()
    if offset:
        try:
            sign = 1 if offset[0] == "+" else -1
            hours, minutes = (int(part) for part in offset[1:].split(":", 1))
            now = now.astimezone(
                datetime.timezone(sign * datetime.timedelta(hours=hours, minutes=minutes))
            )
        except (ValueError, IndexError):
            return f"时区偏移格式不对：{offset}，应形如 +08:00"

    weekdays = "一二三四五六日"
    weekday = weekdays[now.weekday()]
    return (
        f"本地时间：{now.strftime('%Y-%m-%d %H:%M:%S')}"
        f"（周{weekday}，UTC{now.strftime('%z')[:3]}:{now.strftime('%z')[3:]}）\n"
        f"UTC 时间：{now.astimezone(datetime.UTC).strftime('%Y-%m-%d %H:%M:%S')}"
    )


def build() -> list[tuple[ToolSpec, ToolHandler]]:
    """Return the tools this module contributes."""
    return [(_SPEC, _current_time)]


__all__ = ["build"]
