"""``PromptService`` — lifecycle component that owns the live prompt registry.

``start()`` registers the built-in templates and then applies whatever
``prompt.overrides`` the operator configured. It must therefore run **before**
the components that read prompts are built, which is why it sits near the top of
the composition root's registration order — right after configuration.

Consumers read through :func:`jarvis.prompt.render_prompt`, which resolves
against the same registry this service mutates, so an override reaches an
``AgentGraph`` constructed later without any wiring between the two.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING

from jarvis.prompt.registry import PromptRegistry, default_registry
from jarvis.prompt.templates import BUILTIN_TEMPLATES
from jarvis.prompt.types import PromptTemplate

if TYPE_CHECKING:  # pragma: no cover - typing only
    from jarvis.config.schema import PromptSection

logger = logging.getLogger("jarvis.prompt.service")


class PromptService:
    """Registers the shipped prompts and applies config overrides."""

    name = "prompt"

    def __init__(
        self,
        settings_provider: Callable[[], PromptSection],
        *,
        registry: PromptRegistry | None = None,
    ) -> None:
        """Create the service.

        Args:
            settings_provider: Returns the validated ``prompt`` config section.
            registry: Optional registry override. Tests inject a private one so
                an override applied in one test cannot leak into the next — the
                default registry is process-wide.
        """
        self._settings_provider = settings_provider
        self._registry = registry or default_registry()
        self._overridden: tuple[str, ...] = ()
        self._started = False

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        """Register the built-ins, then apply overrides (idempotent)."""
        if self._started:
            return
        for template in BUILTIN_TEMPLATES:
            self._registry.register(template)
        self._overridden = self._apply_overrides()
        self._started = True
        logger.info(
            "prompt service ready (%d template(s)%s)",
            len(self._registry.names()),
            f", {len(self._overridden)} overridden" if self._overridden else "",
        )

    def stop(self) -> None:
        """Restore the shipped text for every overridden template.

        Not just a flag flip: leaving an override in place after stop() would
        make a restart-without-overrides test pass for the wrong reason.
        """
        if self._overridden:
            shipped = {template.name: template for template in BUILTIN_TEMPLATES}
            for name in self._overridden:
                template = shipped.get(name)
                if template is not None:
                    self._registry.register(template)
        self._overridden = ()
        self._started = False

    @property
    def running(self) -> bool:
        return self._started

    @property
    def overridden(self) -> tuple[str, ...]:
        """Names whose body came from configuration rather than the package."""
        return self._overridden

    # -- access ------------------------------------------------------------

    @property
    def registry(self) -> PromptRegistry:
        """The live registry (the shared default unless one was injected)."""
        return self._registry

    def render(self, name: str, **values: object) -> str:
        """Render a prompt by name."""
        return self._registry.render(name, **values)

    def get(self, name: str) -> PromptTemplate:
        """Fetch a template by name."""
        return self._registry.get(name)

    def names(self) -> list[str]:
        """Every registered prompt name."""
        return self._registry.names()

    def templates(self) -> list[PromptTemplate]:
        """Every registered template."""
        return self._registry.templates()

    def stats(self) -> dict[str, object]:
        """Read-only snapshot for the HUD and the health check."""
        payload = self._registry.stats()
        payload["running"] = self._started
        payload["overridden"] = list(self._overridden)
        return payload

    # -- internals ---------------------------------------------------------

    def _apply_overrides(self) -> tuple[str, ...]:
        overrides: Mapping[str, str] = self._settings_provider().overrides
        if not overrides:
            return ()
        applied: list[str] = []
        for name, body in overrides.items():
            existing = self._registry.find(name)
            if existing is None:
                # A typo must leave the assistant working, not refuse to boot.
                logger.warning(
                    "ignoring prompt override for unknown template %r (known: %s)",
                    name,
                    ", ".join(self._registry.names()),
                )
                continue
            self._registry.register(
                PromptTemplate(
                    name=name,
                    body=body,
                    description=existing.description,
                    version=existing.version + 1,
                )
            )
            applied.append(name)
        return tuple(applied)


__all__ = ["PromptService"]
