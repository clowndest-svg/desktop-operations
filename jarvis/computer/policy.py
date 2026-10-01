"""Safety policy for desktop control — the single place that decides 能不能做.

Mouse, keyboard and confirmation gates all live here rather than being spread
across the service, so there is exactly one function to audit and one place a new
action kind has to be classified. ``TYPE`` is treated as dangerous: typed text is
very often a password or a message body, and a policy that cannot see what is
being typed must assume the worst.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING

from jarvis.computer.types import ActionKind, PlannedAction
from jarvis.core.exceptions import ComputerControlError, DangerousOperationRejectedError

if TYPE_CHECKING:  # pragma: no cover - typing only
    from jarvis.config.schema import ComputerSection

logger = logging.getLogger("jarvis.computer.policy")

_MOUSE_KINDS = frozenset(
    {
        ActionKind.MOVE,
        ActionKind.CLICK,
        ActionKind.DOUBLE_CLICK,
        ActionKind.RIGHT_CLICK,
        ActionKind.SCROLL,
        ActionKind.DRAG,
    }
)
_KEYBOARD_KINDS = frozenset({ActionKind.TYPE, ActionKind.KEY})


class SafetyPolicy:
    """Veto actions the configuration does not allow."""

    def __init__(self, section_provider: Callable[[], ComputerSection]) -> None:
        """Create the policy.

        Args:
            section_provider: Returns the validated ``computer`` config section,
                re-read on every check so a configuration reload takes effect
                without rebuilding the service.
        """
        self._section_provider = section_provider

    def check(self, action: PlannedAction, *, confirmed: bool = False) -> None:
        """Raise unless ``action`` is permitted.

        Order matters: a disabled service is reported before a disabled input
        device (nothing can run either way), and the confirmation gate comes last
        because it is the only one a caller can satisfy by asking the user.
        """
        section = self._section_provider()
        if not section.enabled:
            raise ComputerControlError(
                "桌面控制未启用；请在配置中设置 computer.enabled=true",
                details={"enabled": False},
            )
        if action.kind in _MOUSE_KINDS and not section.allow_mouse:
            raise DangerousOperationRejectedError(
                "鼠标操作已被策略禁止（computer.allow_mouse=false）",
                details={"kind": action.kind.value, "reason": "mouse_disabled"},
            )
        if action.kind in _KEYBOARD_KINDS and not section.allow_keyboard:
            raise DangerousOperationRejectedError(
                "键盘操作已被策略禁止（computer.allow_keyboard=false）",
                details={"kind": action.kind.value, "reason": "keyboard_disabled"},
            )
        if action.dangerous and section.confirm_dangerous and not confirmed:
            raise DangerousOperationRejectedError(
                f"该操作被标记为危险，需要用户确认：{action.description}",
                details={"kind": action.kind.value, "reason": "unconfirmed"},
            )
