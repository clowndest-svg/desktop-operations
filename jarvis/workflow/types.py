"""Domain types for the workflow engine.

These are the shapes a YAML definition is parsed into and the shapes an
execution produces. They are frozen because a workflow definition is a document
the user edits, not mutable state: the running copy must not change under a
step that is still reading it.
"""

from __future__ import annotations

import enum
from collections.abc import Mapping
from dataclasses import dataclass


class TriggerKind(enum.StrEnum):
    """What starts a workflow.

    ``MANUAL`` is the default (run it from the UI or by name); ``CRON`` hands the
    workflow to the scheduler; ``EVENT`` is reserved for the event bus and is
    parsed but not yet wired, so a definition using it loads without failing.
    """

    MANUAL = "manual"
    CRON = "cron"
    EVENT = "event"


@dataclass(frozen=True, slots=True)
class StepSpec:
    """One step of a workflow: an action, its arguments and a guard."""

    name: str
    """Step name, unique within the workflow and the key its result is stored under."""

    action: str
    """Action name handed to the ``action_runner`` callback."""

    arguments: Mapping[str, object]
    """Arguments handed to the ``action_runner`` callback."""

    when: str = ""
    """Condition evaluated before the step; empty means "always run"."""

    continue_on_error: bool = False
    """When true, a failure is recorded but later steps still run."""

    def to_dict(self) -> dict[str, object]:
        """JSON-ready view for the UI bridge."""
        return {
            "name": self.name,
            "action": self.action,
            "arguments": dict(self.arguments),
            "when": self.when,
            "continue_on_error": self.continue_on_error,
        }


@dataclass(frozen=True, slots=True)
class WorkflowDef:
    """A parsed workflow definition."""

    name: str
    """Unique workflow name (also used to build the scheduler job id)."""

    description: str
    """Human-readable summary shown in the designer."""

    trigger: TriggerKind
    """How the workflow is started."""

    schedule: str
    """Cron expression, required when ``trigger`` is ``CRON``."""

    steps: tuple[StepSpec, ...]
    """Ordered steps; a tuple because order is the definition."""

    path: str = ""
    """Source file, kept so the UI can point at the definition it loaded."""

    def to_dict(self) -> dict[str, object]:
        """JSON-ready view for the UI bridge."""
        return {
            "name": self.name,
            "description": self.description,
            "trigger": self.trigger.value,
            "schedule": self.schedule,
            "steps": [step.to_dict() for step in self.steps],
            "path": self.path,
        }


@dataclass(frozen=True, slots=True)
class StepResult:
    """The outcome of one step in one run."""

    name: str
    """Name of the step this result belongs to."""

    skipped: bool
    """True when ``when`` evaluated falsy; a skipped step is not a failure."""

    ok: bool
    """True when the step ran and its action returned without raising."""

    output: str
    """Text returned by the action runner (empty when skipped or failed)."""

    error: str = ""
    """``TypeName: message`` of the failure, empty otherwise."""

    elapsed_ms: int = 0
    """Wall-clock duration, for the designer's timeline view."""

    def to_dict(self) -> dict[str, object]:
        """JSON-ready view for the UI bridge."""
        return {
            "name": self.name,
            "skipped": self.skipped,
            "ok": self.ok,
            "output": self.output,
            "error": self.error,
            "elapsed_ms": self.elapsed_ms,
        }


@dataclass(frozen=True, slots=True)
class WorkflowRun:
    """The outcome of one whole workflow execution."""

    run_id: int
    """Row id in ``workflow_runs``."""

    workflow: str
    """Name of the workflow that ran."""

    started_at: str
    """Canonical timestamp of the first step."""

    finished_at: str
    """Canonical timestamp of the last step (or of the stop)."""

    ok: bool
    """True only when every executed step succeeded."""

    steps: tuple[StepResult, ...]
    """Per-step outcomes, in execution order (including the ones that never ran)."""

    error: str = ""
    """First failure that stopped the run, empty when ``ok``."""

    def to_dict(self) -> dict[str, object]:
        """JSON-ready view for the UI bridge."""
        return {
            "run_id": self.run_id,
            "workflow": self.workflow,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "ok": self.ok,
            "steps": [step.to_dict() for step in self.steps],
            "error": self.error,
        }


__all__ = ["StepResult", "StepSpec", "TriggerKind", "WorkflowDef", "WorkflowRun"]
