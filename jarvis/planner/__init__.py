"""Task planning: decomposition of complex requests into executable steps.

Responsibility (delivered in phase 10):
    * Plan models (steps, dependencies, status), plan validation and
      re-planning support used by the Planner Agent.

How it works, and what it deliberately is not:

* :mod:`jarvis.planner.decomposer` asks a model for steps and parses the answer
  defensively — a chatty preamble, an out-of-range dependency index or a
  string where an object belongs are all repaired or dropped with a warning
  rather than costing the user their plan.
* :mod:`jarvis.planner.validator` is what actually decides whether a plan is
  executable: duplicate ids, unknown actions, dangling dependencies and
  **cycles** are refused before anything runs. A cyclic dependency turns an
  executor into an infinite loop, which is why this is a hard gate and not a
  warning.
* :class:`jarvis.planner.service.PlannerService` produces and maintains plans
  and **does not execute them**. Running a step needs the tool registry and the
  event stream, both of which live above this layer; keeping the split means the
  planner is testable with a stub model and no registry, and "who decided" stays
  separate from "who did".

Plans are immutable: advancing a step returns a new plan, because a plan is read
by the model that produced it, the executor working through it, and a human
watching progress, and an object that changes underneath its readers is how
"step 3 failed" becomes "which run was that?".

Allowed dependencies: ``core``, ``config``, ``llm``, ``prompt``.
"""

from jarvis.planner.decomposer import LlmDecomposer
from jarvis.planner.service import PlannerService
from jarvis.planner.types import (
    TERMINAL_STEP_STATUSES,
    Plan,
    PlanStatus,
    PlanStep,
    StepOutcome,
    StepStatus,
)
from jarvis.planner.validator import MAX_TITLE_CHARS, renumber, validate_plan, validate_steps

__all__ = [
    "MAX_TITLE_CHARS",
    "TERMINAL_STEP_STATUSES",
    "LlmDecomposer",
    "Plan",
    "PlanStatus",
    "PlanStep",
    "PlannerService",
    "StepOutcome",
    "StepStatus",
    "renumber",
    "validate_plan",
    "validate_steps",
]
