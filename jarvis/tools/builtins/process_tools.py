"""The one tool that touches a running process -- and it does not touch it.

``propose_kill_process`` writes a row into the panel's pending list. That is the whole
of what the model may do. Ending a process is not undoable, the pid may already have
been handed to something else, and the thing it would stop might be the assistant's own
window, so the act stays where 磁盘删除 has always stayed: behind a person pressing
确认 in a strip that shows them the name and the number.

Two refusals happen at *proposal* time rather than waiting for the act, because a
model that is told "no" immediately can say why to the operator:

* the pid is not running, or has changed identity;
* the name is on the protection list -- system-critical processes, and 小夜 herself.

The re-check at the moment of the act is still there and unchanged. This module only
moves the bad news earlier.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol

from jarvis.tools.types import (
    RiskLevel,
    ToolHandler,
    ToolSpec,
    integer_property,
    object_schema,
    string_property,
)


class ProposalQueue(Protocol):
    """``ProcessService``, narrowed to what this tool may ask of it."""

    def propose(self, pid: int, name: str, reason: str = ...) -> Mapping[str, object]: ...


_PROPOSE = ToolSpec(
    name="propose_kill_process",
    description=(
        "请人在界面上确认结束某个进程。这是「提议」，不是「结束」："
        "它把 pid 和进程名送到占用排行面板的确认条上，只有人按下确认才会真的结束，"
        "小夜自己、系统关键进程（lsass / svchost / dwm 等）会在提议这一步就被拒绝。"
        "pid 和 name 必须来自 list_processes 的同一次结果，两者对不上会被直接拒掉。"
        "reason 写清楚为什么要结束它，确认条上会显示这句。"
    ),
    parameters=object_schema(
        {
            "pid": integer_property("进程号，来自 list_processes", minimum=1),
            "name": string_property("那个号上显示出来的进程名，必须一模一样"),
            "reason": string_property("为什么要结束它，一句话，会显示给人看"),
        },
        required=("pid", "name"),
    ),
    risk=RiskLevel.CAUTION,
)


def build(*, proposals: ProposalQueue | None = None) -> list[tuple[ToolSpec, ToolHandler]]:
    """Return the proposal tool, or nothing when there is no panel to propose to.

    The CLI has no window to press 确认 in, so registering this there would be a tool
    that can only ever produce a request nobody can answer -- the mistake this project
    has already made once with ``run_shell``.
    """
    if proposals is None:
        return []
    return [(_PROPOSE, _make_propose(proposals))]


def _make_propose(proposals: ProposalQueue) -> ToolHandler:
    def propose(arguments: Mapping[str, object]) -> str:
        pid = arguments.get("pid")
        name = arguments.get("name")
        reason = arguments.get("reason")
        if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
            return "pid 必须是 list_processes 里的那个正整数"
        if not isinstance(name, str) or not name.strip():
            return "name 必须和进程表里显示的名字一致"
        verdict = proposals.propose(pid, name, reason if isinstance(reason, str) else "")
        if verdict.get("ok"):
            shown = str(verdict.get("name") or name)
            return (
                f"已把「{shown}（pid {pid}）」放到「占用排行」面板的确认条上，"
                "要等人按「确认结束」才会真的停止。现在请叫用户去点那一下。"
            )
        return f"没有提交这个提议：{verdict.get('error')}"

    return propose


__all__ = ["build"]
