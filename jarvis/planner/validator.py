"""Plan validation: the checks that make a model-authored plan safe to execute.

A language model asked for a plan will happily produce a step that depends on
itself, a dependency index of 7 in a three-step plan, or twelve steps for a
one-line request. None of those are hypothetical — they are the ordinary output
of a model that is reasoning about the *goal* rather than about the data
structure it is filling in.

So every plan is validated before anybody executes it. The checks are cheap and
the failure mode they prevent is expensive: a cyclic dependency turns an
executor into an infinite loop, and a dependency on a non-existent step turns it
into a plan that silently never runs anything.

Validation is separate from decomposition on purpose — the same checks apply to
a plan a human hand-wrote into a workflow file, and to one a re-planner produced
from a partial plan.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence
from typing import Final

from jarvis.core.exceptions import PlannerError
from jarvis.planner.types import Plan, PlanStep, StepStatus

logger = logging.getLogger("jarvis.planner.validator")

MAX_TITLE_CHARS: Final[int] = 200
"""A step title is one readable line, not a paragraph. Longer means the model
packed an explanation into the field a human is supposed to scan."""

_UNVISITED: Final[int] = 0
_IN_PROGRESS: Final[int] = 1
_FINISHED: Final[int] = 2
"""Depth-first colouring marks.

Named rather than inlined as ``0/1/2``: a cycle check where the three states are
anonymous integers is a check nobody can review.
"""


def validate_steps(
    steps: Sequence[PlanStep],
    *,
    max_steps: int,
    known_actions: Iterable[str] | None = None,
) -> tuple[PlanStep, ...]:
    """Check a step list and return it with ids normalised.

    Args:
        steps: Steps as produced, with ids already assigned.
        max_steps: Upper bound on the number of steps.
        known_actions: Tool names a step's ``action`` may name. ``None`` skips
            the check — a plan with no executor bound yet should still validate
            structurally.

    Returns:
        The steps, unchanged apart from normalised ``depends_on``.

    Raises:
        PlannerError: on an empty plan, too many steps, a duplicate id, an
            unknown action, a dependency on a missing step, a self-dependency,
            or a cycle.
    """
    if not steps:
        raise PlannerError("计划里没有任何步骤")
    if len(steps) > max_steps:
        raise PlannerError(
            f"计划步骤过多：{len(steps)} 步，上限 {max_steps} 步",
            details={"steps": len(steps), "max_steps": max_steps},
        )

    ids: list[str] = [step.step_id for step in steps]
    duplicates = sorted({step_id for step_id in ids if ids.count(step_id) > 1})
    if duplicates:
        raise PlannerError(
            f"计划里出现重复的步骤编号：{', '.join(duplicates)}",
            details={"duplicates": duplicates},
        )
    known_ids = set(ids)

    if known_actions is not None:
        allowed = set(known_actions)
        for step in steps:
            if step.action and step.action not in allowed:
                raise PlannerError(
                    f"步骤「{step.title}」引用了不存在的工具：{step.action}",
                    details={"step": step.step_id, "action": step.action},
                )

    for step in steps:
        for dependency in step.depends_on:
            if dependency == step.step_id:
                raise PlannerError(
                    f"步骤「{step.title}」依赖了自己",
                    details={"step": step.step_id},
                )
            if dependency not in known_ids:
                raise PlannerError(
                    f"步骤「{step.title}」依赖了不存在的步骤：{dependency}",
                    details={"step": step.step_id, "depends_on": dependency},
                )

    _reject_cycles(steps)
    return tuple(steps)


def _reject_cycles(steps: Sequence[PlanStep]) -> None:
    """Depth-first cycle detection over the dependency graph.

    Iterative rather than recursive: a malformed plan can be deep, and a
    ``RecursionError`` from inside a validator is a worse error message than
    "步骤 A 和 B 互相依赖".
    """
    graph: dict[str, tuple[str, ...]] = {step.step_id: step.depends_on for step in steps}
    titles = {step.step_id: step.title for step in steps}
    colour: dict[str, int] = dict.fromkeys(graph, _UNVISITED)

    for root in graph:
        if colour[root] != _UNVISITED:
            continue
        # Each stack entry is (node, index of its next dependency to visit).
        stack: list[tuple[str, int]] = [(root, 0)]
        colour[root] = _IN_PROGRESS
        while stack:
            node, index = stack[-1]
            dependencies = graph[node]
            if index >= len(dependencies):
                colour[node] = _FINISHED
                stack.pop()
                continue
            stack[-1] = (node, index + 1)
            neighbour = dependencies[index]
            if colour.get(neighbour, _FINISHED) == _IN_PROGRESS:
                raise PlannerError(
                    f"计划的依赖关系存在环：{titles.get(node, node)} → "
                    f"{titles.get(neighbour, neighbour)}",
                    details={"from": node, "to": neighbour},
                )
            if colour.get(neighbour, _FINISHED) == _UNVISITED:
                colour[neighbour] = _IN_PROGRESS
                stack.append((neighbour, 0))


def validate_plan(
    plan: Plan, *, max_steps: int, known_actions: Iterable[str] | None = None
) -> Plan:
    """Validate a whole plan.

    Raises:
        PlannerError: on an empty goal, or on any structural problem the step
            checks find.
    """
    if not plan.goal.strip():
        raise PlannerError("计划目标不能为空")
    steps = validate_steps(plan.steps, max_steps=max_steps, known_actions=known_actions)
    return Plan(
        goal=plan.goal,
        steps=steps,
        rationale=plan.rationale,
        revision=plan.revision,
        abandoned=plan.abandoned,
    )


def renumber(steps: Sequence[PlanStep], *, start: int = 1) -> tuple[PlanStep, ...]:
    """Assign fresh ``s1``-style ids and remap ``depends_on`` accordingly.

    Used when a re-plan returns steps that refer to each other by their position
    in the *new* list. Doing the remap here rather than trusting the model keeps
    a re-plan from pointing at a step id that only existed in the old plan.
    """
    id_map = {step.step_id: f"s{start + index}" for index, step in enumerate(steps)}
    remapped: list[PlanStep] = []
    for step in steps:
        remapped.append(
            PlanStep(
                step_id=id_map[step.step_id],
                title=step.title,
                action=step.action,
                arguments=step.arguments,
                depends_on=tuple(
                    id_map[dependency] for dependency in step.depends_on if dependency in id_map
                ),
                status=StepStatus.PENDING,
                notes="",
            )
        )
    return tuple(remapped)


__all__ = ["MAX_TITLE_CHARS", "renumber", "validate_plan", "validate_steps"]
