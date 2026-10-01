"""``PlannerService`` — decompose a goal, track progress, re-plan on failure.

Deliberately **not** an executor. The planner produces and maintains a plan;
running the steps is the orchestration layer's job, because that is where the
tools, the model client and the user-facing event stream already live. Keeping
the split means the planner can be tested with a stub model and no tool
registry at all, and it means "who decided to do this" and "who did it" stay
separate questions.

The one thing it does own is the *transition* logic — which steps are ready,
what a failure does to the rest — because that logic is where plans go wrong
(see :meth:`jarvis.planner.types.Plan.ready`).
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING

from jarvis.core.exceptions import JarvisError, PlannerError
from jarvis.planner.decomposer import LlmDecomposer
from jarvis.planner.types import (
    Plan,
    PlanStep,
    StepOutcome,
    StepStatus,
)
from jarvis.planner.validator import renumber, validate_steps

if TYPE_CHECKING:  # pragma: no cover - typing only
    from jarvis.config.schema import PlannerSection
    from jarvis.llm.client import LlmClient

logger = logging.getLogger("jarvis.planner.service")


class PlannerService:
    """Produces and maintains plans. Does not run them."""

    name = "planner"

    def __init__(
        self,
        settings_provider: Callable[[], PlannerSection],
        *,
        llm_provider: Callable[[], LlmClient] | None = None,
        tool_names_provider: Callable[[], Sequence[str]] | None = None,
    ) -> None:
        """Create the service.

        Args:
            settings_provider: Returns the validated ``planner`` config section.
            llm_provider: Yields the chat client used for decomposition.
                ``None`` disables planning and :meth:`plan` says so — a
                text-only install still runs, it just cannot plan.
            tool_names_provider: Yields the names a step's ``action`` may use.
                A provider rather than the registry itself, because ``planner``
                sits at L3 and the dependency table does not let it import
                ``jarvis.tools``.
        """
        self._settings_provider = settings_provider
        self._llm_provider = llm_provider
        self._tool_names_provider = tool_names_provider
        self._started = False

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        """Mark ready (idempotent). No model call, no network."""
        if self._started:
            return
        self._started = True
        logger.info("planner service ready (max_steps=%d)", self._settings_provider().max_steps)

    def stop(self) -> None:
        self._started = False

    @property
    def running(self) -> bool:
        return self._started

    @property
    def enabled(self) -> bool:
        """Whether the config allows planning."""
        return self._settings_provider().enabled

    # -- planning ----------------------------------------------------------

    def plan(self, goal: str) -> Plan:
        """Break ``goal`` into a validated plan.

        Raises:
            PlannerError: if planning is unavailable, the goal is blank, the
                model failed, or its output is not an executable plan.
        """
        self._require()
        decomposer = self._decomposer()
        steps = decomposer.decompose(goal.strip(), tool_names=self._tool_names())
        validated = validate_steps(
            steps,
            max_steps=self._settings_provider().max_steps,
            known_actions=self._tool_names() or None,
        )
        plan = Plan(goal=goal.strip(), steps=validated)
        logger.info("planned %d step(s) for %r", len(plan.steps), goal.strip()[:60])
        return plan

    def replan(self, plan: Plan, failure: str) -> Plan:
        """Produce the next revision of ``plan`` after a failure.

        Already-succeeded steps are carried over untouched and the new steps are
        appended with fresh ids. Replaying a step that succeeded is worse than a
        failed plan: a step with a side effect would fire twice.

        Raises:
            PlannerError: if planning is unavailable or the model call fails.
        """
        self._require()
        if not plan.failed() and not failure.strip():
            raise PlannerError("计划没有失败，不需要重新规划")
        try:
            replacement = self._decomposer().replan(plan, failure, tool_names=self._tool_names())
        except PlannerError:
            raise
        if not replacement:
            logger.warning("re-planner returned nothing; abandoning %r", plan.goal[:60])
            return Plan(
                goal=plan.goal,
                steps=plan.steps,
                rationale="重新规划后没有剩余步骤，目标判定为无法完成",
                revision=plan.revision + 1,
                abandoned=True,
            )

        kept = tuple(step for step in plan.steps if step.status is StepStatus.DONE)
        # The re-planner works with its own 0-based view of the new steps, so its
        # ids are remapped before anything else touches them. A new step that
        # tried to depend on an *old* step id loses that edge — the model has no
        # way to name a step it was not shown, and an unblocked step is a better
        # failure than one that waits forever for a dependency that never comes.
        fresh = renumber(replacement, start=len(kept) + 1)
        merged = validate_steps(
            kept + fresh,
            max_steps=self._settings_provider().max_steps,
            known_actions=self._tool_names() or None,
        )
        logger.info(
            "re-planned %r: %d kept, %d new (revision %d)",
            plan.goal[:60],
            len(kept),
            len(fresh),
            plan.revision + 1,
        )
        return Plan(
            goal=plan.goal,
            steps=merged,
            rationale=f"第 {plan.revision} 版失败后重新规划",
            revision=plan.revision + 1,
        )

    # -- progress ----------------------------------------------------------

    def ready_steps(self, plan: Plan) -> tuple[PlanStep, ...]:
        """Pending steps whose dependencies have finished."""
        return plan.ready()

    def mark(self, plan: Plan, step_id: str, status: StepStatus, *, notes: str = "") -> Plan:
        """Record a step's new status and return the updated plan.

        Raises:
            PlannerError: if ``step_id`` is not in the plan.
        """
        step = plan.step(step_id)
        if step is None:
            raise PlannerError(f"计划里没有这个步骤：{step_id}", details={"step": step_id})
        return plan.with_step(step.with_status(status, notes=notes))

    def apply_outcome(self, plan: Plan, outcome: StepOutcome) -> Plan:
        """Fold one step's result into the plan.

        A failure does **not** automatically cascade to the remaining steps:
        they stay ``PENDING`` and blocked, so a re-plan can decide what to do
        with them. Marking them ``SKIPPED`` here would throw away the work the
        re-planner is about to need.
        """
        status = StepStatus.DONE if outcome.ok else StepStatus.FAILED
        notes = outcome.detail if outcome.ok else outcome.error or outcome.detail
        return self.mark(plan, outcome.step_id, status, notes=notes)

    def abandon(self, plan: Plan, reason: str) -> Plan:
        """Give up on a plan, keeping the steps for the record."""
        return Plan(
            goal=plan.goal,
            steps=plan.steps,
            rationale=reason,
            revision=plan.revision,
            abandoned=True,
        )

    # -- introspection -----------------------------------------------------

    def stats(self) -> dict[str, object]:
        """Read-only snapshot for the HUD and the health check."""
        settings = self._settings_provider()
        return {
            "running": self._started,
            "enabled": settings.enabled,
            "max_steps": settings.max_steps,
            "has_model": self._llm_provider is not None,
            "known_tools": len(self._tool_names()),
        }

    # -- internals ---------------------------------------------------------

    def _require(self) -> None:
        if not self._started:
            raise PlannerError("规划服务未启动")
        if not self.enabled:
            raise PlannerError("规划功能未启用（planner.enabled=false）")
        if self._llm_provider is None:
            raise PlannerError("规划需要一个大模型：当前没有配置模型客户端")

    def _decomposer(self) -> LlmDecomposer:
        provider = self._llm_provider
        if provider is None:  # pragma: no cover - _require already refused
            raise PlannerError("规划需要一个大模型：当前没有配置模型客户端")
        settings = self._settings_provider()
        try:
            client = provider()
        except JarvisError as exc:
            raise PlannerError(f"规划需要一个大模型：{exc}") from exc
        return LlmDecomposer(
            client,
            max_steps=settings.max_steps,
            temperature=settings.temperature,
        )

    def _tool_names(self) -> tuple[str, ...]:
        if self._tool_names_provider is None:
            return ()
        try:
            return tuple(self._tool_names_provider())
        except Exception:  # pragma: no cover - a registry bug must not break planning
            logger.exception("could not read the tool name list")
            return ()


__all__ = ["PlannerService"]
