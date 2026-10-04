"""The built-in tool set.

One place to see everything JARVIS can do to the machine out of the box, and
the risk level attached to each. Adding a tool means adding it here — there is
no auto-import magic, because a capability that appears without anybody reading
this list is exactly the kind of thing that ends up in a security review.
"""

from __future__ import annotations

from collections.abc import Callable

from jarvis.tools.builtins import (
    assistant_tools,
    computer_tools,
    file_tools,
    insight_tools,
    process_tools,
    selfconfig_tools,
    shell_tools,
    system_tools,
    text_tools,
    time_tools,
    web_tools,
)
from jarvis.tools.disk_cleaner import DiskCleaner
from jarvis.tools.monitor import SystemMonitor
from jarvis.tools.policy import ToolPolicy
from jarvis.tools.types import ToolHandler, ToolSpec


def build_builtin_tools(
    policy: ToolPolicy,
    *,
    monitor_factory: Callable[[], SystemMonitor],
    cleaner_factory: Callable[[], DiskCleaner],
    computer_factory: Callable[[], computer_tools.DesktopControl] | None = None,
    shell_gate_factory: Callable[[], shell_tools.ShellGate | None] | None = None,
    web_opener: object | None = None,
    web_timeout_seconds: float = web_tools.DEFAULT_TIMEOUT_SECONDS,
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
) -> list[tuple[ToolSpec, ToolHandler]]:
    """Assemble every built-in tool.

    Args:
        policy: Shared path/risk policy, so file tools resolve against the same
            configured roots the registry enforces.
        monitor_factory: Builds the telemetry reader on demand.
        cleaner_factory: Builds the junk scanner on demand (read-only here).
        computer_factory: The desktop-control service, or ``None`` to leave the
            mouse and keyboard tools out entirely.
        shell_gate_factory: The command-line level the operator set, or ``None`` to
            leave ``run_powershell`` out. Same rule as the desktop tools: do not
            advertise a capability the registry cannot reach.
        web_opener: Optional ``urllib`` opener override for tests.
        web_timeout_seconds: Per-request timeout for ``fetch_url``.
        speaker: The announcer behind ``speak``, or ``None`` to leave it out.
        reminders: The reminder service behind ``add_reminder`` and its two
            read-only companions. ``None`` leaves all three out -- the same rule as
            the two above: never advertise a capability the registry cannot reach.
        memory: The assistant's long-term memory, behind ``memory_search`` and
            ``remember``. ``None`` (or a service that never started) leaves both out:
            a search that can only ever answer "记忆服务未启动" would teach the model
            that it has memory it cannot use.
        knowledge: The RAG corpus behind ``knowledge_search``. ``None``, or a base
            the operator switched off, leaves the tool out for the same reason -- an
            empty result from a disabled base reads as "you never told me about that".
        monitor: The one sampler the HUD is polling, behind ``system_trend`` and
            ``list_processes``. An instance rather than ``monitor_factory``: these two
            tools have to read the *same* object the window reads, because a fresh
            sampler has no previous CPU sample and answers 0.0% on a busy machine.
            ``None`` (no psutil) leaves both out.
        usage: The token ledger behind ``usage_stats``, or ``None`` to leave it out.
        alerts: The alert centre, read-only through ``system_report``. ``None`` leaves the
            alert lines out of that report rather than replacing them with a reassuring
            "no alerts" -- a missing service is not a healthy machine.
        proposals: The panel's pending-confirmation queue, behind
            ``propose_kill_process``. ``None`` on a headless run: with no window there
            is nobody to press 确认, and a tool that can only produce an unanswered
            request is the ``run_shell`` mistake repeated.

    Returns:
        ``(spec, handler)`` pairs, ready for
        :meth:`jarvis.tools.registry.ToolRegistry.register`.
    """
    return [
        *time_tools.build(),
        *text_tools.build(),
        *file_tools.build(policy),
        *system_tools.build(monitor_factory, cleaner_factory, alerts),
        *web_tools.build(timeout_seconds=web_timeout_seconds, opener=web_opener),
        *(
            shell_tools.build_with_gate(shell_gate_factory, allow_shell=policy.allows_shell)
            if shell_gate_factory is not None
            else shell_tools.build(allow_shell=policy.allows_shell)
        ),
        *(computer_tools.build(computer_factory) if computer_factory is not None else []),
        *assistant_tools.build(
            speaker=speaker,
            reminders=reminders,
            memory=memory,
            knowledge=knowledge,
        ),
        *insight_tools.build(monitor=monitor, usage=usage),
        *process_tools.build(proposals=proposals),
        *selfconfig_tools.build(settings=settings, voices=voices, conversations=conversations),
    ]


__all__ = ["build_builtin_tools"]
