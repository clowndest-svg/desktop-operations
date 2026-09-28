"""Tool-calling agent: a small, *real* toolset the supervisor can route to.

Tools execute locally (current time, safe arithmetic), so the agent is fully
testable without any network or LLM. When no built-in tool matches the input
it returns an empty result, signalling the router to fall back to chat.
"""

from __future__ import annotations

import ast
import datetime
import operator
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Final, cast

from jarvis.agent.types import AgentContext, AgentResult

# Whitelisted arithmetic operators — keeps ``calculate`` safe (no attribute
# access, no calls, no comprehensions).
_ALLOWED_OPS: Final[Mapping[type, Callable[..., Any]]] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.USub: operator.neg,
    ast.Mod: operator.mod,
}


@dataclass(frozen=True, slots=True)
class Tool:
    """One built-in capability the tool agent can invoke."""

    name: str
    """Stable tool id (referenced by the supervisor / tests)."""

    description: str
    """Human-readable purpose."""

    handler: Callable[[str], str]
    """Maps the argument string to a result string."""


class ToolAgent:
    """Routes simple, structured requests to built-in tools (real execution)."""

    name = "tools"

    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._tools: dict[str, Tool] = {
            "get_time": Tool("get_time", "返回当前本地时间", self._get_time),
            "calculate": Tool("calculate", "计算一个算术表达式", self._calculate),
        }

    def run(self, context: AgentContext) -> AgentResult:
        tool, args = self._detect(context.user_text)
        if tool is None:
            return AgentResult(text="", used_tools=())
        return AgentResult(text=tool.handler(args), used_tools=(tool.name,))

    # -- detection ---------------------------------------------------------

    def _detect(self, text: str) -> tuple[Tool | None, str]:
        if any(k in text for k in ("时间", "几点", "现在", "日期", "time", "date")):
            return self._tools["get_time"], ""
        if any(c in text for c in "+-*/%") and any(ch.isdigit() for ch in text):
            return self._tools["calculate"], text
        return None, ""

    # -- handlers ----------------------------------------------------------

    def _get_time(self, _args: str) -> str:
        now = datetime.datetime.fromtimestamp(self._clock())
        return f"当前时间：{now.strftime('%Y-%m-%d %H:%M:%S')}"

    def _calculate(self, expr: str) -> str:
        # The user text may be "计算 1+1"; pull out just the arithmetic part.
        extracted = self._extract_expr(expr)
        if extracted is None:
            return "计算失败：未找到有效的算式"
        try:
            value = self._eval(extracted)
        except (SyntaxError, ValueError, ZeroDivisionError, TypeError) as exc:
            return f"计算失败：{exc}"
        return f"计算结果是 {value}"

    _EXPR_RE: Final[re.Pattern[str]] = re.compile(r"[-+*/%0-9.() ]+")

    def _extract_expr(self, text: str) -> str | None:
        for candidate in cast("list[str]", self._EXPR_RE.findall(text)):
            candidate = candidate.strip()
            if not candidate:
                continue
            if any(c in candidate for c in "+-*/%") and any(ch.isdigit() for ch in candidate):
                return candidate
        return None

    def _eval(self, expr: str) -> float:
        tree = ast.parse(expr, mode="eval")
        if not isinstance(tree, ast.Expression):
            raise ValueError("无效的表达式")
        return self._eval_node(tree.body)

    def _eval_node(self, node: ast.AST) -> float:
        if isinstance(node, ast.BinOp):
            left = self._eval_node(node.left)
            right = self._eval_node(node.right)
            op = _ALLOWED_OPS.get(type(node.op))
            if op is None:
                raise ValueError(f"不支持的运算符: {type(node.op).__name__}")
            return float(op(left, right))
        if isinstance(node, ast.UnaryOp):
            operand = self._eval_node(node.operand)
            op = _ALLOWED_OPS.get(type(node.op))
            if op is None:
                raise ValueError(f"不支持的运算符: {type(node.op).__name__}")
            return float(op(operand))
        if isinstance(node, ast.Constant):
            if isinstance(node.value, (int, float)):
                return float(node.value)
            raise ValueError("表达式只能包含数字和运算符")
        raise ValueError("表达式只能包含数字和运算符")
