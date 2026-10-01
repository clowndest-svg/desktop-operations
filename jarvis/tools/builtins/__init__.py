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

    Returns:
        ``(spec, handler)`` pairs, ready for
        :meth:`jarvis.tools.registry.ToolRegistry.register`.
    """
    return [
        *time_tools.build(),
        *text_tools.build(),
        *file_tools.build(policy),
        *system_tools.build(monitor_factory, cleaner_factory),
        *web_tools.build(timeout_seconds=web_timeout_seconds, opener=web_opener),
        *(
            shell_tools.build_with_gate(shell_gate_factory, allow_shell=policy.allows_shell)
            if shell_gate_factory is not None
            else shell_tools.build(allow_shell=policy.allows_shell)
        ),
        *(computer_tools.build(computer_factory) if computer_factory is not None else []),
        *assistant_tools.build(speaker=speaker, reminders=reminders),
    ]


__all__ = ["build_builtin_tools"]
