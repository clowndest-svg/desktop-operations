"""Ending a process: the same two doors as a delete, on a shorter fuse.

``disk_cleaner`` already insists that a destructive act needs an explicit human
confirmation and that the protections are re-checked at the moment of the act, not
when the list was drawn. That discipline matters *more* here: a deleted file can
sometimes be recovered, a killed process usually cannot, and the thing being killed
may have changed identity in the meantime.

So three rules are structural rather than advisory:

* **A PID is not an identity.** Between the page drawing the row and the operator
  clicking 确认, a process can exit and Windows can hand the same number to something
  else -- possibly ``lsass.exe``. Every target therefore carries the *name it was
  shown with*, and the name is re-read at act time; a mismatch is a refusal, not a
  warning.
* **Some names cannot be touched at all**, whatever the UI sent: the processes whose
  death takes the session or the machine with it, and the assistant's own process
  tree. An assistant that can kill its own window cannot tell you it did.
* **``confirmed`` comes from a button, never from a model.** This module is reachable
  from the desktop bridge only; there is deliberately no tool registered for it,
  because "the model may end any process it can name" is a different capability from
  "a person ticked a row they could see".
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from jarvis.core.exceptions import JarvisError

logger = logging.getLogger("jarvis.tools.process_control")

OS_CRITICAL: Final[frozenset[str]] = frozenset(
    {
        "system",
        "registry",
        "smss.exe",
        "csrss.exe",
        "wininit.exe",
        "services.exe",
        "lsass.exe",
        "svchost.exe",
        "winlogon.exe",
        "fontdrvhost.exe",
        "dwm.exe",
        "memory compression",
        "memcompression",
        "taskhostw.exe",
        "userinit.exe",
        "spoolsv.exe",
    }
)
"""Names whose death is not the operator's to cause by accident.

``svchost.exe`` is in here on purpose: one host can carry the network or audio
service, and a person who genuinely needs to recycle a service will do it through
``services.msc`` or an elevated shell, where they can see what they are restarting.
"""

OWN_NAMES: Final[frozenset[str]] = frozenset({"小夜.exe", "pythonw.exe", "msedgewebview2.exe"})
"""The assistant's own parts, by name as well as by tree.

The tree check below catches them anyway; the names are a second door, because the
WebView2 processes that render this window are children of a *different* root and
can outlive a restart.
"""

_GRACE_SECONDS: Final[float] = 3.0
"""How long to wait after ``terminate`` before forcing the issue."""


@dataclass(frozen=True, slots=True)
class ProcessTarget:
    """One row the operator ticked: a number *and* the name it was shown under."""

    pid: int
    name: str

    @staticmethod
    def from_wire(raw: object) -> ProcessTarget | None:
        if not isinstance(raw, dict):
            return None
        pid = raw.get("pid")
        name = raw.get("name")
        if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
            return None
        if not isinstance(name, str) or not name.strip():
            return None
        return ProcessTarget(pid=pid, name=name.strip())

    def to_dict(self) -> dict[str, object]:
        return {"pid": self.pid, "name": self.name}


@dataclass(frozen=True, slots=True)
class KillOutcome:
    """What happened to one target. ``ok`` is False with the reason attached."""

    pid: int
    name: str
    ok: bool
    note: str = ""

    def to_dict(self) -> dict[str, object]:
        return {"pid": self.pid, "name": self.name, "ok": self.ok, "note": self.note}


class ProcessControlError(JarvisError):
    """The whole request was refused (nothing was confirmed, nothing was listed)."""


def _lower(name: str) -> str:
    return name.strip().lower()


class ProcessController:
    """Ends confirmed processes, refusing anything the rules below object to."""

    def __init__(
        self,
        *,
        psutil_module: Any | None = None,
        audit_log: Path,
        own_pids: Iterable[int] | None = None,
    ) -> None:
        self._ps = psutil_module
        self._audit_log = audit_log
        self._own_pids = frozenset(own_pids) if own_pids is not None else None

    # -- the guards ----------------------------------------------------------

    def _own_tree(self) -> set[int]:
        """This process, its parents, and everything it started.

        Computed at act time rather than cached: the WebView2 renderer for the window
        is created after the assistant boots, and a cached set would be exactly as
        stale as the moment the assistant needed it least.
        """
        if self._own_pids is not None:
            return set(self._own_pids)
        psutil = self._module()
        pids = {os.getpid()}
        try:
            current = psutil.Process(os.getpid())
            pids.update(ancestor.pid for ancestor in current.parents())
            pids.update(child.pid for child in current.children(recursive=True))
        except Exception as exc:  # pragma: no cover - a vanished ancestor
            logger.debug("could not map the assistant's own process tree: %r", exc)
        return pids

    def _module(self) -> Any:
        if self._ps is not None:
            return self._ps
        import psutil  # imported late: the desktop build always has it, tests do not need it

        self._ps = psutil
        return psutil

    def refusal(self, target: ProcessTarget) -> str:
        """Why this target may not be ended, or ``""`` when it may."""
        if _lower(target.name) in OS_CRITICAL:
            return f"{target.name} 是系统关键进程，不能从这里结束"
        if _lower(target.name) in OWN_NAMES:
            return f"{target.name} 是小夜自己（或它的窗口），结束了就没人回答你了"
        if target.pid in self._own_tree():
            return f"PID {target.pid} 属于小夜自己的进程树"
        return ""

    def real_name(self, pid: int) -> str | None:
        """What that number is running right now, or ``None`` if nothing is.

        Exposed for the *proposal* step, where a name the model got wrong is cheap to
        catch. It is not a substitute for the re-read inside :meth:`_kill_one`: a pid
        can be handed to a different process between the two calls, and only the second
        check happens next to the act.
        """
        try:
            return str(self._module().Process(pid).name())
        except Exception:
            return None

    # -- the act -------------------------------------------------------------

    def kill(self, targets: Sequence[ProcessTarget], *, confirmed: bool) -> list[KillOutcome]:
        """End each confirmed target, re-checking the guards and the name first.

        Raises:
            ProcessControlError: nothing was confirmed. A request that arrives
                unconfirmed is not a partial success -- it is the button having been
                pressed once instead of twice, and the answer is to say so.
        """
        if not confirmed:
            raise ProcessControlError("结束进程需要二次确认：勾好后点「确认结束」。")
        psutil = self._module()
        outcomes: list[KillOutcome] = []
        for target in targets:
            blocked = self.refusal(target)
            if blocked:
                outcomes.append(KillOutcome(target.pid, target.name, False, blocked))
                self._audit(target, False, blocked)
                continue
            outcomes.append(self._kill_one(psutil, target))
        return outcomes

    def _kill_one(self, psutil: Any, target: ProcessTarget) -> KillOutcome:
        try:
            process = psutil.Process(target.pid)
        except Exception:  # NoSuchProcess, or a pid the OS will not talk about
            note = "进程已经不在了"
            self._audit(target, False, note)
            return KillOutcome(target.pid, target.name, False, note)

        # The identity check, at the only moment it means anything.
        try:
            actual = str(process.name())
        except Exception:
            actual = ""
        if actual and _lower(actual) != _lower(target.name):
            note = f"PID {target.pid} 已被复用（现在是 {actual}），没有动它"
            self._audit(target, False, note)
            return KillOutcome(target.pid, target.name, False, note)

        try:
            process.terminate()
            try:
                process.wait(timeout=_GRACE_SECONDS)
            except Exception:
                # Still there after the grace period: Windows has no polite second
                # door, so this is the forceful one.
                process.kill()
        except psutil.AccessDenied:
            note = "权限不够：这个进程属于别人或以管理员运行，小夜这里结束不了"
            self._audit(target, False, note)
            return KillOutcome(target.pid, target.name, False, note)
        except Exception as exc:
            note = f"结束失败：{type(exc).__name__}"
            self._audit(target, False, note)
            return KillOutcome(target.pid, target.name, False, note)

        self._audit(target, True, "已结束")
        logger.warning("ended process pid=%d name=%s", target.pid, target.name)
        return KillOutcome(target.pid, target.name, True, "已结束")

    # -- the record ----------------------------------------------------------

    def _audit(self, target: ProcessTarget, ok: bool, note: str) -> None:
        """One line per attempt, including the refused ones.

        The refusal is the interesting row: "I asked and it said no" is what a person
        looks for afterwards when the machine starts behaving oddly.
        """
        row = {
            "at": time.time(),
            "pid": target.pid,
            "name": target.name,
            "ok": ok,
            "note": note,
        }
        try:
            self._audit_log.parent.mkdir(parents=True, exist_ok=True)
            with self._audit_log.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
        except OSError as exc:  # pragma: no cover - a full or locked data dir
            logger.error("could not write the process audit to %s: %s", self._audit_log, exc)


__all__ = [
    "OS_CRITICAL",
    "OWN_NAMES",
    "KillOutcome",
    "ProcessControlError",
    "ProcessController",
    "ProcessTarget",
]
