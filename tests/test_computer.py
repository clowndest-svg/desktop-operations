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
        "allow_typing": True,
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

    def test_typing_needs_its_own_switch_not_the_confirmed_flag(self) -> None:
        """打字那把钥匙是 computer.allow_typing，不是 `confirmed`。

        旧规则把它当"危险动作 → 需要 confirmed"，而工具层明确不让模型传那个参数
        （能自己按确认的闸就是摆设），两条合起来等于永久关闭 —— 用户把档位开到「键鼠全开」
        说"发句你好"，打字仍被拒，而界面上没有任何开关能开。现在开关就是那把钥匙。
        """
        policy = SafetyPolicy(lambda: _section(allow_typing=False, confirm_dangerous=True))
        action = PlannedAction(ActionKind.TYPE, {"text": "secret"}, "输入", dangerous=True)
        with pytest.raises(DangerousOperationRejectedError, match="允许打字"):
            policy.check(action)

    def test_the_typing_switch_is_the_consent_however_confirm_is_set(self) -> None:
        """打开开关 = 操作者本人对"她会往别人输入框里写字"的明确同意。

        ``confirmed=False``（工具层永远传不了别的）也应当放行，否则那把开关就成了摆设。
        """
        policy = SafetyPolicy(lambda: _section(allow_typing=True, confirm_dangerous=True))
        action = PlannedAction(ActionKind.TYPE, {"text": "secret"}, "输入", dangerous=True)
        policy.check(action)

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

    def test_typing_is_refused_until_the_typing_switch_is_on(self) -> None:
        """默认关着，且拒绝理由必须点名那把开关（否则用户不知道去哪儿打开）。"""
        service = ComputerService(
            lambda: _section(allow_typing=False, dry_run=False), controller=_FakeController()
        )
        service.start()
        with pytest.raises(DangerousOperationRejectedError, match="允许打字"):
            service.type_text("hunter2")

    def test_typing_works_once_the_switch_is_on(self) -> None:
        controller = _FakeController()
        service = ComputerService(
            lambda: _section(allow_typing=True, dry_run=False), controller=controller
        )
        service.start()
        result = service.type_text("hunter2")
        assert result.ok is True and result.executed is True
        assert [action.kind for action in controller.actions] == [ActionKind.TYPE]

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


class TestLaunching:
    """把程序叫起来：它是少数"不需要看见屏幕"也能做成的事。

    走的是同一个 Service（计划 → 策略 → 执行），所以演练档照样只报告、关档照样拒绝；
    真正的启动动作在 controller 里，测试用假 controller 记账。
    """

    def test_launch_is_planned_with_the_resolved_path(self) -> None:
        controller = _FakeController()
        service = ComputerService(lambda: _section(dry_run=True), controller=controller)
        service.start()

        result = service.launch(r"D:\RuanJian\微信\Weixin\Weixin.exe", label="微信")

        assert result.executed is False, "演练档不真的启动"
        assert "微信" in result.detail and "Weixin.exe" in result.detail
        assert controller.actions == []

    def test_a_real_launch_reaches_the_controller(self) -> None:
        controller = _FakeController()
        service = ComputerService(lambda: _section(dry_run=False), controller=controller)
        service.start()

        result = service.launch(r"C:\Windows\System32\calc.exe", label="计算器")

        assert result.ok is True and result.executed is True
        assert [action.kind for action in controller.actions] == [ActionKind.LAUNCH]
        assert str(controller.actions[0].params["target"]).endswith("calc.exe")

    def test_an_already_running_app_plans_to_bring_its_window_forward(self) -> None:
        controller = _FakeController()
        service = ComputerService(lambda: _section(dry_run=True), controller=controller)
        service.start()

        result = service.launch("", label="微信", pid=13532)

        assert "最前面" in result.detail

    def test_launching_needs_no_mouse_or_keyboard_permission(self) -> None:
        """键盘档（有键盘、没鼠标）也应当能启动程序 —— 这不是点击。"""
        controller = _FakeController()
        service = ComputerService(
            lambda: _section(dry_run=False, allow_mouse=False, allow_keyboard=False),
            controller=controller,
        )
        service.start()

        assert service.launch(r"C:\Windows\System32\notepad.exe", label="记事本").ok is True

    def test_find_apps_is_a_passthrough_to_the_launcher(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """查找是只读的，所以不经过策略；这条钉住"查找不该被档位挡住"。"""
        import jarvis.computer.service as service_module

        seen: list[str] = []

        def fake(query: str) -> tuple[tuple[object, ...], tuple[str, ...]]:
            seen.append(query)
            return ((), ("开始菜单",))

        monkeypatch.setattr(service_module, "find_apps", fake)
        service = ComputerService(lambda: _section(enabled=False), controller=_FakeController())

        assert service.find_apps("微信") == ((), ("开始菜单",))
        assert seen == ["微信"]
