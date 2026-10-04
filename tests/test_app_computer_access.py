"""Four named levels over four boolean switches.

What these pin is the mapping, because the mapping *is* the feature: if 「键盘」
ever came out with ``allow_mouse=True``, the label on the dialog and the policy in
force would be two different facts, and the dialog would be lying in the dangerous
direction.
"""

from __future__ import annotations

from typing import Any, cast

from jarvis.app.computer_access import FULL, KEYBOARD, OFF, REHEARSAL, ComputerAccess
from jarvis.app.preferences import COMPUTER_TIER, Preferences
from jarvis.config.schema import ComputerSection


def _base() -> ComputerSection:
    return ComputerSection(
        enabled=False,
        dry_run=True,
        allow_mouse=False,
        allow_keyboard=False,
        allow_typing=False,
        confirm_dangerous=True,
    )


def _access(tmp_path: Any) -> tuple[ComputerAccess, Preferences]:
    prefs = Preferences(tmp_path / "prefs.json")
    return ComputerAccess(prefs, _base), prefs


class TestLevelMapping:
    def test_off_registers_nothing(self, tmp_path: Any) -> None:
        access, prefs = _access(tmp_path)
        prefs.set(COMPUTER_TIER, OFF)

        section = access.section()

        assert section.enabled is False

    def test_rehearsal_plans_but_touches_nothing(self, tmp_path: Any) -> None:
        access, prefs = _access(tmp_path)
        prefs.set(COMPUTER_TIER, REHEARSAL)

        section = access.section()

        assert (section.enabled, section.dry_run, section.allow_mouse, section.allow_keyboard) == (
            True,
            True,
            False,
            False,
        )

    def test_keyboard_moves_keys_but_not_the_pointer(self, tmp_path: Any) -> None:
        access, prefs = _access(tmp_path)
        prefs.set(COMPUTER_TIER, KEYBOARD)

        section = access.section()

        assert (section.dry_run, section.allow_keyboard, section.allow_mouse) == (
            False,
            True,
            False,
        )

    def test_full_opens_both_and_stops_rehearsing(self, tmp_path: Any) -> None:
        access, prefs = _access(tmp_path)
        prefs.set(COMPUTER_TIER, FULL)

        section = access.section()

        assert (section.dry_run, section.allow_keyboard, section.allow_mouse) == (
            False,
            True,
            True,
        )

    def test_an_unknown_level_falls_back_to_rehearsal_not_to_off(self, tmp_path: Any) -> None:
        access, prefs = _access(tmp_path)
        prefs.set(COMPUTER_TIER, 99)

        section = access.section()

        assert section.enabled is True
        assert section.dry_run is True

    def test_the_base_config_never_leaks_into_a_higher_level(self, tmp_path: Any) -> None:
        """A config file that opens the mouse must not outrank a window that closed it."""
        prefs = Preferences(tmp_path / "prefs.json")
        prefs.set(COMPUTER_TIER, REHEARSAL)
        opened = ComputerSection(
            enabled=True,
            dry_run=False,
            allow_mouse=True,
            allow_keyboard=True,
            allow_typing=True,
            confirm_dangerous=True,
        )
        access = ComputerAccess(prefs, lambda: opened)

        section = access.section()

        assert section.allow_mouse is False
        assert section.dry_run is True


class TestChoosing:
    def test_setting_a_level_is_remembered_and_reported(self, tmp_path: Any) -> None:
        access, prefs = _access(tmp_path)

        listing = access.set_tier(KEYBOARD)

        assert listing["error"] == ""
        assert prefs.number(COMPUTER_TIER) == KEYBOARD
        assert access.current()["label"] == "键盘"
        levels = cast("list[dict[str, Any]]", listing["levels"])
        marked = [entry for entry in levels if entry["current"]]
        assert [entry["tier"] for entry in marked] == [KEYBOARD]

    def test_a_level_that_does_not_exist_is_refused(self, tmp_path: Any) -> None:
        access, _ = _access(tmp_path)

        listing = access.set_tier(7)

        assert listing["error"]
        assert access.tier() == REHEARSAL

    def test_a_non_numeric_level_is_refused_without_raising(self, tmp_path: Any) -> None:
        access, _ = _access(tmp_path)

        assert access.set_tier("全开")["error"]

    def test_the_default_before_anyone_chooses_is_rehearsal(self, tmp_path: Any) -> None:
        access, _ = _access(tmp_path)

        assert access.tier() == REHEARSAL


class TestTheTypingSwitch:
    """打字的钥匙跟档位分开，因为风险不是一件事。

    实测撞到的：档位开到「键鼠全开」，说"给老妈发句你好"，打字仍被拒 —— 而且没有任何
    界面开关能打开它（旧规则要 `confirmed=true`，而工具层明确不让模型传那个参数）。
    现在这把钥匙是 `computer.allow_typing`，默认关，独立于档位。
    """

    def test_off_by_default_even_at_the_widest_tier(self, tmp_path: Any) -> None:
        access, prefs = _access(tmp_path)
        prefs.set(COMPUTER_TIER, FULL)

        section = access.section()

        assert section.allow_mouse is True and section.allow_keyboard is True
        assert section.allow_typing is False, "开鼠标键盘不等于同意往别人输入框里写字"

    def test_turning_it_on_survives_a_tier_change(self, tmp_path: Any) -> None:
        access, _prefs = _access(tmp_path)

        access.set_typing(True)
        assert access.section().allow_typing is True

        access.set_tier(REHEARSAL)
        assert access.section().allow_typing is True, "档位不该顺手把打字关掉"
        assert access.section().dry_run is True

    def test_levels_reports_it_so_the_panel_can_draw_it(self, tmp_path: Any) -> None:
        access, _prefs = _access(tmp_path)

        assert access.levels()["current"]["allow_typing"] is False  # type: ignore[index]
        access.set_typing(True)
        assert access.levels()["current"]["allow_typing"] is True  # type: ignore[index]

    def test_a_non_boolean_is_refused_without_writing(self, tmp_path: Any) -> None:
        access, _prefs = _access(tmp_path)

        answer = access.set_typing("yes")

        assert "只认 true/false" in str(answer["error"])
        assert access.section().allow_typing is False

    def test_the_base_config_can_only_open_it_explicitly(self, tmp_path: Any) -> None:
        """配置文件里写 allow_typing: true 是操作者自己的选择，preferences 覆盖它。"""
        prefs = Preferences(tmp_path / "prefs.json")
        prefs.set(COMPUTER_TIER, FULL)

        opened = ComputerSection(
            enabled=True,
            dry_run=False,
            allow_mouse=True,
            allow_keyboard=True,
            allow_typing=True,
            confirm_dangerous=True,
        )
        access = ComputerAccess(prefs, lambda: opened)

        assert access.section().allow_typing is True

        access.set_typing(False)
        assert access.section().allow_typing is False, "界面关掉要能压住配置文件"
