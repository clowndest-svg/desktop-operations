"""``ComputerService`` — the lifecycle component for desktop control.

Every action is planned, checked against :class:`~jarvis.computer.policy.SafetyPolicy`
and only then executed. ``computer.dry_run`` (the default) stops after the plan,
so the whole pipeline can be exercised — and tested — without the cursor ever
moving.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING

from jarvis.computer.controller import PyAutoGuiController
from jarvis.computer.policy import SafetyPolicy
from jarvis.computer.types import ActionKind, ActionResult, InputController, PlannedAction
from jarvis.core.exceptions import ComputerControlError

if TYPE_CHECKING:  # pragma: no cover - typing only
    from jarvis.config.schema import ComputerSection

logger = logging.getLogger("jarvis.computer.service")

_DRY_RUN_PREFIX = "（演练模式）将会"


def _describe(kind: ActionKind, params: Mapping[str, object]) -> str:
    """Build the Chinese description shown to the user before an action runs.

    The typed text is deliberately reduced to a character count: it is the one
    parameter that routinely holds a password or a private message, and this
    description is logged and surfaced in the HUD.
    """
    match kind:
        case ActionKind.MOVE:
            return f"移动鼠标到 ({params.get('x')}, {params.get('y')})"
        case ActionKind.CLICK:
            label = "双击" if params.get("double") else "单击"
            button = params.get("button", "left")
            return f"{label} ({params.get('x')}, {params.get('y')})，按键 {button}"
        case ActionKind.DOUBLE_CLICK:
            return f"双击 ({params.get('x')}, {params.get('y')})"
        case ActionKind.RIGHT_CLICK:
            return f"右键点击 ({params.get('x')}, {params.get('y')})"
        case ActionKind.TYPE:
            return f"输入 {len(str(params.get('text', '')))} 个字符的文本"
        case ActionKind.KEY:
            return f"按下按键 {params.get('key')}"
        case ActionKind.SCROLL:
            return f"滚动 {params.get('amount')} 个单位"
        case ActionKind.DRAG:
            return (
                f"从 ({params.get('x1')}, {params.get('y1')}) "
                f"拖动到 ({params.get('x2')}, {params.get('y2')})"
            )


class ComputerService:
    """Plan and (when allowed) perform mouse/keyboard actions."""

    name = "computer"

    def __init__(
        self,
        settings_provider: Callable[[], ComputerSection],
        *,
        controller: InputController | None = None,
    ) -> None:
        """Create the service.

        Args:
            settings_provider: Returns the validated ``computer`` config section.
                A provider rather than the section itself because configuration
                does not exist yet at registration time.
            controller: Optional override; tests inject a fake and never touch
                pyautogui.
        """
        self._settings_provider = settings_provider
        self._override = controller
        self._controller: InputController | None = None
        self._policy = SafetyPolicy(settings_provider)
        self._running = False
        self._executed = 0
        self._planned = 0
        self._failed = 0

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        """Mark the service ready. No controller is built and nothing is touched."""
        if self._running:
            return
        section = self._settings_provider()
        self._running = True
        logger.info(
            "computer service ready (dry_run=%s, allow_mouse=%s, allow_keyboard=%s)",
            section.dry_run,
            section.allow_mouse,
            section.allow_keyboard,
        )

    def stop(self) -> None:
        """Drop the controller reference (idempotent)."""
        self._controller = None
        self._running = False

    @property
    def running(self) -> bool:
        """Whether :meth:`start` has run and :meth:`stop` has not."""
        return self._running

    # -- operations --------------------------------------------------------

    def move(self, x: int, y: int) -> ActionResult:
        """Move the pointer to ``(x, y)``."""
        return self._run(self.plan(ActionKind.MOVE, x=x, y=y))

    def click(self, x: int, y: int, *, button: str = "left", double: bool = False) -> ActionResult:
        """Click at ``(x, y)``, optionally twice or with a non-left button."""
        return self._run(self.plan(ActionKind.CLICK, x=x, y=y, button=button, double=double))

    def type_text(self, text: str) -> ActionResult:
        """Type ``text``.

        Marked dangerous, so under the default ``confirm_dangerous=true`` it is
        refused here; a confirmation flow at a higher layer is what would clear
        it. That is intentional for the one action that types secrets.
        """
        return self._run(self.plan(ActionKind.TYPE, text=text))

    def press_key(self, key: str) -> ActionResult:
        """Press a single named key (``enter``, ``esc``, …)."""
        return self._run(self.plan(ActionKind.KEY, key=key))

    def scroll(self, amount: int) -> ActionResult:
        """Scroll by ``amount`` units (positive is up, as pyautogui defines it)."""
        return self._run(self.plan(ActionKind.SCROLL, amount=amount))

    def plan(self, kind: ActionKind, **params: object) -> PlannedAction:
        """Describe an action without performing it.

        Planning is always safe and never consults the policy: the whole point is
        to show the user what *would* happen so they can refuse it.
        """
        return PlannedAction(
            kind=kind,
            params=dict(params),
            description=_describe(kind, params),
            dangerous=kind is ActionKind.TYPE,
        )

    def stats(self) -> dict[str, object]:
        """Read-only snapshot, safe before ``start`` or after ``stop``."""
        section = self._settings_provider()
        return {
            "running": self._running,
            "enabled": section.enabled,
            "dry_run": section.dry_run,
            "controller": self._controller.name if self._controller is not None else "",
            "executed": self._executed,
            "planned": self._planned,
            "failed": self._failed,
        }

    # -- helpers -----------------------------------------------------------

    def _run(self, action: PlannedAction, *, confirmed: bool = False) -> ActionResult:
        """Check, then perform (or plan) one action.

        Policy rejections propagate: they are the designed signal that the user
        asked for something the configuration forbids, and callers need the
        specific exception (mouse vs keyboard vs unconfirmed) to explain it.
        Execution failures do not propagate — they come back as ``ok=False`` so a
        failed click cannot abort an otherwise fine turn.
        """
        self._policy.check(action, confirmed=confirmed)
        section = self._settings_provider()
        if section.dry_run:
            self._planned += 1
            return ActionResult(
                ok=True,
                executed=False,
                detail=f"{_DRY_RUN_PREFIX}{action.description}",
                action=action,
            )
        controller = self._ensure_controller()
        try:
            controller.execute(action)
        except ComputerControlError as exc:
            self._failed += 1
            return ActionResult(
                ok=False,
                executed=False,
                detail=str(exc),
                action=action,
                error=str(exc),
            )
        except Exception as exc:  # a third-party controller may raise anything
            self._failed += 1
            detail = f"桌面操作执行失败：{type(exc).__name__}: {exc}"
            return ActionResult(
                ok=False,
                executed=False,
                detail=detail,
                action=action,
                error=detail,
            )
        self._executed += 1
        return ActionResult(ok=True, executed=True, detail=action.description, action=action)

    def _ensure_controller(self) -> InputController:
        if self._controller is None:
            self._controller = self._override or PyAutoGuiController()
        return self._controller
