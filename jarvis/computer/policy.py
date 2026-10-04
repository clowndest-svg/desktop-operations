"""Safety policy for desktop control — the single place that decides 能不能做.

Mouse, keyboard, typing and confirmation gates all live here rather than being
spread across the service, so there is exactly one function to audit and one place
a new action kind has to be classified.

``TYPE`` has a lock of its own —— ``computer.allow_typing`` —— 因为打出去的字很可能是密码
或消息正文。它**故意不走** ``confirm_dangerous``/``confirmed`` 那条路：工具层明确不让模型传
``confirmed``（能自己按确认的闸就是摆设），两条合起来等于**永久关闭** —— 用户把档位开到
「键鼠全开」、说"给老妈发句你好"，打字那一步仍被拒，而界面上没有任何开关能打开它。

现在那把钥匙就是「允许打字」：默认关，开了等于操作者本人对"她会把字写进别人的输入框"这件事
明确点头。所以它配得上当钥匙，而不是让模型自己去按确认。
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
        if action.kind is ActionKind.TYPE:
            # 打字有自己的一把锁(见模块开头): 独立于档位, 也独立于 confirm_dangerous.
            if not section.allow_typing:
                raise DangerousOperationRejectedError(
                    "输入文字需要先打开「允许打字」（computer.allow_typing=false）："
                    "打出去的字可能落在别人的对话框里，所以它是独立于档位的一把钥匙，默认关着。",
                    details={"kind": action.kind.value, "reason": "typing_disabled"},
                )
        elif action.dangerous and section.confirm_dangerous and not confirmed:
            # TYPE 在上面单独处理(它的同意就是那把开关); 这条留给以后新增的危险动作.
            raise DangerousOperationRejectedError(
                f"该操作被标记为危险，需要用户确认：{action.description}",
                details={"kind": action.kind.value, "reason": "unconfirmed"},
            )
