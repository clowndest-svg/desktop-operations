"""Prompt template vocabulary.

A template is *text plus a contract*: which placeholders it declares, and which
version of the wording this is. Both matter more than they look.

Declared placeholders mean a missing value is a loud error instead of a prompt
that silently says "关于 {user} 的偏好" to the model. A version number means
"the persona changed" shows up in a diff and in a log line, rather than as an
unexplained behaviour change three weeks later.
"""

from __future__ import annotations

import string
from collections.abc import Mapping
from dataclasses import dataclass

from jarvis.core.exceptions import PromptError

_FORMATTER = string.Formatter()


@dataclass(frozen=True, slots=True)
class PromptTemplate:
    """One versioned piece of prompt text."""

    name: str
    """Stable id, e.g. ``hud_assistant``. Referenced by code, never by prose."""

    body: str
    """The template text. ``{placeholder}`` fields are filled by :meth:`render`."""

    description: str = ""
    """Why this prompt exists and what it constrains. Read by humans only."""

    version: int = 1
    """Bumped whenever the wording changes.

    Not decoration: this assistant's behaviour is largely prompt-defined, so
    "which prompt was live when this answer looked wrong" is a real question.
    The version is logged with every override.
    """

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise PromptError("提示词名称不能为空")
        if not self.body.strip():
            raise PromptError(f"提示词内容不能为空：{self.name}")
        if self.version < 1:
            raise PromptError(f"提示词版本号必须 >= 1：{self.name}")

    @property
    def variables(self) -> tuple[str, ...]:
        """Placeholder names this template expects, in first-appearance order.

        ``{{`` / ``}}`` escapes are skipped: the supervisor prompt embeds JSON
        examples, and treating those braces as placeholders would make an
        otherwise valid template fail to render.
        """
        found: list[str] = []
        for _literal, field_name, _spec, _conversion in _FORMATTER.parse(self.body):
            if field_name is None:
                continue
            root = field_name.split(".")[0].split("[")[0]
            if root and root not in found:
                found.append(root)
        return tuple(found)

    def render(self, values: Mapping[str, object] | None = None) -> str:
        """Fill the placeholders.

        Strict on purpose — both a missing value and an extra one are errors:

        * missing → the model would be shown a literal ``{user}``;
        * extra → a caller believes it is influencing the prompt when it is not,
          which is the failure mode that makes prompt bugs hard to find.

        Raises:
            PromptError: on a missing or unknown placeholder.
        """
        provided = dict(values or {})
        expected = set(self.variables)
        missing = sorted(expected - set(provided))
        if missing:
            raise PromptError(
                f"提示词 {self.name} 缺少变量：{', '.join(missing)}",
                details={"prompt": self.name, "missing": missing},
            )
        unknown = sorted(set(provided) - expected)
        if unknown:
            raise PromptError(
                f"提示词 {self.name} 收到未声明的变量：{', '.join(unknown)}",
                details={"prompt": self.name, "unknown": unknown},
            )
        try:
            return self.body.format(**provided)
        except (KeyError, IndexError, ValueError) as exc:
            raise PromptError(
                f"提示词 {self.name} 渲染失败：{exc}",
                details={"prompt": self.name},
            ) from exc

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "description": self.description,
            "version": self.version,
            "variables": list(self.variables),
            "length": len(self.body),
        }


@dataclass(frozen=True, slots=True)
class PromptOverride:
    """An operator-supplied replacement for one template's body."""

    name: str
    body: str
    version: int = 1
    description: str = "operator override"
