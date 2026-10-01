"""``PromptRegistry`` — name-addressed access to prompt templates.

Two access styles, on purpose:

* :func:`render_prompt` / :meth:`PromptRegistry.render` resolve **by name at call
  time**. Use this anywhere an operator might want to retune the wording: the
  override is applied at start-up, and a call-time lookup is what makes it take
  effect without a restart-time re-import.
* The template constants in :mod:`jarvis.prompt.templates` are for tests and for
  the places that genuinely want the shipped text regardless of overrides.

Registering a name that already exists **replaces** it. That is what makes
``prompt.overrides`` work, and it is also the honest behaviour for a registry
whose whole job is "the current wording for this name".
"""

from __future__ import annotations

import logging
from collections.abc import Sequence

from jarvis.core.exceptions import PromptError
from jarvis.prompt.templates import BUILTIN_TEMPLATES
from jarvis.prompt.types import PromptTemplate

logger = logging.getLogger("jarvis.prompt.registry")


class PromptRegistry:
    """Holds the live prompt templates, keyed by name."""

    def __init__(self, templates: Sequence[PromptTemplate] = ()) -> None:
        self._templates: dict[str, PromptTemplate] = {}
        for template in templates:
            self.register(template)

    # -- writes ------------------------------------------------------------

    def register(self, template: PromptTemplate) -> None:
        """Add or replace a template.

        Raises:
            PromptError: on an empty name (the dataclass already rejects one, so
                this guards a hand-built object).
        """
        if not template.name.strip():
            raise PromptError("提示词名称不能为空")
        existing = self._templates.get(template.name)
        if existing is not None and existing.body != template.body:
            logger.info(
                "prompt %r replaced (v%d -> v%d, %d -> %d chars)",
                template.name,
                existing.version,
                template.version,
                len(existing.body),
                len(template.body),
            )
        self._templates[template.name] = template

    def unregister(self, name: str) -> bool:
        """Remove a template. Returns whether it existed."""
        return self._templates.pop(name, None) is not None

    # -- reads -------------------------------------------------------------

    def find(self, name: str) -> PromptTemplate | None:
        """Look a template up, or ``None`` when it is not registered."""
        return self._templates.get(name)

    def get(self, name: str) -> PromptTemplate:
        """Look a template up.

        Raises:
            PromptError: when the name is unknown. The message lists what *is*
                registered — a typo in a prompt name should cost one read, not
                a debugging session.
        """
        template = self._templates.get(name)
        if template is None:
            raise PromptError(
                f"未注册的提示词：{name}",
                details={"prompt": name, "available": sorted(self._templates)},
            )
        return template

    def render(self, name: str, **values: object) -> str:
        """Render a template by name.

        Raises:
            PromptError: on an unknown name or a placeholder mismatch.
        """
        return self.get(name).render(values)

    def names(self) -> list[str]:
        """Every registered name, sorted."""
        return sorted(self._templates)

    def templates(self) -> list[PromptTemplate]:
        """Every registered template, sorted by name."""
        return [self._templates[name] for name in self.names()]

    def stats(self) -> dict[str, object]:
        """Read-only snapshot for the HUD and the health check."""
        templates = self.templates()
        return {
            "count": len(templates),
            "templates": [template.to_dict() for template in templates],
        }


_DEFAULT: PromptRegistry = PromptRegistry(BUILTIN_TEMPLATES)
"""The registry every consumer reads through.

Module-level rather than injected because prompt lookup happens inside objects
that are built before the composition root has anything to inject (an
``AgentGraph``, a ``MemoryExtractor``). ``PromptService`` mutates *this* instance
at start-up, which is what makes a config override reach them.
"""


def default_registry() -> PromptRegistry:
    """The process-wide registry."""
    return _DEFAULT


def render_prompt(name: str, **values: object) -> str:
    """Render a built-in prompt by name (see :func:`default_registry`).

    Raises:
        PromptError: on an unknown name or a placeholder mismatch.
    """
    return _DEFAULT.render(name, **values)


__all__ = [
    "PromptRegistry",
    "default_registry",
    "render_prompt",
]
