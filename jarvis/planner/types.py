"""Plan vocabulary: steps, statuses, and the plan itself.

Plans are **immutable**. Advancing a step produces a new :class:`Plan` rather
than mutating one, because a plan is read by several places at once — the model
that produced it, the executor working through it, and a human looking at the
progress — and an object that changes underneath its readers is how "step 3
failed" becomes "which run was that?".

Dependencies are declared as *indices*, not as "the previous step". That is what
lets the executor run independent steps together, and lets a re-plan skip the
part that already succeeded instead of replaying it.
"""

from __future__ import annotations

import enum
from collections.abc import Mapping
from dataclasses import dataclass, field


class StepStatus(enum.StrEnum):
    """Where one step is in its lifecycle."""

    PENDING = "pending"
    """Not started. May or may not be unblocked yet — see ``Plan.ready``."""

    RUNNING = "running"
    """Claimed by an executor."""

    DONE = "done"
    """Finished successfully."""

    FAILED = "failed"
    """Attempted and failed. A re-plan starts from here."""

    SKIPPED = "skipped"
    """Deliberately not run — a guard was false, or a re-plan dropped it."""


class PlanStatus(enum.StrEnum):
    """Where the plan as a whole is."""

    DRAFT = "draft"
    """Produced, not started."""

    ACTIVE = "active"
    """At least one step is done or running."""

    DONE = "done"
    """Every step finished successfully (or was skipped on purpose)."""

    FAILED = "failed"
    """At least one step failed and no re-plan rescued it."""

    ABANDONED = "abandoned"
    """Given up on by a human or by the re-planner."""


TERMINAL_STEP_STATUSES: frozenset[StepStatus] = frozenset(
    {StepStatus.DONE, StepStatus.FAILED, StepStatus.SKIPPED}
)
"""Step states that will not change again without an explicit re-plan."""


@dataclass(frozen=True, slots=True)
class PlanStep:
    """One executable unit of a plan."""

    step_id: str
    """Stable id, ``s1`` / ``s2`` … Positional so a re-plan can refer to the
    part that already ran."""

    title: str
    """One line a human can read and verify against. Chinese, no pronouns."""

    action: str = ""
    """Tool name to invoke, or empty when the step is reasoning-only."""

    arguments: Mapping[str, object] = field(default_factory=dict)
    """Arguments for :attr:`action`."""

    depends_on: tuple[str, ...] = ()
    """Step ids that must be ``DONE`` (or ``SKIPPED``) before this one runs."""

    status: StepStatus = StepStatus.PENDING
    notes: str = ""
    """Free text: an executor's output summary, or why it failed."""

    def to_dict(self) -> dict[str, object]:
        return {
            "step_id": self.step_id,
            "title": self.title,
            "action": self.action,
            "arguments": dict(self.arguments),
            "depends_on": list(self.depends_on),
            "status": self.status.value,
            "notes": self.notes,
        }

    def with_status(self, status: StepStatus, *, notes: str = "") -> PlanStep:
        """Return a copy in a new status, optionally with new notes."""
        return PlanStep(
            step_id=self.step_id,
            title=self.title,
            action=self.action,
            arguments=self.arguments,
            depends_on=self.depends_on,
            status=status,
            notes=notes or self.notes,
        )


@dataclass(frozen=True, slots=True)
class Plan:
    """A goal plus the ordered steps that should achieve it."""

    goal: str
    steps: tuple[PlanStep, ...]
    rationale: str = ""
    """Why the plan looks like this. Useful when the user disagrees with it."""

    revision: int = 1
    """1 for the first plan, +1 per re-plan. Lets a UI say "第 2 版计划"."""

    abandoned: bool = False
    """Set when a human or the re-planner gave up.

    The one piece of plan state that is *not* derivable from the steps: a plan
    whose steps all failed could equally be "failed" or "given up on", and the
    difference matters to whoever reads it.
    """

    @property
    def status(self) -> PlanStatus:
        """Where the plan is, **derived** from its steps.

        A property rather than a field on purpose. When it was a field, a plan
        built directly with a done step still reported ``DRAFT`` until somebody
        called :meth:`with_step`, and two sources of truth for "is this finished"
        is how a plan ends up displayed as complete while a step says ``failed``.
        """
        if self.abandoned:
            return PlanStatus.ABANDONED
        if any(step.status is StepStatus.FAILED for step in self.steps):
            return PlanStatus.FAILED
        if self.steps and all(step.status in TERMINAL_STEP_STATUSES for step in self.steps):
            return PlanStatus.DONE
        if any(step.status is not StepStatus.PENDING for step in self.steps):
            return PlanStatus.ACTIVE
        return PlanStatus.DRAFT

    def to_dict(self) -> dict[str, object]:
        return {
            "goal": self.goal,
            "status": self.status.value,
            "rationale": self.rationale,
            "revision": self.revision,
            "steps": [step.to_dict() for step in self.steps],
        }

    # -- queries -----------------------------------------------------------

    def step(self, step_id: str) -> PlanStep | None:
        """Look a step up by id."""
        for candidate in self.steps:
            if candidate.step_id == step_id:
                return candidate
        return None

    def with_step(self, updated: PlanStep) -> Plan:
        """Return a copy with one step replaced."""
        steps = tuple(updated if step.step_id == updated.step_id else step for step in self.steps)
        return Plan(
            goal=self.goal,
            steps=steps,
            rationale=self.rationale,
            revision=self.revision,
            abandoned=self.abandoned,
        )

    def ready(self) -> tuple[PlanStep, ...]:
        """Pending steps whose dependencies have all finished.

        A dependency that **failed** does not unblock its dependents, and a
        dependency that was **skipped** does: skipping means "this was not
        needed", which is a satisfied precondition, while failing means "the
        input this step needs does not exist".
        """
        done_ids = {
            step.step_id
            for step in self.steps
            if step.status in {StepStatus.DONE, StepStatus.SKIPPED}
        }
        blocked_by_failure = {
            step.step_id for step in self.steps if step.status is StepStatus.FAILED
        }
        ready: list[PlanStep] = []
        for step in self.steps:
            if step.status is not StepStatus.PENDING:
                continue
            if any(dep in blocked_by_failure for dep in step.depends_on):
                continue
            if all(dep in done_ids for dep in step.depends_on):
                ready.append(step)
        return tuple(ready)

    def pending(self) -> tuple[PlanStep, ...]:
        """Steps not yet in a terminal state."""
        return tuple(step for step in self.steps if step.status not in TERMINAL_STEP_STATUSES)

    def failed(self) -> tuple[PlanStep, ...]:
        """Steps that failed."""
        return tuple(step for step in self.steps if step.status is StepStatus.FAILED)

    def completed_titles(self) -> tuple[str, ...]:
        """Titles of the steps that already succeeded — what a re-plan must not repeat."""
        return tuple(step.title for step in self.steps if step.status is StepStatus.DONE)

    @property
    def is_complete(self) -> bool:
        """Whether nothing is left to do."""
        return not self.pending()

    @property
    def succeeded(self) -> bool:
        """Whether the goal was reached: everything finished and nothing failed."""
        return self.is_complete and not self.failed()

    def summary(self) -> str:
        """One-line progress, e.g. ``2/5 完成，1 失败``."""
        done = sum(1 for step in self.steps if step.status is StepStatus.DONE)
        failed = len(self.failed())
        suffix = f"，{failed} 失败" if failed else ""
        return f"{done}/{len(self.steps)} 完成{suffix}"


@dataclass(frozen=True, slots=True)
class StepOutcome:
    """What happened when a step ran, as reported back to the planner."""

    step_id: str
    ok: bool
    detail: str = ""
    error: str = ""

    def to_dict(self) -> dict[str, object]:
        return {"step_id": self.step_id, "ok": self.ok, "detail": self.detail, "error": self.error}
