"""Text and arithmetic tools.

``calculate`` is the reason :mod:`jarvis.tools.safe_eval` exists: the model is
perfectly willing to send ``__import__("os").system("rm -rf /")`` as an
"expression", and the AST whitelist is what makes that a parse error rather
than a shell command.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Final

from jarvis.tools.safe_eval import ExpressionError, evaluate_text
from jarvis.tools.types import (
    ToolHandler,
    ToolSpec,
    integer_property,
    object_schema,
    string_property,
)

_CALCULATE = ToolSpec(
    name="calculate",
    description=(
        "计算一个算术表达式，支持 + - * / % ** 和括号。" "需要精确算术时使用，不要自己心算。"
    ),
    parameters=object_schema(
        {"expression": string_property("要计算的算式，例如 (12+8)*3/2")},
        required=["expression"],
    ),
)

_TEXT_STATS = ToolSpec(
    name="text_stats",
    description="统计一段文本的字符数、行数、词数，以及最常出现的若干字符。",
    parameters=object_schema(
        {
            "text": string_property("要统计的文本"),
            "top_chars": integer_property("返回前几个高频字符", default=5, minimum=0),
        },
        required=["text"],
    ),
)

MAX_TOP_CHARS: Final[int] = 50
"""Upper bound on the character histogram.

A structural safety bound, not a tunable: the point of the argument is to keep
the reply short, so accepting 100000 would defeat its own purpose.
"""


def _calculate(arguments: Mapping[str, object]) -> str:
    expression = arguments.get("expression")
    if not isinstance(expression, str):
        return "缺少算式"
    try:
        value = evaluate_text(expression)
    except ExpressionError as exc:
        return f"计算失败：{exc}"
    # Render 4.0 as 4: the model reads "计算结果是 4" more reliably than "4.0".
    rendered = int(value) if value.is_integer() else round(value, 10)
    return f"计算结果是 {rendered}"


def _text_stats(arguments: Mapping[str, object]) -> str:
    text = arguments.get("text")
    if not isinstance(text, str):
        return "缺少文本"
    raw_top = arguments.get("top_chars")
    top = MAX_TOP_CHARS if not isinstance(raw_top, int) else min(raw_top, MAX_TOP_CHARS)
    lines = text.splitlines()
    words = len([token for token in text.split() if token])
    summary = [
        f"字符数：{len(text)}",
        f"行数：{len(lines)}",
        f"词数（按空白切分）：{words}",
    ]
    if top > 0:
        counts: dict[str, int] = {}
        for character in text:
            if not character.isspace():
                counts[character] = counts.get(character, 0) + 1
        ranked = sorted(counts.items(), key=lambda pair: (-pair[1], pair[0]))[:top]
        summary.append("高频字符：" + "、".join(f"{ch}×{n}" for ch, n in ranked))
    return "\n".join(summary)


def build() -> list[tuple[ToolSpec, ToolHandler]]:
    """Return the tools this module contributes."""
    return [(_CALCULATE, _calculate), (_TEXT_STATS, _text_stats)]


__all__ = ["build"]
