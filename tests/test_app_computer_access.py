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
        enabled=False, dry_run=True, allow_mouse=False, allow_keyboard=False, confirm_dangerous=True
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
