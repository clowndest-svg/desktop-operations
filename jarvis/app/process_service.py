"""Process service: the application-layer door onto the confirmed-kill tool.

Same shape as :mod:`jarvis.app.disk_service`, for the same reason: the page may not
reach into L2 (architecture rule 6), and the entries it sends back are the rows a
human actually saw. Anything that is not one of those rows is dropped here, before
the controller ever looks at it.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from jarvis.core.exceptions import JarvisError
from jarvis.tools.process_control import ProcessControlError, ProcessTarget

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Callable, Sequence

    from jarvis.tools.process_control import KillOutcome

logger = logging.getLogger("jarvis.app.process_service")


class ProcessService:
    """Turns the page's ticked rows into a guarded, audited kill."""

    name = "process_service"

    def __init__(self, controller_factory: Callable[[], Any | None]) -> None:
        self._controller_factory = controller_factory

    def start(self) -> None:
        return None

    def stop(self) -> None:
        return None

    def kill(self, items: Sequence[object], *, confirmed: bool) -> dict[str, Any]:
        """End exactly what was ticked, if the operator confirmed twice.

        The reply is always a shape the page can render -- ``error`` says why when
        there is nothing to render. A raise here would reach the page as an opaque
        pywebview failure, which is the worst place to lose a "no".
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
        return {
            "results": [entry.to_dict() for entry in outcomes],
            "ended": ended,
            "error": "",
        }


__all__ = ["ProcessService"]
