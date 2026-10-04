"""Process service: the application-layer door onto the confirmed-kill tool.

Same shape as :mod:`jarvis.app.disk_service`, for the same reason: the page may not
reach into L2 (architecture rule 6), and the entries it sends back are the rows a
human actually saw. Anything that is not one of those rows is dropped here, before
the controller ever looks at it.

The proposal queue is the second door the assistant's side walks through. A model may
say "end notepad.exe"; it may not end it. :meth:`propose` checks the guards, puts the
request in a short bounded list the panel renders, and the existing
:meth:`kill` runs only when a person presses 确认. Nothing here ever kills on a
proposal -- and a proposal that is never confirmed simply ages out of the queue.
"""

from __future__ import annotations

import logging
import time
from collections import deque
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from jarvis.core.exceptions import JarvisError
from jarvis.tools.process_control import ProcessControlError, ProcessTarget

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Callable, Sequence

    from jarvis.tools.process_control import KillOutcome

logger = logging.getLogger("jarvis.app.process_service")

MAX_PROPOSALS = 8
"""How many unanswered suggestions the queue holds.

Bounded because this is a prompt a person has to read, not an inbox. When it is full
the oldest one drops -- the assistant can always ask again, and a queue that grows is
how a confirmation strip turns into noise nobody looks at.
"""

PROPOSAL_TTL_SECONDS = 600.0
"""How long a proposal stays offerable. Ten minutes: long enough to come back from
another window, short enough that a pid is still likely to be the same process."""


@dataclass(frozen=True, slots=True)
class ProcessProposal:
    """One thing the assistant asked to end, waiting on a person."""

    pid: int
    name: str
    reason: str
    asked_at: float

    def to_dict(self) -> dict[str, object]:
        return {
            "pid": self.pid,
            "name": self.name,
            "reason": self.reason,
            "asked_at": self.asked_at,
            "age_seconds": round(max(0.0, time.time() - self.asked_at), 1),
        }


class ProcessService:
    """Turns the page's ticked rows into a guarded, audited kill."""

    name = "process_service"

    def __init__(self, controller_factory: Callable[[], Any | None]) -> None:
        self._controller_factory = controller_factory
        self._proposals: deque[ProcessProposal] = deque(maxlen=MAX_PROPOSALS)

    def start(self) -> None:
        return None

    def stop(self) -> None:
        return None

    # -- the assistant's side ------------------------------------------------

    def propose(self, pid: int, name: str, reason: str = "") -> dict[str, Any]:
        """Take the model's request, check it, and queue it for a human.

        Never ends anything. Returns the reason it was refused, so she can say why to
        the operator instead of reporting a request that was never made.
        """
        controller = self._controller_factory()
        if controller is None:
            return {"ok": False, "error": "进程服务未启动", "proposals": len(self._proposals)}
        target = ProcessTarget(pid=pid, name=name.strip())
        if not target.name:
            return {
                "ok": False,
                "error": "要给进程名，不能只给号",
                "proposals": len(self._proposals),
            }
        actual = controller.real_name(pid)
        if actual is None:
            return {
                "ok": False,
                "error": f"没有 PID {pid} 这个进程，或者它刚退出了",
                "proposals": len(self._proposals),
            }
        if actual.strip().lower() != target.name.lower():
            return {
                "ok": False,
                "error": f"PID {pid} 现在跑的是 {actual}，不是 {target.name}，没有替你说这个",
                "proposals": len(self._proposals),
            }
        blocked = controller.refusal(ProcessTarget(pid=pid, name=actual))
        if blocked:
            logger.info("process proposal refused by the guard: pid=%d %s", pid, blocked)
            return {"ok": False, "error": blocked, "proposals": len(self._proposals)}
        self._prune()
        self._proposals.append(
            ProcessProposal(pid=pid, name=actual, reason=reason.strip(), asked_at=time.time())
        )
        logger.info("assistant proposed ending pid=%d %s; waiting on a human", pid, actual)
        return {
            "ok": True,
            "error": "",
            "proposals": len(self._proposals),
            "name": actual,
        }

    def proposals(self) -> list[dict[str, object]]:
        """What the panel should be offering, newest last."""
        self._prune()
        return [entry.to_dict() for entry in list(self._proposals)]

    def dismiss(self, pid: int) -> bool:
        """Drop one proposal without ending anything: 「不用了」 is an answer too."""
        before = len(self._proposals)
        self._proposals = deque(
            (entry for entry in self._proposals if entry.pid != pid), maxlen=MAX_PROPOSALS
        )
        return len(self._proposals) != before

    def _prune(self) -> None:
        """Forget proposals that have stopped being about a live, recognisable process."""
        cutoff = time.time() - PROPOSAL_TTL_SECONDS
        kept = [entry for entry in self._proposals if entry.asked_at >= cutoff]
        if len(kept) != len(self._proposals):
            self._proposals = deque(kept, maxlen=MAX_PROPOSALS)

    # -- the human's side ----------------------------------------------------

    def kill(self, items: Sequence[object], *, confirmed: bool) -> dict[str, Any]:
        """End exactly what was ticked, if the operator confirmed twice.

        The reply is always a shape the page can render -- ``error`` says why when
        there is nothing to render. A raise here would reach the page as an opaque
        pywebview failure, which is the worst place to lose a "no".

        A pid that a proposal is waiting on is dropped from the queue either way: the
        person just answered the question, and leaving the strip up invites a second
        kill of whatever number that process used to hold.
        """
        controller = self._controller_factory()
        if controller is None:
            return {"results": [], "ended": 0, "error": "进程服务未启动"}
        approved = []
        malformed = 0
        for raw in items:
            target = ProcessTarget.from_wire(raw)
            if target is None:
                malformed += 1
                continue
            approved.append(target)
        if malformed:
            logger.error("process kill rejected %d malformed entry(ies)", malformed)
        if not approved:
            return {"results": [], "ended": 0, "error": "没有收到有效的进程条目"}
        try:
            outcomes: Sequence[KillOutcome] = controller.kill(approved, confirmed=confirmed)
        except (ProcessControlError, JarvisError) as exc:
            logger.warning("process kill refused: %s", exc)
            return {"results": [], "ended": 0, "error": str(exc)}
        ended = sum(1 for entry in outcomes if entry.ok)
        for entry in outcomes:
            self.dismiss(entry.pid)
        return {
            "results": [entry.to_dict() for entry in outcomes],
            "ended": ended,
            "error": "",
        }


__all__ = ["MAX_PROPOSALS", "ProcessProposal", "ProcessService"]
