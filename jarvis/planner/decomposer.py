"""Turning a goal into steps, via the model.

The model proposes; this module disposes. Every field is parsed defensively
because the reply is free text that merely *usually* contains JSON:

* A chatty preamble, a fenced code block, or a trailing "以上。" all happen.
* ``depends_on`` comes back as **indices** (that is what the prompt asks for),
  and a model will reference step 7 in a three-step plan.
* An ``arguments`` value will occasionally be a string instead of an object.

None of those should cost the user their plan. Each is repaired or dropped with
a warning, and the result is then handed to
:func:`jarvis.planner.validator.validate_steps`, which is the part that actually
decides whether it is executable.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Mapping, Sequence
from typing import Final, cast

from jarvis.core.exceptions import JarvisError, PlannerError
from jarvis.llm.client import LlmClient
from jarvis.llm.types import ChatMessage, GenerationOptions
from jarvis.planner.types import Plan, PlanStep, StepStatus
from jarvis.prompt import render_prompt

logger = logging.getLogger("jarvis.planner.decomposer")

_JSON_BLOB: Final[re.Pattern[str]] = re.compile(r"[\[{].*[\]}]", re.DOTALL)
"""Grab the outermost JSON value out of a reply that may have prose around it."""


def _tools_block(tool_names: Sequence[str]) -> str:
    """Render the available tool names for the prompt.

    Names only, no schemas: the planner decides *what* to do, and the executor
    validates arguments against the schema at call time. Sending full schemas
    here would double the prompt for a decision that does not need them.
    """
    return "\n".join(f"- {name}" for name in tool_names) if tool_names else "（当前没有可用工具）"


class LlmDecomposer:
    """Asks a model for a step list and parses the answer."""

    def __init__(
        self,
        llm: LlmClient,
        *,
        max_steps: int,
        temperature: float | None = None,
    ) -> None:
        self._llm = llm
        self._max_steps = max(1, max_steps)
        self._options = (
            GenerationOptions(temperature=temperature) if temperature is not None else None
        )

    def decompose(self, goal: str, *, tool_names: Sequence[str] = ()) -> tuple[PlanStep, ...]:
        """Break ``goal`` into steps.

        Raises:
            PlannerError: if the goal is blank, the model call fails, or the
                reply contains no usable steps.
        """
        if not goal.strip():
            raise PlannerError("计划目标不能为空")
        prompt = render_prompt(
            "planner_decompose",
            max_steps=self._max_steps,
            tools=_tools_block(tool_names),
            goal=goal.strip(),
        )
        raw = self._ask([ChatMessage.user(prompt)])
        steps = self._parse_steps(raw)
        if not steps:
            # For a *fresh* goal an empty plan is not an answer: the caller asked
            # for steps and got none. (For a re-plan it is an answer — see
            # :meth:`replan` — which is why the parser itself does not raise.)
            raise PlannerError("规划器没有给出任何有效步骤", details={"reply": raw[:200]})
        return steps

    def replan(
        self,
        plan: Plan,
        failure: str,
        *,
        tool_names: Sequence[str] = (),
    ) -> tuple[PlanStep, ...]:
        """Produce replacement steps for the unfinished part of ``plan``.

        Raises:
            PlannerError: if the model call fails, or it returns nothing — which
                means "this goal cannot be finished", and the caller turns that
                into an abandoned plan rather than an empty one.
        """
        completed = plan.completed_titles()
        plan_text = "\n".join(
            f"- [{step.status.value}] {step.title}" + (f"（{step.notes}）" if step.notes else "")
            for step in plan.steps
        )
        if completed:
            plan_text += "\n已成功、不要重复：" + "、".join(completed)
        prompt = render_prompt(
            "planner_replan",
            goal=plan.goal,
            plan=plan_text,
            failure=failure.strip() or "未说明",
            tools=_tools_block(tool_names),
        )
        raw = self._ask([ChatMessage.user(prompt)])
        return self._parse_steps(raw)

    # -- internals ---------------------------------------------------------

    def _ask(self, messages: Sequence[ChatMessage]) -> str:
        try:
            return self._llm.complete(list(messages), options=self._options).content
        except JarvisError as exc:
            raise PlannerError(f"规划失败：{exc}", details={"reason": str(exc)}) from exc

    def _parse_steps(self, reply: str) -> tuple[PlanStep, ...]:
        """Parse the reply into steps, repairing what can be repaired.

        Returns an empty tuple for a well-formed but empty answer, and for one
        where no entry carried a usable title. Whether that is a failure is the
        *caller's* decision, and the two callers differ: for a fresh goal it
        means the model did not do the task, while for a re-plan it is the
        documented way of saying "this goal cannot be finished".

        Raises:
            PlannerError: only when there is no JSON, or its ``steps`` is not a
                list — i.e. when there is nothing to interpret at all.
        """
        payload = self._extract_json(reply)
        if payload is None:
            logger.warning("planner reply contained no JSON: %r", reply[:200])
            raise PlannerError("规划器没有返回可解析的步骤", details={"reply": reply[:200]})

        rationale = ""
        entries: object = payload
        if isinstance(payload, Mapping):
            rationale = str(payload.get("rationale", "") or "")
            entries = payload.get("steps", [])
        if not isinstance(entries, list):
            raise PlannerError("规划器返回的 steps 不是数组", details={"reply": reply[:200]})

        drafts: list[tuple[str, str, Mapping[str, object], list[object]]] = []
        for entry in entries[: self._max_steps]:
            if not isinstance(entry, Mapping):
                continue
            title = entry.get("title")
            if not isinstance(title, str) or not title.strip():
                continue
            action = entry.get("action")
            raw_arguments = entry.get("arguments")
            arguments: Mapping[str, object] = (
                raw_arguments if isinstance(raw_arguments, Mapping) else {}
            )
            raw_deps = entry.get("depends_on")
            deps: list[object] = list(raw_deps) if isinstance(raw_deps, list) else []
            drafts.append(
                (
                    title.strip(),
                    action.strip() if isinstance(action, str) else "",
                    arguments,
                    deps,
                )
            )

        steps = tuple(
            PlanStep(
                step_id=f"s{index + 1}",
                title=title,
                action=action,
                arguments=dict(arguments),
                depends_on=self._resolve_dependencies(deps, index, len(drafts)),
                status=StepStatus.PENDING,
            )
            for index, (title, action, arguments, deps) in enumerate(drafts)
        )
        if rationale:
            logger.debug("planner rationale: %s", rationale[:200])
        return steps

    @staticmethod
    def _resolve_dependencies(raw: Sequence[object], index: int, total: int) -> tuple[str, ...]:
        """Turn 0-based indices into step ids, dropping the impossible ones.

        A dependency the model invented is dropped with a warning rather than
        failing the whole plan: refusing to plan because of one bogus edge is a
        worse outcome for the user than planning without it, and the warning is
        the honest record. The validator still rejects anything structurally
        broken that survives this step.
        """
        resolved: list[str] = []
        for item in raw:
            if isinstance(item, bool):
                continue
            if isinstance(item, int):
                target = item
            elif isinstance(item, str) and item.strip().lstrip("-").isdigit():
                target = int(item.strip())
            else:
                logger.warning("dropping non-numeric dependency %r", item)
                continue
            if not 0 <= target < total:
                logger.warning(
                    "dropping out-of-range dependency %d (plan has %d steps)", target, total
                )
                continue
            if target == index:
                logger.warning("dropping self-dependency on step %d", target)
                continue
            step_id = f"s{target + 1}"
            if step_id not in resolved:
                resolved.append(step_id)
        return tuple(resolved)

    @staticmethod
    def _decode_json(text: str) -> object | None:
        """Decode JSON, returning ``None`` instead of raising.

        ``cast`` because ``json.loads`` is typed as returning ``Any`` — this
        function's whole point is to narrow it to ``object`` so callers have to
        check the shape before touching it.
        """
        try:
            return cast("object", json.loads(text))
        except json.JSONDecodeError:
            return None

    @classmethod
    def _extract_json(cls, reply: str) -> object | None:
        """Find and decode the outermost JSON value in a reply."""
        text = reply.strip()
        if not text:
            return None
        # Prefer the fenced block when there is one: a model that wraps its JSON
        # in ```json ... ``` may also mention braces in the surrounding prose.
        fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
        candidate = fenced.group(1).strip() if fenced else text
        for attempt in (candidate, text):
            decoded = cls._decode_json(attempt)
            if decoded is not None:
                return decoded
        match = _JSON_BLOB.search(candidate)
        if match is None:
            return None
        return cls._decode_json(match.group(0))


__all__ = ["LlmDecomposer"]
