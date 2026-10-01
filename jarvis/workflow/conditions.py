"""Safe condition evaluation for workflow ``when`` guards.

Why not ``eval``: a workflow file is a *document the user writes*. Running it
through ``eval`` would make "when: ..." a remote-code-execution hole reachable
from a text editor. Instead this module implements a tiny expression language —
``{{ path }}`` references, comparisons, ``contains`` and ``and``/``or``/``not``
— over a hand-written tokenizer and recursive-descent parser. Nothing here can
call a function, import a module or touch an attribute; a path reference only
ever walks mappings and sequences, so ``{{ __import__("os") }}`` resolves to
"missing" rather than running anything.

Supported syntax::

    {{ steps.检查磁盘.output }} contains 低
    {{ variables.mode }} == "fast"
    {{ steps.a.ok }} and not {{ steps.b.ok }}
    {{ steps.count }} > 3
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping, Sequence

from jarvis.core.exceptions import WorkflowError

logger = logging.getLogger("jarvis.workflow.conditions")

_TOKEN_RE = re.compile(
    r"""
    \{\{\s*(?P<var>[^{}]*?)\s*\}\}
  | "(?P<dq>[^"]*)"
  | '(?P<sq>[^']*)'
  | (?P<op>==|!=|>=|<=|>|<)
  | (?P<word>[^\s]+)
    """,
    re.VERBOSE,
)

_KEYWORDS: frozenset[str] = frozenset({"and", "or", "not", "contains"})
_BOOLEANS: frozenset[str] = frozenset({"true", "false"})

# A bare word is only ever a *literal*, never code, so it may contain letters
# (including CJK), digits, dots and hyphens — but never parentheses or quotes.
_BARE_RE = re.compile(r"^[\w.\-]+$")
# Path segments name dict keys; a segment with punctuation cannot be a key, so
# it resolves to "missing" instead of being interpreted.
_SEGMENT_RE = re.compile(r"^[^\W\d]\w*$")

Token = tuple[str, str]


def _as_number(text: str) -> float | None:
    """Parse a numeric literal, returning ``None`` when it is not one."""
    try:
        return float(text)
    except ValueError:
        return None


def _numeric(value: object) -> float | None:
    """Numeric view of a value, treating ``bool`` as a boolean (not 0/1)."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _truthy(value: object) -> bool:
    """Evaluate a bare value as a condition.

    Strings get the usual "false-like" spellings so ``{{ x }}`` over a config
    string behaves the way a user reading the YAML would expect.
    """
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        text = value.strip()
        return text != "" and text.lower() not in {"false", "0", "no", "off", "否", "假"}
    return bool(value)


def _classify(word: str, expression: str) -> Token:
    """Turn a bare word into a keyword, boolean, number or string literal."""
    lowered = word.lower()
    if lowered in _KEYWORDS:
        return ("kw", lowered)
    if lowered in _BOOLEANS:
        return ("bool", lowered)
    if _as_number(word) is not None:
        return ("num", word)
    if not _BARE_RE.match(word):
        raise WorkflowError(
            f"条件表达式包含不支持的记号：{word!r}",
            details={"expression": expression, "token": word},
        )
    return ("str", word)


def _tokenize(expression: str) -> list[Token]:
    """Split an expression into tokens, rejecting anything unrecognised.

    Raises:
        WorkflowError: if a character is neither whitespace nor a token.
    """
    tokens: list[Token] = []
    position = 0
    for match in _TOKEN_RE.finditer(expression):
        gap = expression[position : match.start()]
        if gap.strip():
            raise WorkflowError(
                f"条件表达式包含无法识别的字符：{gap.strip()!r}",
                details={"expression": expression},
            )
        position = match.end()
        var, dq, sq, op = (
            match.group("var"),
            match.group("dq"),
            match.group("sq"),
            match.group("op"),
        )
        if var is not None:
            tokens.append(("var", var))
        elif dq is not None:
            tokens.append(("str", dq))
        elif sq is not None:
            tokens.append(("str", sq))
        elif op is not None:
            tokens.append(("op", op))
        else:
            tokens.append(_classify(match.group("word"), expression))
    tail = expression[position:]
    if tail.strip():
        raise WorkflowError(
            f"条件表达式包含无法识别的字符：{tail.strip()!r}",
            details={"expression": expression},
        )
    if not tokens:
        raise WorkflowError("条件表达式为空", details={"expression": expression})
    return tokens


class _Parser:
    """Recursive-descent parser over the tokens produced by :func:`_tokenize`."""

    def __init__(self, tokens: list[Token], context: Mapping[str, object], expression: str) -> None:
        self._tokens = tokens
        self._context = context
        self._expression = expression
        self._index = 0

    # -- token cursor ------------------------------------------------------

    def _peek(self) -> Token | None:
        if self._index < len(self._tokens):
            return self._tokens[self._index]
        return None

    def _advance(self) -> Token:
        token = self._tokens[self._index]
        self._index += 1
        return token

    # -- grammar -----------------------------------------------------------

    def parse(self) -> bool:
        """Parse the whole expression and reject leftover tokens."""
        value = self._parse_or()
        if self._index != len(self._tokens):
            extra = self._tokens[self._index]
            raise WorkflowError(
                f"条件表达式无法解析：多余的记号 {extra[1]!r}",
                details={"expression": self._expression},
            )
        return value

    def _parse_or(self) -> bool:
        left = self._parse_and()
        while self._peek() == ("kw", "or"):
            self._advance()
            right = self._parse_and()
            left = left or right
        return left

    def _parse_and(self) -> bool:
        left = self._parse_not()
        while self._peek() == ("kw", "and"):
            self._advance()
            right = self._parse_not()
            left = left and right
        return left

    def _parse_not(self) -> bool:
        if self._peek() == ("kw", "not"):
            self._advance()
            return not self._parse_not()
        return self._parse_comparison()

    def _parse_comparison(self) -> bool:
        left = self._parse_primary()
        token = self._peek()
        if token is not None and (token[0] == "op" or token == ("kw", "contains")):
            self._advance()
            right = self._parse_primary()
            return self._compare(left, token[1], right)
        return _truthy(left)

    def _parse_primary(self) -> object:
        token = self._peek()
        if token is None:
            raise WorkflowError("条件表达式意外结束", details={"expression": self._expression})
        kind, value = self._advance()
        if kind == "var":
            return self._resolve(value)
        if kind == "str":
            return value
        if kind == "num":
            return _as_number(value)
        if kind == "bool":
            return value == "true"
        raise WorkflowError(
            f"条件表达式出现意外的记号：{value!r}",
            details={"expression": self._expression},
        )

    # -- semantics ---------------------------------------------------------

    def _compare(self, left: object, operator: str, right: object) -> bool:
        if operator == "contains":
            return str(right) in str(left)
        left_number = _numeric(left)
        right_number = _numeric(right)
        both_numeric = left_number is not None and right_number is not None
        if operator == "==":
            return left_number == right_number if both_numeric else str(left) == str(right)
        if operator == "!=":
            return left_number != right_number if both_numeric else str(left) != str(right)
        if both_numeric:
            assert left_number is not None and right_number is not None
            if operator == ">":
                return left_number > right_number
            if operator == "<":
                return left_number < right_number
            if operator == ">=":
                return left_number >= right_number
            if operator == "<=":
                return left_number <= right_number
        left_text, right_text = str(left), str(right)
        if operator == ">":
            return left_text > right_text
        if operator == "<":
            return left_text < right_text
        if operator == ">=":
            return left_text >= right_text
        if operator == "<=":
            return left_text <= right_text
        raise WorkflowError(
            f"不支持的比较运算符：{operator!r}", details={"expression": self._expression}
        )

    def _resolve(self, path: str) -> object:
        """Walk ``a.b.c`` through the context, returning ``None`` if absent.

        Only mappings and sequences are traversed; there is no attribute access
        and no call, which is what makes a hostile path harmless.
        """
        if not path.strip():
            return None
        current: object = self._context
        for segment in path.split("."):
            segment = segment.strip()
            if not _SEGMENT_RE.match(segment):
                return None
            current = self._descend(current, segment)
            if current is None:
                return None
        return current

    @staticmethod
    def _descend(current: object, segment: str) -> object:
        if isinstance(current, Mapping):
            value: object = current.get(segment)
            return value
        if isinstance(current, Sequence) and not isinstance(current, (str, bytes, bytearray)):
            if segment.isdigit():
                index = int(segment)
                if 0 <= index < len(current):
                    item: object = current[index]
                    return item
            return None
        return None


class ConditionEvaluator:
    """Evaluates workflow ``when`` expressions without ever executing code."""

    def evaluate(self, expression: str, context: Mapping[str, object]) -> bool:
        """Evaluate ``expression`` against ``context``.

        Args:
            expression: The guard text (may reference ``{{ path }}`` values).
            context: Read-only view of variables and previous step results.

        Returns:
            The truth value of the expression.

        Raises:
            WorkflowError: if the expression cannot be tokenized or parsed. A
                malformed guard is a definition bug the user must see, not a
                silently-false condition.
        """
        text = expression.strip()
        if not text:
            return True
        tokens = _tokenize(text)
        return _Parser(tokens, context, text).parse()


__all__ = ["ConditionEvaluator"]
