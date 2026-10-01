"""Computer use: mouse/keyboard control of the local desktop.

Responsibility:
    * Mouse move/click/double-click/right-click/drag and keyboard input
      (pyautogui), coordinated with ``vision``/``ocr`` for locate-then-act loops
      by the layers above. Every action passes
      :class:`~jarvis.computer.policy.SafetyPolicy`, and ``computer.dry_run``
      (the default) plans without executing.

``pyautogui`` is optional (the ``computer`` extra) and imported lazily, so a
plain install imports this package for free.

Allowed dependencies: ``core``, ``config``, ``vision``, ``ocr``.
"""

from jarvis.computer.controller import PyAutoGuiController
from jarvis.computer.policy import SafetyPolicy
from jarvis.computer.service import ComputerService
from jarvis.computer.types import ActionKind, ActionResult, InputController, PlannedAction

__all__ = [
    "ActionKind",
    "ActionResult",
    "ComputerService",
    "InputController",
    "PlannedAction",
    "PyAutoGuiController",
    "SafetyPolicy",
]
