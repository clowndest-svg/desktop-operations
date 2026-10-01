"""Computer-control types: the action vocabulary, its plan and its result.

``PlannedAction`` is built *before* anything is touched, so the safety policy can
veto it and the user can be told what would happen. ``ActionResult`` always
carries the action it came from, so a caller never has to correlate a result
with a request by hand.
"""

from __future__ import annotations

import enum
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol, runtime_checkable


class ActionKind(enum.StrEnum):
    """Every pointer/keyboard action this layer can perform."""

    MOVE = "move"
    CLICK = "click"
    DOUBLE_CLICK = "double_click"
    RIGHT_CLICK = "right_click"
    TYPE = "type"
    KEY = "key"
    SCROLL = "scroll"
    DRAG = "drag"


@dataclass(frozen=True, slots=True)
class PlannedAction:
    """A single action, described before it is performed."""

    kind: ActionKind
    params: Mapping[str, object]
    description: str
    """Chinese, user-facing. Never contains typed text (see ``ComputerService``)."""

    dangerous: bool = False
    """True when the action needs explicit confirmation under the policy."""

    def to_dict(self) -> dict[str, object]:
        """JSON-ready form for the UI bridge."""
        return {
            "kind": self.kind.value,
            "params": dict(self.params),
            "description": self.description,
            "dangerous": self.dangerous,
        }


@dataclass(frozen=True, slots=True)
class ActionResult:
    """The outcome of one action."""

    ok: bool
    executed: bool
    """False when ``computer.dry_run`` is on and nothing was actually done."""

    detail: str
    action: PlannedAction
    error: str = ""
    """Non-empty when ``ok`` is False; kept separate for the HUD's error line."""

    def to_dict(self) -> dict[str, object]:
        """JSON-ready form for the UI bridge."""
        return {
            "ok": self.ok,
            "executed": self.executed,
            "detail": self.detail,
            "error": self.error,
            "action": self.action.to_dict(),
        }


@runtime_checkable
class InputController(Protocol):
    """The driver seam: perform one planned action on the real desktop."""

    @property
    def name(self) -> str:
        """Short controller identifier, for logs and ``stats()``."""
        ...

    def execute(self, action: PlannedAction) -> None:
        """Perform ``action``. Raises on failure; the service wraps it."""
        ...
