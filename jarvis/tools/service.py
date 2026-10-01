"""``ToolService`` — the lifecycle component that owns the tool registry.

Registered in the composition root like every other service. ``start()`` builds
the registry and registers the built-ins; MCP and plugins then add their own
tools to the *same* registry through ``registry_provider``, which is what makes
one policy apply to all three sources.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING

from jarvis.tools.builtins import assistant_tools, build_builtin_tools, computer_tools, shell_tools
from jarvis.tools.registry import ToolRegistry
from jarvis.tools.types import ToolCall, ToolResult, ToolSpec

if TYPE_CHECKING:  # pragma: no cover - typing only
    from jarvis.config.schema import ToolsSection
    from jarvis.tools.disk_cleaner import DiskCleaner
    from jarvis.tools.monitor import SystemMonitor

logger = logging.getLogger("jarvis.tools.service")


class ToolService:
    """Owns the registry and exposes it to the rest of the application."""

    name = "tools"

    def __init__(
        self,
        settings_provider: Callable[[], ToolsSection],
        *,
        monitor_factory: Callable[[], SystemMonitor],
        cleaner_factory: Callable[[], DiskCleaner],
        computer_factory: Callable[[], computer_tools.DesktopControl] | None = None,
        shell_gate_factory: Callable[[], shell_tools.ShellGate | None] | None = None,
        web_opener: object | None = None,
        speaker: assistant_tools.Speaker | None = None,
        reminders: assistant_tools.Reminders | None = None,
        registry: ToolRegistry | None = None,
    ) -> None:
        """Create the service.

        Args:
            settings_provider: Returns the validated ``tools`` config section.
            monitor_factory: Builds the telemetry reader (``psutil`` may be
                missing; that failure belongs at call time).
            cleaner_factory: Builds the junk scanner.
            computer_factory: Builds the desktop-control service for the mouse and
                keyboard tools. ``None`` leaves those tools unregistered: an
                assistant that cannot move a cursor should not advertise that it
                can, and a half-populated tool list is how a model promises the
                operator something that will only ever be refused.
            web_opener: Optional ``urllib`` opener override for tests.
            registry: Optional pre-built registry (tests inject one).

        ``shell_gate_factory`` is the command-line level a person set in the window.
        ``None`` leaves ``run_powershell`` unregistered, which is the same rule the
        desktop tools follow: a capability the registry cannot reach must not be
        advertised, because the model will then promise it to the operator.
        """
        self._settings_provider = settings_provider
        self._monitor_factory = monitor_factory
        self._cleaner_factory = cleaner_factory
        self._computer_factory = computer_factory
        self._shell_gate_factory = shell_gate_factory
        self._web_opener = web_opener
        self._speaker = speaker
        self._reminders = reminders
        self._registry = registry or ToolRegistry(settings_provider)
        self._started = False

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        """Register the built-in tools (idempotent).

        Safe to call twice: registering a name from the same source replaces it,
        so a restart does not produce a duplicate-tool error.
        """
        if self._started:
            return
        settings = self._settings_provider()
        if not settings.enabled:
            # Not an error: a text-only install with no tools is a valid
            # configuration, and the assistant still answers questions.
            self._started = True
            logger.info("tool service disabled by configuration; no tools registered")
            return
        for spec, handler in build_builtin_tools(
            self._registry.policy,
            monitor_factory=self._monitor_factory,
            cleaner_factory=self._cleaner_factory,
            computer_factory=self._computer_factory,
            shell_gate_factory=self._shell_gate_factory,
            speaker=self._speaker,
            reminders=self._reminders,
            web_opener=self._web_opener,
        ):
            self._registry.register(spec, handler)
        self._started = True
        logger.info("tool service ready (%d tools)", len(self._registry.names()))

    def stop(self) -> None:
        """Drop every registration, including MCP and plugin tools."""
        self._registry.clear()
        self._started = False

    @property
    def running(self) -> bool:
        return self._started

    @property
    def enabled(self) -> bool:
        """Whether the config allows tools at all."""
        return self._settings_provider().enabled

    # -- access ------------------------------------------------------------

    @property
    def registry(self) -> ToolRegistry:
        """The shared registry. MCP and plugins register into this instance."""
        return self._registry

    def invoke(
        self,
        name: str,
        arguments: Mapping[str, object] | None = None,
        *,
        confirmed: bool = False,
    ) -> ToolResult:
        """Call a tool by name. Never raises."""
        if not self.enabled:
            return ToolResult(ok=False, error="工具功能未启用（tools.enabled=false）", tool=name)
        return self._registry.invoke(name, arguments, confirmed=confirmed)

    def specs(self) -> list[ToolSpec]:
        """Every registered spec."""
        return self._registry.specs()

    def to_openai_tools(self) -> list[dict[str, object]]:
        """The ``tools`` array for a chat-completion request."""
        return self._registry.to_openai_tools()

    def register(self, spec: ToolSpec, handler: Callable[[Mapping[str, object]], str]) -> None:
        """Register an externally-provided tool (MCP bridge, plugin, app layer).

        Raises:
            ToolError: on a name collision from a different source.
        """
        self._registry.register(spec, handler)

    def recent(self) -> tuple[ToolCall, ...]:
        """The last tool calls, newest first. What the HUD's 最近动作 shows."""
        return self._registry.recent()

    def stats(self) -> dict[str, object]:
        """Read-only snapshot for the HUD and the health check."""
        payload = self._registry.stats()
        payload["running"] = self._started
        payload["enabled"] = self.enabled
        return payload


__all__ = ["ToolService"]
