"""Desktop control as four named levels, not four checkboxes.

The raw switches (``enabled`` / ``dry_run`` / ``allow_mouse`` / ``allow_keyboard``)
are honest but combinatorial: sixteen combinations, of which maybe four are things
a person actually means. A level answers the question an operator has -- "how much
of my machine may it touch right now" -- and each level maps to exactly one
combination, so the label on the button and the policy in force cannot drift.

The choice lives in preferences, not in the config file, for the same reason the
model picker does: it is a decision made from the window, and the config file is
what the operator edits with a text editor.
"""

from __future__ import annotations

import dataclasses
import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Final

from jarvis.app.preferences import COMPUTER_TIER, Preferences

if TYPE_CHECKING:  # pragma: no cover - typing only
    from jarvis.config.schema import ComputerSection

logger = logging.getLogger("jarvis.app.computer_access")

OFF: Final[int] = 0
REHEARSAL: Final[int] = 1
KEYBOARD: Final[int] = 2
FULL: Final[int] = 3

TIERS: Final[tuple[tuple[int, str, str, dict[str, bool]], ...]] = (
    (OFF, "关闭", "工具一条都不注册，模型连问都不能问。", {"enabled": False}),
    (
        REHEARSAL,
        "演练",
        "模型能规划动作并看到结果，但指针和键盘一下都不动。",
        {"enabled": True, "dry_run": True, "allow_mouse": False, "allow_keyboard": False},
    ),
    (
        KEYBOARD,
        "键盘",
        "真的按键和输入文字；指针仍然不动。够开开始菜单、填表、回车。",
        {"enabled": True, "dry_run": False, "allow_mouse": False, "allow_keyboard": True},
    ),
    (
        FULL,
        "键鼠全开",
        "指针和键盘都真的动。这一档能替你点「发送」，请想清楚再开。",
        {"enabled": True, "dry_run": False, "allow_mouse": True, "allow_keyboard": True},
    ),
)
"""id, 标签, 一句人话, 以及它对应的开关组合。顺序就是弹窗里的顺序。"""

DEFAULT_TIER: Final[int] = REHEARSAL


def section_for_tier(base: ComputerSection, tier: int) -> ComputerSection:
    """The config section a level stands for. Unknown levels fall back to 演练.

    Falling back *up* to rehearsal rather than down to off would be wrong in the
    scary direction; falling back to off would silently disable a machine the
    operator had opened. Rehearsal is the one level that is both useful and
    incapable of touching anything.
    """
    flags = dict(next((entry for entry in TIERS if entry[0] == tier), TIERS[REHEARSAL])[3])
    return dataclasses.replace(base, **flags)


class ComputerAccess:
    """Reads and writes the current level, and derives the section from it."""

    name = "computer_access"

    def __init__(self, prefs: Preferences, section_provider: Callable[[], ComputerSection]) -> None:
        self._prefs = prefs
        self._base = section_provider

    def start(self) -> None:
        logger.info("desktop control level: %s", self.current()["label"])

    def stop(self) -> None:
        return None

    def tier(self) -> int:
        value = self._prefs.number(COMPUTER_TIER, default=DEFAULT_TIER)
        return value if any(entry[0] == value for entry in TIERS) else DEFAULT_TIER

    def section(self) -> ComputerSection:
        """What :class:`~jarvis.computer.service.ComputerService` should enforce."""
        return section_for_tier(self._base(), self.tier())

    def current(self) -> dict[str, object]:
        tier = self.tier()
        entry = next(item for item in TIERS if item[0] == tier)
        section = self.section()
        return {
            "tier": tier,
            "label": entry[1],
            "describe": entry[2],
            "enabled": section.enabled,
            "dry_run": section.dry_run,
            "allow_mouse": section.allow_mouse,
            "allow_keyboard": section.allow_keyboard,
        }

    def levels(self) -> dict[str, object]:
        return {
            "error": "",
            "current": self.current(),
            "levels": [
                {"tier": tier, "label": label, "describe": describe, "current": tier == self.tier()}
                for tier, label, describe, _flags in TIERS
            ],
        }

    def set_tier(self, value: object) -> dict[str, object]:
        raw = value if isinstance(value, (int, str)) and not isinstance(value, bool) else None
        if raw is None:
            return {**self.levels(), "error": f"档位不是数字：{value!r}"}
        try:
            tier = int(raw)
        except ValueError:
            return {**self.levels(), "error": f"档位不是数字：{value!r}"}
        if not any(entry[0] == tier for entry in TIERS):
            return {**self.levels(), "error": f"没有这一档：{tier}"}
        self._prefs.set(COMPUTER_TIER, tier)
        logger.warning("desktop control level set to %d (%s)", tier, self.current()["label"])
        return {"error": "", **self.levels()}


__all__ = ["DEFAULT_TIER", "TIERS", "ComputerAccess", "section_for_tier"]
