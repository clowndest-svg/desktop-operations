"""Tests for the computer-control layer (jarvis.computer).

No test moves the real cursor or types on the real keyboard: the service is
driven through a fake controller and the pyautogui adapter is exercised at its
lazy-import seam. ``dry_run`` is covered explicitly because it is the default —
if it ever stopped working, the test suite would start clicking on whatever
window happens to be focused.
"""

from __future__ import annotations

import importlib
from typing import Any

import pytest

from jarvis.computer import (
    ActionKind,
    ActionResult,
    ComputerService,
    InputController,
    PlannedAction,
    PyAutoGuiController,
    SafetyPolicy,
)
from jarvis.computer import controller as controller_module
from jarvis.config.schema import ComputerSection
from jarvis.core.exceptions import ComputerControlError, DangerousOperationRejectedError


def _section(**overrides: object) -> ComputerSection:
    data: dict[str, object] = {
        "enabled": True,
        "dry_run": True,
        "allow_mouse": True,
        "allow_keyboard": True,
        "confirm_dangerous": False,
    }
    data.update(overrides)
    return ComputerSection.from_mapping(data)


class _FakeController:
    """Records actions instead of performing them."""

    def __init__(self) -> None:
        self.actions: list[PlannedAction] = []
        self.fail = False

    @property
    def name(self) -> str:
        return "fake"

    def execute(self, action: PlannedAction) -> None:
        self.actions.append(action)
        if self.fail:
            raise ComputerControlError("controller boom")


class _ExplodingController:
    """Stands in for a PyAutoGuiController whose dependency is missing."""

    @property
    def name(self) -> str:
        return "exploding"

    def execute(self, action: PlannedAction) -> None:
        raise ComputerControlError(
            "桌面控制需要可选的 'pyautogui' 依赖（安装：pip install jarvis-assistant[computer]）"
        )


class TestSafetyPolicy:
    def test_disabled_service_is_refused(self) -> None:
        policy = SafetyPolicy(lambda: _section(enabled=False))
        with pytest.raises(ComputerControlError, match=r"computer\.enabled=true"):
            policy.check(PlannedAction(ActionKind.MOVE, {"x": 1, "y": 2}, "移动"))

    def test_mouse_action_is_refused_when_mouse_is_disabled(self) -> None:
        policy = SafetyPolicy(lambda: _section(allow_mouse=False))
        with pytest.raises(DangerousOperationRejectedError, match="allow_mouse"):
            policy.check(PlannedAction(ActionKind.CLICK, {"x": 1, "y": 2}, "点击"))

    def test_keyboard_action_is_refused_when_keyboard_is_disabled(self) -> None:
        policy = SafetyPolicy(lambda: _section(allow_keyboard=False))
        with pytest.raises(DangerousOperationRejectedError, match="allow_keyboard"):
            policy.check(PlannedAction(ActionKind.KEY, {"key": "enter"}, "按键"))

    def test_dangerous_action_needs_confirmation(self) -> None:
        """Typed text can be a password; the gate must default to asking."""
        policy = SafetyPolicy(lambda: _section(confirm_dangerous=True))
        action = PlannedAction(ActionKind.TYPE, {"text": "secret"}, "输入", dangerous=True)
        with pytest.raises(DangerousOperationRejectedError, match="需要用户确认"):
            policy.check(action)

    def test_confirmed_dangerous_action_passes(self) -> None:
        policy = SafetyPolicy(lambda: _section(confirm_dangerous=True))
        action = PlannedAction(ActionKind.TYPE, {"text": "secret"}, "输入", dangerous=True)
        policy.check(action, confirmed=True)

    def test_plain_action_passes_when_allowed(self) -> None:
        policy = SafetyPolicy(lambda: _section())
        policy.check(PlannedAction(ActionKind.MOVE, {"x": 1, "y": 2}, "移动"))


class TestPlannedAction:
    def test_to_dict_is_json_ready(self) -> None:
        action = PlannedAction(ActionKind.MOVE, {"x": 1, "y": 2}, "移动鼠标", dangerous=False)
        assert action.to_dict() == {
            "kind": "move",
            "params": {"x": 1, "y": 2},
            "description": "移动鼠标",
            "dangerous": False,
        }


class TestActionResult:
    def test_to_dict_nests_the_action(self) -> None:
        action = PlannedAction(ActionKind.KEY, {"key": "esc"}, "按下按键 esc")
        result = ActionResult(ok=True, executed=False, detail="（演练模式）", action=action)
        payload = result.to_dict()
        assert payload["ok"] is True
        assert payload["executed"] is False
        assert payload["error"] == ""
        assert payload["action"] == action.to_dict()


class TestComputerService:
    def test_fake_controller_satisfies_the_protocol(self) -> None:
        assert isinstance(_FakeController(), InputController)

    def test_dry_run_does_not_execute(self) -> None:
        """The default configuration must be inert; a regression here would start
        moving a real user's mouse during the test suite."""
        controller = _FakeController()
        service = ComputerService(lambda: _section(dry_run=True), controller=controller)
        service.start()
        result = service.move(10, 20)
        assert result.ok is True
        assert result.executed is False
        assert result.detail.startswith("（演练模式）")
        assert controller.actions == []

    def test_dry_run_covers_every_public_action(self) -> None:
        controller = _FakeController()
        service = ComputerService(lambda: _section(dry_run=True), controller=controller)
        service.start()
        results = [
            service.move(1, 2),
            service.click(3, 4, button="right", double=True),
            service.type_text("密码"),
            service.press_key("enter"),
            service.scroll(-3),
        ]
        assert all(result.executed is False for result in results)
        assert controller.actions == []

    def test_disabled_service_refuses_actions(self) -> None:
        service = ComputerService(lambda: _section(enabled=False), controller=_FakeController())
        service.start()
        with pytest.raises(ComputerControlError, match="未启用"):
            service.move(1, 2)

    def test_mouse_action_is_refused_when_mouse_is_disabled(self) -> None:
        service = ComputerService(
            lambda: _section(allow_mouse=False, dry_run=False), controller=_FakeController()
        )
        service.start()
        with pytest.raises(DangerousOperationRejectedError, match="allow_mouse"):
            service.click(1, 2)

    def test_typing_is_refused_under_the_default_confirmation_policy(self) -> None:
        service = ComputerService(
            lambda: _section(confirm_dangerous=True, dry_run=False), controller=_FakeController()
        )
        service.start()
        with pytest.raises(DangerousOperationRejectedError, match="危险"):
            service.type_text("hunter2")

    def test_action_reaches_the_controller_when_allowed(self) -> None:
        controller = _FakeController()
        service = ComputerService(
            lambda: _section(dry_run=False, allow_mouse=True), controller=controller
        )
        service.start()
        result = service.move(5, 6)
        assert result.ok is True
        assert result.executed is True
        assert [action.kind for action in controller.actions] == [ActionKind.MOVE]

    def test_typing_reaches_the_controller_when_confirmation_is_off(self) -> None:
        controller = _FakeController()
        service = ComputerService(
            lambda: _section(dry_run=False, allow_keyboard=True, confirm_dangerous=False),
            controller=controller,
        )
        service.start()
        assert service.type_text("hello").executed is True
        assert controller.actions[0].params == {"text": "hello"}

    def test_controller_failure_is_reported_not_raised(self) -> None:
        """One failed click must not abort the turn; the caller gets ok=False."""
        controller = _FakeController()
        controller.fail = True
        service = ComputerService(lambda: _section(dry_run=False), controller=controller)
        service.start()
        result = service.move(1, 2)
        assert result.ok is False
        assert result.executed is False
        assert "controller boom" in result.error

    def test_missing_pyautogui_is_reported_as_a_result(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("jarvis.computer.service.PyAutoGuiController", _ExplodingController)
        service = ComputerService(lambda: _section(dry_run=False, allow_mouse=True))
        service.start()
        result = service.move(1, 2)
        assert result.ok is False
        assert "pyautogui" in result.error

    def test_plan_describes_without_acting(self) -> None:
        controller = _FakeController()
        service = ComputerService(lambda: _section(dry_run=True), controller=controller)
        action = service.plan(ActionKind.CLICK, x=1, y=2)
        assert action.kind is ActionKind.CLICK
        assert "单击" in action.description
        assert controller.actions == []

    def test_typed_text_is_never_echoed_in_the_description(self) -> None:
        """The description is logged and shown in the HUD, so a password must not
        appear in it — only a length."""
        service = ComputerService(lambda: _section(), controller=_FakeController())
        action = service.plan(ActionKind.TYPE, text="hunter2")
        assert "hunter2" not in action.description
        assert "7" in action.description
        assert action.dangerous is True

    def test_start_is_idempotent(self) -> None:
        service = ComputerService(lambda: _section(), controller=_FakeController())
        service.start()
        service.start()
        assert service.running is True

    def test_stop_clears_running(self) -> None:
        service = ComputerService(lambda: _section(), controller=_FakeController())
        service.start()
        service.stop()
        assert service.running is False

    def test_stats_shape(self) -> None:
        service = ComputerService(lambda: _section(dry_run=True), controller=_FakeController())
        service.start()
        service.move(1, 2)
        stats = service.stats()
        assert stats["running"] is True
        assert stats["enabled"] is True
        assert stats["dry_run"] is True
        assert stats["planned"] == 1
        assert stats["executed"] == 0
        assert stats["failed"] == 0


class TestPyAutoGuiController:
    def test_missing_pyautogui_names_the_extra(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(importlib, "import_module", _boom_import)
        with pytest.raises(ComputerControlError, match=r"jarvis-assistant\[computer\]"):
            controller_module._load_pyautogui()

    def test_name_is_stable(self) -> None:
        assert PyAutoGuiController().name == "pyautogui"


def _boom_import(name: str) -> Any:
    raise ImportError(name)
