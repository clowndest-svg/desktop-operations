"""The assistant's own tools: say it, and remember to say it later.

Why these are here and not in ``app``
-------------------------------------
The model can only reach what the registry advertises, so "念给我听" and "十分钟后
提醒我" have to be tools. But ``tools`` sits below ``app`` in the dependency table
and that rule is machine-checked, so nothing here imports a reminder or an announcer:
both arrive as the narrow protocols below, and the composition root hands in the real
objects. That is the same seam ``shell_tools`` uses for ``ShellGate`` and
``computer_tools`` uses for ``DesktopControl``.

Why every one of these is SAFE
------------------------------
None of them touch the machine. They write a row in the assistant's own database and
make it talk. The dangerous capabilities are on the other side of a tier the operator
has to raise by hand; putting these behind one would only teach the operator to raise
it for no reason.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Protocol

from jarvis.tools.types import RiskLevel, ToolHandler, ToolSpec, object_schema, string_property


class Speaker(Protocol):
    """The announcer: one sentence in, a line about what happened out."""

    def announce(self, text: str, *, title: str = "小夜") -> str: ...


class Reminders(Protocol):
    """The reminder service, as far as a tool is concerned."""

    def add(self, text: str, when_text: str = "") -> object: ...

    # Sequence, not list: a service returning the concrete row type must still
    # satisfy this, and list is invariant where Sequence is covariant.
    def all_reminders(self) -> Sequence[object]: ...

    def cancel(self, key: str) -> str: ...


def _spec(name: str, description: str, parameters: Mapping[str, object] | None = None) -> ToolSpec:
    return ToolSpec(
        name=name,
        description=description,
        parameters=object_schema(dict(parameters or {})),
        risk=RiskLevel.SAFE,
    )


_SPEAK = _spec(
    "speak",
    "用语音朗读一段话。用户说「念给我听」「读一下」、或者回答本来就是一句要听到的话时使用。"
    "参数 text 是要念出来的内容（标点不会被念出来，数字会被念成中文）。"
    "title 只有弹托盘气泡时看得到，提醒类的播报写「提醒」。",
    {
        "text": string_property("要朗读的文本"),
        "title": string_property("托盘气泡的标题，默认「小夜」"),
    },
)

_ADD = _spec(
    "add_reminder",
    "设置一条提醒。用户说「十分钟后提醒我喝水」「明天九点半提醒我开会」时使用。"
    "when 支持「N 分钟/小时/天后」「今天/明天/后天 + 时间」「19:40」「2026-10-03 08:00」；"
    "听不懂的时间会直接被拒绝，不要换个写法硬试。",
    {
        "text": string_property("到点要提醒的内容，一句话"),
        "when": string_property("什么时候提醒，中文时间短语或 ISO 时间"),
    },
)

_LIST = _spec(
    "list_reminders",
    "列出还没到的提醒。用户问「我还有什么提醒」「下一个提醒是什么时候」时使用。只读。",
)

_CANCEL = _spec(
    "cancel_reminder",
    "取消一条提醒。可以给它的内容里的词（「取消喝水那条」）或列表里的 job_id。"
    "如果有多条都像，会拒绝并要求说得更具体，不会替你猜。",
    {"key": string_property("提醒内容里的关键词，或 job_id")},
)


def _text_of(arguments: Mapping[str, object], key: str) -> str:
    value = arguments.get(key)
    return value.strip() if isinstance(value, str) else ""


def build(
    *,
    speaker: Speaker | None = None,
    reminders: Reminders | None = None,
) -> list[tuple[ToolSpec, ToolHandler]]:
    """Assemble the tools whose backing service exists.

    A missing backing object means the tool is left out rather than registered to
    fail at call time: an advertised capability that answers "not configured" every
    single time is worse than the model not knowing it could have tried.
    """
    tools: list[tuple[ToolSpec, ToolHandler]] = []
    if speaker is not None:
        tools.append((_SPEAK, _make_speak(speaker)))
    if reminders is not None:
        tools.extend(_reminder_tools(reminders))
    return tools


def _make_speak(speaker: Speaker) -> ToolHandler:
    def speak(arguments: Mapping[str, object]) -> str:
        text = _text_of(arguments, "text")
        if not text:
            return "没有要念的内容"
        title = _text_of(arguments, "title")
        return speaker.announce(text, title=title) if title else speaker.announce(text)

    return speak


def _reminder_tools(reminders: Reminders) -> list[tuple[ToolSpec, ToolHandler]]:
    def add(arguments: Mapping[str, object]) -> str:
        text = _text_of(arguments, "text")
        when = _text_of(arguments, "when")
        try:
            made = reminders.add(text, when)
        except Exception as exc:
            return f"设置失败：{exc}"
        return f"已设置提醒：{_field(made, 'text')}，{_field(made, 'when')}"

    def show(arguments: Mapping[str, object]) -> str:
        rows = reminders.all_reminders()
        waiting = [row for row in rows if _field(row, "enabled") is not False]
        if not waiting:
            return "现在没有待办的提醒"
        lines = [f"{_field(row, 'when')} {_field(row, 'text')}" for row in waiting[:20]]
        return "待办提醒：\n" + "\n".join(lines)

    def cancel(arguments: Mapping[str, object]) -> str:
        key = _text_of(arguments, "key") or _text_of(arguments, "text")
        try:
            return f"已取消：{reminders.cancel(key)}"
        except Exception as exc:
            return f"取消失败：{exc}"

    return [(_ADD, add), (_LIST, show), (_CANCEL, cancel)]


def _field(row: object, name: str) -> object:
    """Read one field out of a value object or its dict view.

    The reminder row is a dataclass here and a plain dict after ``to_dict()``, and a
    tool that has to know which one it holds is a tool that will be wrong the first
    time someone changes the service's return type.
    """
    if isinstance(row, dict):
        return row.get(name, "")
    return getattr(row, name, "")


__all__ = ["build"]
