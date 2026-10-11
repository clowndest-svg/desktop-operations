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

from jarvis.tools.builtins import (
    assistant_tools,
    build_builtin_tools,
    computer_tools,
    image_tools,
    insight_tools,
    mirror_tools,
    process_tools,
    selfconfig_tools,
    shell_tools,
    system_tools,
)
from jarvis.tools.registry import ToolRegistry
from jarvis.tools.types import ToolCall, ToolResult, ToolSpec

if TYPE_CHECKING:  # pragma: no cover - typing only
    from jarvis.config.schema import ToolsSection
    from jarvis.tools.disk_cleaner import DiskCleaner
    from jarvis.tools.monitor import SystemMonitor

logger = logging.getLogger("jarvis.tools.service")

_RUN_POWERSHELL: str = "run_powershell"
"""The one tool whose *registration* does not follow the switch that governs it."""

_SHELL_OFF: str = "off"
"""``jarvis.app.command_access.OFF``. Spelled as the wire value because ``tools`` may
not import ``app`` -- the same reason ``ShellGate`` is a structural protocol."""

_DESKTOP_TOOLS: frozenset[str] = frozenset(
    {"open_app", "mouse_move", "mouse_click", "mouse_scroll", "key_press", "type_text"}
)
"""The six that a switched-off 桌面控制 refuses every time. ``computer_status`` is not
here on purpose -- it is how she finds out, and how she says so."""


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
        memory: assistant_tools.MemorySurface | None = None,
        knowledge: assistant_tools.KnowledgeSurface | None = None,
        monitor: SystemMonitor | None = None,
        usage: insight_tools.UsageLedger | None = None,
        proposals: process_tools.ProposalQueue | None = None,
        settings: selfconfig_tools.SettingsMirror | None = None,
        voices: selfconfig_tools.VoiceChanger | None = None,
        conversations: selfconfig_tools.ConversationSearch | None = None,
        alerts: system_tools.AlertCentre | None = None,
        mirror: mirror_tools.MirrorSurface | None = None,
        phone: mirror_tools.PhoneScreenSurface | None = None,
        images: image_tools.ImageEngineSurface | None = None,
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

        ``memory`` and ``knowledge`` are the assistant's own stores. They are checked
        at :meth:`start`, not at construction, because ``start()`` runs after they
        have started -- and a search tool pointed at a store that never came up would
        answer "没找到" about something the operator did tell it.

        ``monitor`` and ``usage`` are the two panels she could not see: the load
        sampler and the token ledger. ``monitor`` must be the *same* instance the HUD
        polls, not another one -- a freshly built ``SystemMonitor`` has no previous CPU
        sample and has therefore been reporting 0.0% on a machine at 80%.
        """
        self._settings_provider = settings_provider
        self._monitor_factory = monitor_factory
        self._cleaner_factory = cleaner_factory
        self._computer_factory = computer_factory
        self._shell_gate_factory = shell_gate_factory
        self._web_opener = web_opener
        self._speaker = speaker
        self._reminders = reminders
        self._memory = memory
        self._knowledge = knowledge
        self._monitor = monitor
        self._usage = usage
        self._proposals = proposals
        self._settings = settings
        self._voices = voices
        self._conversations = conversations
        self._alerts = alerts
        self._mirror = mirror
        self._phone = phone
        self._images = images
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
            memory=self._memory,
            knowledge=self._knowledge,
            monitor=self._monitor,
            usage=self._usage,
            proposals=self._proposals,
            settings=self._settings,
            voices=self._voices,
            conversations=self._conversations,
            alerts=self._alerts,
            mirror=self._mirror,
            # Not stored-and-forgotten: a surface that reaches __init__ but not the
            # builder registers nothing, and the HUD would advertise a capability the
            # registry has never heard of.
            phone=self._phone,
            images=self._images,
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
        """The ``tools`` array for a chat-completion request.

        Filtered to what would actually run if she asked. ``run_powershell`` is the
        case this exists for: its own description promises 「关闭=问不到」, and the
        desktop registers it unconditionally, so on a fresh install the model was
        handed a 182-token invitation to do a thing that can only ever answer 关闭 ——
        on a 4K local window that is a fifth of the budget spent on a dead end, and
        the model then reports to the operator that it tried.

        A gate it cannot read is left **in** rather than dropped: a capability that
        silently disappears is worse than one that refuses with a reason, and the
        refusal now names where to click.
        """
        return [spec.to_openai_schema() for spec in self._registry.specs() if self._reachable(spec)]

    def _reachable(self, spec: ToolSpec) -> bool:
        """Whether this tool would do something, rather than only refuse, right now."""
        if spec.name in _DESKTOP_TOOLS:
            return self._desktop_enabled()
        if spec.name != _RUN_POWERSHELL or self._shell_gate_factory is None:
            return True
        try:
            gate = self._shell_gate_factory()
        except Exception:  # pragma: no cover - a preference read that throws
            logger.exception("could not read the command-line level; advertising it anyway")
            return True
        if gate is None:
            return False
        return gate.mode() != _SHELL_OFF

    def _desktop_enabled(self) -> bool:
        """Whether 桌面控制 is switched on at all, per the level a person picked.

        Tier 关闭's own copy in the popup promises 「工具一条都不注册，模型连问都不能问」.
        The tools are registered regardless -- the level only refuses at call time -- so
        without this filter that sentence is a lie and the model spends a fifth of a 4K
        local window on six tools that can only answer 「已关闭」. ``computer_status`` is
        deliberately *not* filtered: reading the switch is how she explains why the
        other six are gone.
        """
        if self._computer_factory is None:
            return True  # not registered in the first place; nothing to decide here
        try:
            service = self._computer_factory()
            stats = service.stats()
        except Exception:
            logger.exception("could not read the desktop-control level; advertising it anyway")
            return True
        enabled = stats.get("enabled") if isinstance(stats, Mapping) else None
        return True if enabled is None else bool(enabled)

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
