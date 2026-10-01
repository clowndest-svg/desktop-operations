"""The command line as four named levels, plus the record of what was run.

Same shape as :mod:`jarvis.app.computer_access`, on purpose: an operator asking
"how much of my machine may it touch right now" gets one answer per axis, not a
checklist. Mouse/keyboard and command execution are **separate** axes because they
are different risks — a stray click is undoable, a ``Remove-Item`` is not, and the
reason to be able to say "it may type but it may never run a command" is exactly the
reason not to fold them into one ladder.

Why a level and not the raw ``tools.allow_shell`` switch: that switch is the
installation's answer ("this build may run commands at all"), while this is the
session's answer ("right now, on this machine, by me"). Two switches would be two
places to be wrong, so the level is the only thing the window touches, and the
config file keeps its coarser veto through ``tools.enabled``.

Every run is written to an audit line before it happens and updated after: a
capability this broad that leaves no trace is not something I would turn on either.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import time
from collections import deque
from pathlib import Path
from typing import Any, Final

from jarvis.app.preferences import SHELL_TIER, Preferences

logger = logging.getLogger("jarvis.app.command_access")

OFF: Final[str] = "off"
REHEARSAL: Final[str] = "rehearsal"
USER: Final[str] = "user"
ADMIN: Final[str] = "admin"

MODES: Final[tuple[tuple[str, int, str, str], ...]] = (
    (OFF, 0, "关闭", "模型问不到这条能力，命令行一次都不会碰。"),
    (
        REHEARSAL,
        1,
        "演练",
        "模型可以要一条命令，这里回它「本来会执行什么」，一条都不真跑。",
    ),
    (
        USER,
        2,
        "当前用户",
        "真的执行，权限就是你打开小夜时的那个身份——能读能写你能碰的东西。",
    ),
    (
        ADMIN,
        3,
        "管理员",
        "每条命令都弹一次 UAC，你在弹窗上点「是」它才动。提权进程拿不到管道，"
        "所以输出走结果文件回读，退出码也不保证。",
    ),
)
"""(mode, 档位序号, 标签, 一句人话)。序号就是弹窗里的顺序。"""

DEFAULT_MODE: Final[str] = OFF
"""Off until a person turns it on. Unlike 桌面控制, which defaults to 演练: rehearsal
here still means "the model asked for a command", and the point of the default is
that a fresh install never advertises that it can run things."""

_MAX_AUDIT_ROWS: Final[int] = 200
"""Rows kept in memory for the panel. The file on disk keeps everything."""

_LABELS: Final[dict[str, tuple[str, str]]] = {
    mode: (label, describe) for mode, _number, label, describe in MODES
}


def mode_for_tier(tier: object) -> str:
    """The mode a stored integer stands for; anything unknown is 关闭."""
    for mode, number, _label, _describe in MODES:
        if tier == number:
            return mode
    return OFF


def tier_for_mode(mode: str) -> int:
    for name, number, _label, _describe in MODES:
        if name == mode:
            return number
    return 0


@dataclasses.dataclass(frozen=True)
class CommandRecord:
    """One line of the command audit.

    ``exit_code`` is ``None`` when the run never produced one: a cancelled UAC
    prompt, a timeout, or an elevated run whose status cannot cross the integrity
    boundary. All three are real outcomes and the panel has to be able to say so
    without inventing a zero.
    """

    at: float
    mode: str
    script: str
    exit_code: int | None = None
    seconds: float | None = None
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "at": self.at,
            "mode": self.mode,
            "label": _LABELS.get(self.mode, (self.mode, ""))[0],
            "script": self.script,
            "exit_code": self.exit_code,
            "seconds": self.seconds,
            "note": self.note,
        }


class CommandAccess:
    """The level in force, and the ledger of what ran under it."""

    name = "command_access"

    def __init__(self, prefs: Preferences, audit_file: Path) -> None:
        self._prefs = prefs
        self._audit_file = audit_file
        self._recent: deque[CommandRecord] = deque(maxlen=_MAX_AUDIT_ROWS)

    # -- lifecycle -----------------------------------------------------------

    def start(self) -> None:
        self._load_history()
        logger.warning("command-line level: %s", self.current()["label"])

    def stop(self) -> None:
        return None

    # -- the level -----------------------------------------------------------

    def mode(self) -> str:
        return mode_for_tier(self._prefs.number(SHELL_TIER, default=tier_for_mode(DEFAULT_MODE)))

    def current(self) -> dict[str, Any]:
        mode = self.mode()
        label, describe = _LABELS[mode]
        return {"mode": mode, "tier": tier_for_mode(mode), "label": label, "describe": describe}

    def levels(self) -> dict[str, object]:
        """Everything the popup needs: the levels, and the one in force."""
        active = self.mode()
        return {
            "error": "",
            "current": self.current(),
            "levels": [
                {
                    "mode": mode,
                    "tier": number,
                    "label": label,
                    "describe": describe,
                    "current": mode == active,
                }
                for mode, number, label, describe in MODES
            ],
            "commands": [entry.to_dict() for entry in reversed(self._recent)],
        }

    def set_mode(self, value: object) -> dict[str, object]:
        """Move between levels. Refuses anything that is not a known number."""
        if isinstance(value, bool) or not isinstance(value, (int, str)):
            return {**self.levels(), "error": f"档位不是数字：{value!r}"}
        try:
            tier = int(value)
        except (TypeError, ValueError):
            return {**self.levels(), "error": f"档位不是数字：{value!r}"}
        wanted = next((mode for mode, number, _l, _d in MODES if number == tier), "")
        if not wanted:
            return {**self.levels(), "error": f"没有这一档：{tier}"}
        self._prefs.set(SHELL_TIER, tier)
        logger.warning("command-line level set to %s (%s)", wanted, _LABELS[wanted][0])
        return {"error": "", **self.levels()}

    # -- the ledger ----------------------------------------------------------

    def record(self, entry: CommandRecord) -> None:
        """Append one run to the file and to the in-memory tail the panel reads.

        The write happens *before* the command runs (the caller fills in the rest
        and calls :meth:`complete`), because the line that matters most is the one
        that proves what was attempted on a command that never came back.
        """
        self._recent.append(entry)
        self._write(entry)

    def audit(
        self,
        *,
        mode: str,
        script: str,
        exit_code: int | None,
        seconds: float | None,
        note: str,
    ) -> None:
        """The gate the tool layer calls. Field-for-field the same, without the tool
        layer having to import a class from this one.
        """
        self.record(
            CommandRecord(
                at=now(),
                mode=mode,
                script=script,
                exit_code=exit_code,
                seconds=seconds,
                note=note,
            ),
        )

    def _write(self, entry: CommandRecord) -> None:
        try:
            self._audit_file.parent.mkdir(parents=True, exist_ok=True)
            with self._audit_file.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry.to_dict(), ensure_ascii=False) + "\n")
        except OSError as exc:  # pragma: no cover - a full or locked data dir
            logger.error("命令审计写不下去：%s", exc)

    def _load_history(self) -> None:
        """Re-read the tail of the audit file so the panel survives a restart."""
        try:
            lines = self._audit_file.read_text(encoding="utf-8").splitlines()[-_MAX_AUDIT_ROWS:]
        except OSError:
            return
        for line in lines:
            try:
                raw = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(raw, dict):
                continue
            self._recent.append(
                CommandRecord(
                    at=float(raw.get("at") or 0),
                    mode=str(raw.get("mode") or ""),
                    script=str(raw.get("script") or ""),
                    exit_code=_as_int(raw.get("exit_code")),
                    seconds=_as_float(raw.get("seconds")),
                    note=str(raw.get("note") or ""),
                ),
            )

    def recent(self) -> list[dict[str, Any]]:
        return [entry.to_dict() for entry in reversed(self._recent)]


def _as_int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _as_float(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    return float(value) if isinstance(value, (int, float)) else None


def now() -> float:
    """One place for the audit clock, so a test can patch it."""
    return time.time()


__all__ = [
    "ADMIN",
    "DEFAULT_MODE",
    "OFF",
    "REHEARSAL",
    "USER",
    "CommandAccess",
    "CommandRecord",
    "mode_for_tier",
    "tier_for_mode",
]
