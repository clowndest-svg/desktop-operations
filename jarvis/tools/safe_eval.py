"""Safe arithmetic evaluation, shared by the calculator tool and the agent.

``eval()`` on model-authored text is a remote code execution hole with extra
steps, and a hand-rolled regex parser gets precedence wrong. The middle path is
:mod:`ast`: parse with the real Python grammar (so ``2+3*4`` is 14, not 20) and
then walk the tree refusing every node type that is not a number, a binary
operator or a unary sign.

This lives in ``tools`` rather than in ``agent`` because both need it and the
architecture only allows ``agent`` to depend on ``tools`` — not the other way
round. A second copy would be a second place to get the whitelist wrong.
"""

from __future__ import annotations

import ast
import operator
import re
from collections.abc import Callable, Mapping
from typing import Final

_ALLOWED_BINARY: Final[Mapping[type, Callable[[float, float], float]]] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}

_ALLOWED_UNARY: Final[Mapping[type, Callable[[float], float]]] = {
    ast.UAdd: operator.pos,
    ast.USub: operator.neg,
}

_EXPRESSION: Final[re.Pattern[str]] = re.compile(r"[-+*/%(). 0-9eE^]+")
"""Characters an arithmetic expression may contain.

A pre-filter, not the safety mechanism — the AST walk is. It exists so that
"计算 3 加 5" fails with "未找到有效的算式" instead of a syntax error.
"""

MAX_POWER: Final[float] = 1e6
"""Refuse absurd exponents: ``9**9**9`` is a denial of service, not a sum."""


class ExpressionError(ValueError):
    """The text is not an arithmetic expression this module is willing to run."""


def extract_expression(text: str) -> str | None:
    """Pull the arithmetic part out of a sentence like ``计算 1+1``.

    Returns ``None`` when there is nothing that looks like a sum, so the caller
    can answer "我没找到算式" rather than evaluating something surprising.
    """
    # Annotated because ``Pattern.findall`` is typed as returning ``list[Any]``
    # in some typeshed versions; the pattern guarantees ``str``.
    candidates: list[str] = _EXPRESSION.findall(text.replace("^", "**"))
    for candidate in candidates:
        stripped = candidate.strip().rstrip(".")
        if not stripped:
            continue
        if any(character in stripped for character in "+-*/%") and any(
            character.isdigit() for character in stripped
        ):
            return stripped
    return None


def evaluate(expression: str) -> float:
    """Evaluate an arithmetic expression.

    Raises:
        ExpressionError: on a syntax error, a forbidden node type, or a
            division by zero. Every failure mode is one exception type so the
            tool layer has a single thing to catch.
    """
    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError as exc:
        raise ExpressionError(f"不是合法的算式：{expression}") from exc
    if not isinstance(tree, ast.Expression):
        raise ExpressionError("不是合法的算式")
    return _walk(tree.body)


def evaluate_text(text: str) -> float:
    """Extract an expression from ``text`` and evaluate it.

    Raises:
        ExpressionError: if there is no expression, or it cannot be evaluated.
    """
    extracted = extract_expression(text)
    if extracted is None:
        raise ExpressionError("未找到有效的算式")
    return evaluate(extracted)


def _walk(node: ast.AST) -> float:
    if isinstance(node, ast.Constant):
        if isinstance(node.value, bool) or not isinstance(node.value, int | float):
            raise ExpressionError("算式只能包含数字和运算符")
        return float(node.value)
    if isinstance(node, ast.BinOp):
        left = _walk(node.left)
        right = _walk(node.right)
        binary_op = _ALLOWED_BINARY.get(type(node.op))
        if binary_op is None:
            raise ExpressionError(f"不支持的运算符：{type(node.op).__name__}")
        if isinstance(node.op, ast.Pow) and abs(right) > MAX_POWER:
            raise ExpressionError("指数过大")
        try:
            return float(binary_op(left, right))
        except ZeroDivisionError as exc:
            raise ExpressionError("除数不能为零") from exc
    if isinstance(node, ast.UnaryOp):
        operand = _walk(node.operand)
        unary_op = _ALLOWED_UNARY.get(type(node.op))
        if unary_op is None:
            raise ExpressionError(f"不支持的一元运算符：{type(node.op).__name__}")
        return float(unary_op(operand))
    raise ExpressionError(f"不支持的语法：{type(node).__name__}")


__all__ = ["MAX_POWER", "ExpressionError", "evaluate", "evaluate_text", "extract_expression"]
