"""Tests for task planning (jarvis.planner)."""

from __future__ import annotations

from collections.abc import Iterator, Sequence

import pytest

from jarvis.config.schema import PlannerSection
from jarvis.core.exceptions import LlmError, PlannerError
from jarvis.llm.client import LlmClient
from jarvis.llm.types import ChatMessage, ChatResponse, GenerationOptions, StreamChunk
from jarvis.planner import (
    LlmDecomposer,
    Plan,
    PlannerService,
    PlanStatus,
    PlanStep,
    StepOutcome,
    StepStatus,
    renumber,
    validate_plan,
    validate_steps,
)

TOOLS = ("read_file", "system_report", "disk_scan")


class ScriptedLlm:
    """Returns canned replies in order, recording every prompt it saw."""

    def __init__(self, *replies: str) -> None:
        self._replies = list(replies)
        self.calls: list[list[ChatMessage]] = []
        self.options: list[GenerationOptions | None] = []

    @property
    def provider_name(self) -> str:
        return "scripted"

    @property
    def model(self) -> str:
        return "scripted"

    def complete(
        self, messages: Sequence[ChatMessage], *, options: GenerationOptions | None = None
    ) -> ChatResponse:
        self.calls.append(list(messages))
        self.options.append(options)
        if not self._replies:
            raise LlmError("no scripted reply left")
        return ChatResponse(content=self._replies.pop(0), model="scripted")

    def stream(
        self, messages: Sequence[ChatMessage], *, options: GenerationOptions | None = None
    ) -> Iterator[StreamChunk]:
        yield from ()


def _section(**overrides: object) -> PlannerSection:
    raw: dict[str, object] = {"enabled": True, "max_steps": 8, "temperature": 0.2}
    raw.update(overrides)
    return PlannerSection.from_mapping(raw)


def _service(llm: object | None, **overrides: object) -> PlannerService:
    section = _section(**overrides)
    provider = (lambda: llm) if llm is not None else None
    service = PlannerService(
        lambda: section,
        llm_provider=provider,  # type: ignore[arg-type]
        tool_names_provider=lambda: TOOLS,
    )
    service.start()
    return service


def _step(
    step_id: str,
    *,
    action: str = "",
    depends_on: tuple[str, ...] = (),
    status: StepStatus = StepStatus.PENDING,
    title: str = "",
) -> PlanStep:
    return PlanStep(
        step_id=step_id,
        title=title or f"步骤 {step_id}",
        action=action,
        depends_on=depends_on,
        status=status,
    )


# ---------------------------------------------------------------------------
# Validator
# ---------------------------------------------------------------------------


class TestValidator:
    def test_valid_plan_passes(self) -> None:
        steps = [_step("s1", action="read_file"), _step("s2", depends_on=("s1",))]
        assert validate_steps(steps, max_steps=8, known_actions=TOOLS) == tuple(steps)

    def test_empty_plan_is_refused(self) -> None:
        with pytest.raises(PlannerError, match="没有任何步骤"):
            validate_steps([], max_steps=8)

    def test_too_many_steps_is_refused(self) -> None:
        steps = [_step(f"s{i}") for i in range(1, 10)]
        with pytest.raises(PlannerError, match="步骤过多"):
            validate_steps(steps, max_steps=8)

    def test_duplicate_ids_are_refused(self) -> None:
        with pytest.raises(PlannerError, match="重复的步骤编号"):
            validate_steps([_step("s1"), _step("s1")], max_steps=8)

    def test_unknown_action_is_refused(self) -> None:
        """A plan that names a tool that does not exist is a plan that cannot run."""
        with pytest.raises(PlannerError, match="不存在的工具"):
            validate_steps([_step("s1", action="nope")], max_steps=8, known_actions=TOOLS)

    def test_action_check_is_skipped_without_a_tool_list(self) -> None:
        assert validate_steps([_step("s1", action="anything")], max_steps=8)

    def test_dangling_dependency_is_refused(self) -> None:
        with pytest.raises(PlannerError, match="依赖了不存在的步骤"):
            validate_steps([_step("s1", depends_on=("s9",))], max_steps=8)

    def test_self_dependency_is_refused(self) -> None:
        with pytest.raises(PlannerError, match="依赖了自己"):
            validate_steps([_step("s1", depends_on=("s1",))], max_steps=8)

    def test_two_node_cycle_is_refused(self) -> None:
        """A cyclic dependency turns an executor into an infinite loop."""
        steps = [_step("s1", depends_on=("s2",)), _step("s2", depends_on=("s1",))]
        with pytest.raises(PlannerError, match="存在环"):
            validate_steps(steps, max_steps=8)

    def test_three_node_cycle_is_refused(self) -> None:
        steps = [
            _step("s1", depends_on=("s3",)),
            _step("s2", depends_on=("s1",)),
            _step("s3", depends_on=("s2",)),
        ]
        with pytest.raises(PlannerError, match="存在环"):
            validate_steps(steps, max_steps=8)

    def test_diamond_dependency_is_allowed(self) -> None:
        """Only cycles are refused; a step with two parents is ordinary."""
        steps = [
            _step("s1"),
            _step("s2", depends_on=("s1",)),
            _step("s3", depends_on=("s1",)),
            _step("s4", depends_on=("s2", "s3")),
        ]
        assert len(validate_steps(steps, max_steps=8)) == 4

    def test_deep_chain_does_not_recurse(self) -> None:
        """An iterative DFS: a malformed deep plan must not raise RecursionError
        from inside the validator."""
        steps = [_step("s1")] + [_step(f"s{i}", depends_on=(f"s{i - 1}",)) for i in range(2, 300)]
        assert len(validate_steps(steps, max_steps=400)) == 299

    def test_validate_plan_rejects_blank_goal(self) -> None:
        plan = Plan(goal="  ", steps=(_step("s1"),))
        with pytest.raises(PlannerError, match="目标不能为空"):
            validate_plan(plan, max_steps=8)

    def test_validate_plan_passes_a_good_plan(self) -> None:
        plan = Plan(goal="备份", steps=(_step("s1"),))
        assert validate_plan(plan, max_steps=8).goal == "备份"


class TestRenumber:
    def test_ids_are_reassigned_and_dependencies_remapped(self) -> None:
        steps = [_step("s1"), _step("s2", depends_on=("s1",))]
        renumbered = renumber(steps, start=5)
        assert [step.step_id for step in renumbered] == ["s5", "s6"]
        assert renumbered[1].depends_on == ("s5",)

    def test_status_is_reset_to_pending(self) -> None:
        """A re-planned step must not inherit a DONE status from the old plan."""
        steps = [_step("s1", status=StepStatus.DONE)]
        assert renumber(steps)[0].status is StepStatus.PENDING

    def test_unknown_dependencies_are_dropped(self) -> None:
        steps = [_step("s1", depends_on=("ghost",))]
        assert renumber(steps)[0].depends_on == ()


# ---------------------------------------------------------------------------
# Plan model
# ---------------------------------------------------------------------------


class TestPlanModel:
    def test_ready_returns_steps_with_no_dependencies(self) -> None:
        plan = Plan(goal="g", steps=(_step("s1"), _step("s2", depends_on=("s1",))))
        assert [step.step_id for step in plan.ready()] == ["s1"]

    def test_a_finished_dependency_unblocks_its_dependent(self) -> None:
        plan = Plan(
            goal="g",
            steps=(_step("s1", status=StepStatus.DONE), _step("s2", depends_on=("s1",))),
        )
        assert [step.step_id for step in plan.ready()] == ["s2"]

    def test_a_failed_dependency_does_not_unblock(self) -> None:
        """Failing means "the input this step needs does not exist"."""
        plan = Plan(
            goal="g",
            steps=(_step("s1", status=StepStatus.FAILED), _step("s2", depends_on=("s1",))),
        )
        assert plan.ready() == ()

    def test_a_skipped_dependency_does_unblock(self) -> None:
        """Skipping means "this was not needed", which is a satisfied precondition."""
        plan = Plan(
            goal="g",
            steps=(_step("s1", status=StepStatus.SKIPPED), _step("s2", depends_on=("s1",))),
        )
        assert [step.step_id for step in plan.ready()] == ["s2"]

    def test_independent_steps_are_both_ready(self) -> None:
        plan = Plan(goal="g", steps=(_step("s1"), _step("s2")))
        assert [step.step_id for step in plan.ready()] == ["s1", "s2"]

    def test_with_step_recomputes_the_status(self) -> None:
        plan = Plan(goal="g", steps=(_step("s1"),))
        updated = plan.with_step(_step("s1", status=StepStatus.DONE))
        assert updated.status is PlanStatus.DONE

    def test_status_becomes_failed_when_a_step_fails(self) -> None:
        plan = Plan(goal="g", steps=(_step("s1"),))
        assert plan.with_step(_step("s1", status=StepStatus.FAILED)).status is PlanStatus.FAILED

    def test_status_is_active_while_work_is_outstanding(self) -> None:
        plan = Plan(goal="g", steps=(_step("s1", status=StepStatus.DONE), _step("s2")))
        assert plan.status is PlanStatus.ACTIVE

    def test_status_is_derived_at_construction_not_only_on_update(self) -> None:
        """When status was a field, a plan built directly with a done step still
        reported DRAFT until somebody called with_step."""
        plan = Plan(goal="g", steps=(_step("s1", status=StepStatus.DONE),))
        assert plan.status is PlanStatus.DONE

    def test_abandoned_wins_over_a_failed_step(self) -> None:
        plan = Plan(
            goal="g",
            steps=(_step("s1", status=StepStatus.FAILED),),
            abandoned=True,
        )
        assert plan.status is PlanStatus.ABANDONED

    def test_summary_counts_done_and_failed(self) -> None:
        plan = Plan(
            goal="g",
            steps=(
                _step("s1", status=StepStatus.DONE),
                _step("s2", status=StepStatus.FAILED),
                _step("s3"),
            ),
        )
        assert plan.summary() == "1/3 完成，1 失败"

    def test_succeeded_requires_no_failures(self) -> None:
        ok = Plan(goal="g", steps=(_step("s1", status=StepStatus.DONE),))
        bad = Plan(
            goal="g",
            steps=(_step("s1", status=StepStatus.DONE), _step("s2", status=StepStatus.FAILED)),
        )
        assert ok.succeeded is True
        assert bad.succeeded is False

    def test_completed_titles_is_what_a_replan_must_not_repeat(self) -> None:
        plan = Plan(
            goal="g",
            steps=(
                _step("s1", status=StepStatus.DONE, title="读手册"),
                _step("s2", title="备份"),
            ),
        )
        assert plan.completed_titles() == ("读手册",)

    def test_step_lookup(self) -> None:
        plan = Plan(goal="g", steps=(_step("s1"),))
        assert plan.step("s1") is not None
        assert plan.step("nope") is None

    def test_with_status_keeps_existing_notes(self) -> None:
        step = PlanStep(step_id="s1", title="t", notes="原有说明")
        assert step.with_status(StepStatus.RUNNING).notes == "原有说明"

    def test_to_dict_is_json_ready(self) -> None:
        import json

        plan = Plan(goal="g", steps=(_step("s1"),))
        assert json.loads(json.dumps(plan.to_dict())) == plan.to_dict()


# ---------------------------------------------------------------------------
# Decomposer
# ---------------------------------------------------------------------------


class TestDecomposer:
    def _decompose(self, reply: str, **kwargs: object) -> tuple[PlanStep, ...]:
        llm = ScriptedLlm(reply)
        return LlmDecomposer(llm, max_steps=8, **kwargs).decompose("目标", tool_names=TOOLS)  # type: ignore[arg-type]

    def test_parses_a_plain_array(self) -> None:
        steps = self._decompose('[{"title": "读取配置"}, {"title": "备份数据"}]')
        assert [step.title for step in steps] == ["读取配置", "备份数据"]
        assert [step.step_id for step in steps] == ["s1", "s2"]

    def test_parses_an_object_with_rationale(self) -> None:
        steps = self._decompose('{"rationale": "因为要备份", "steps": [{"title": "读取配置"}]}')
        assert [step.title for step in steps] == ["读取配置"]

    def test_tolerates_a_fenced_block_with_prose(self) -> None:
        reply = '好的，这是计划：\n```json\n[{"title": "读取配置"}]\n```\n以上。'
        assert [step.title for step in self._decompose(reply)] == ["读取配置"]

    def test_tolerates_prose_around_a_bare_array(self) -> None:
        reply = '这是我的计划：[{"title": "读取配置"}] 请确认。'
        assert [step.title for step in self._decompose(reply)] == ["读取配置"]

    def test_dependency_indices_become_step_ids(self) -> None:
        """The prompt asks for 0-based indices; the plan speaks in step ids."""
        steps = self._decompose('[{"title": "a"}, {"title": "b", "depends_on": [0]}]')
        assert steps[1].depends_on == ("s1",)

    def test_out_of_range_dependency_is_dropped(self) -> None:
        """A model referencing step 7 in a two-step plan is hallucinating; refusing
        the whole plan for that is worse for the user than planning without it."""
        steps = self._decompose('[{"title": "a"}, {"title": "b", "depends_on": [7]}]')
        assert steps[1].depends_on == ()

    def test_self_dependency_is_dropped(self) -> None:
        steps = self._decompose('[{"title": "a", "depends_on": [0]}]')
        assert steps[0].depends_on == ()

    def test_non_numeric_dependency_is_dropped(self) -> None:
        steps = self._decompose('[{"title": "a"}, {"title": "b", "depends_on": ["s1"]}]')
        assert steps[1].depends_on == ()

    def test_string_index_is_accepted(self) -> None:
        """Models mix ``0`` and ``"0"`` freely."""
        steps = self._decompose('[{"title": "a"}, {"title": "b", "depends_on": ["0"]}]')
        assert steps[1].depends_on == ("s1",)

    def test_boolean_dependency_is_dropped(self) -> None:
        """``True`` is an ``int`` in Python; it must not become index 1."""
        steps = self._decompose(
            '[{"title": "a"}, {"title": "b"}, {"title": "c", "depends_on": [true]}]'
        )
        assert steps[2].depends_on == ()

    def test_arguments_that_are_not_an_object_become_empty(self) -> None:
        steps = self._decompose('[{"title": "a", "arguments": "oops"}]')
        assert dict(steps[0].arguments) == {}

    def test_action_is_read(self) -> None:
        steps = self._decompose('[{"title": "a", "action": "read_file"}]')
        assert steps[0].action == "read_file"

    def test_entries_without_a_title_are_skipped(self) -> None:
        steps = self._decompose('[{"title": "  "}, {"action": "x"}, {"title": "好的"}]')
        assert [step.title for step in steps] == ["好的"]

    def test_max_steps_caps_the_result(self) -> None:
        reply = "[" + ",".join(f'{{"title": "t{i}"}}' for i in range(20)) + "]"
        llm = ScriptedLlm(reply)
        steps = LlmDecomposer(llm, max_steps=3).decompose("目标")
        assert len(steps) == 3

    def test_the_prompt_receives_the_tool_list(self) -> None:
        """A plan that names tools the executor does not have is unusable."""
        llm = ScriptedLlm('[{"title": "a"}]')
        LlmDecomposer(llm, max_steps=8).decompose("目标", tool_names=TOOLS)
        assert "read_file" in llm.calls[0][-1].content

    def test_temperature_reaches_the_request(self) -> None:
        llm = ScriptedLlm('[{"title": "a"}]')
        LlmDecomposer(llm, max_steps=8, temperature=0.2).decompose("目标")
        assert llm.options[0] is not None
        assert llm.options[0].temperature == pytest.approx(0.2)

    def test_no_temperature_means_provider_default(self) -> None:
        llm = ScriptedLlm('[{"title": "a"}]')
        LlmDecomposer(llm, max_steps=8).decompose("目标")
        assert llm.options[0] is None

    @pytest.mark.parametrize("reply", ["这不是 JSON", "[]", "{}", "[1, 2]", "[{"])
    def test_unusable_replies_raise(self, reply: str) -> None:
        """Returning an empty plan would hide "the model did not do the task"
        behind "there is nothing to do"."""
        with pytest.raises(PlannerError):
            self._decompose(reply)

    def test_blank_goal_is_refused(self) -> None:
        with pytest.raises(PlannerError, match="目标不能为空"):
            LlmDecomposer(ScriptedLlm("[]"), max_steps=8).decompose("   ")

    def test_model_failure_becomes_a_planner_error(self) -> None:
        with pytest.raises(PlannerError, match="规划失败"):
            LlmDecomposer(ScriptedLlm(), max_steps=8).decompose("目标")


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class TestPlannerService:
    def test_plan_produces_a_validated_plan(self) -> None:
        service = _service(ScriptedLlm('[{"title": "读取配置", "action": "read_file"}]'))
        plan = service.plan("看看配置")
        assert plan.goal == "看看配置"
        assert [step.title for step in plan.steps] == ["读取配置"]
        assert plan.status is PlanStatus.DRAFT

    def test_plan_rejects_an_action_the_registry_does_not_have(self) -> None:
        service = _service(ScriptedLlm('[{"title": "a", "action": "rm_rf"}]'))
        with pytest.raises(PlannerError, match="不存在的工具"):
            service.plan("危险的事")

    def test_plan_before_start_raises(self) -> None:
        section = _section()
        service = PlannerService(lambda: section, llm_provider=lambda: ScriptedLlm("[]"))
        with pytest.raises(PlannerError, match="未启动"):
            service.plan("x")

    def test_plan_when_disabled_raises(self) -> None:
        service = _service(ScriptedLlm("[]"), enabled=False)
        with pytest.raises(PlannerError, match="未启用"):
            service.plan("x")

    def test_plan_without_a_model_says_so(self) -> None:
        """A text-only install still runs; it just cannot plan."""
        service = _service(None)
        with pytest.raises(PlannerError, match="需要一个大模型"):
            service.plan("x")

    def test_mark_updates_one_step(self) -> None:
        service = _service(ScriptedLlm("[]"))
        plan = Plan(goal="g", steps=(_step("s1"),))
        updated = service.mark(plan, "s1", StepStatus.DONE, notes="完成")
        assert updated.step("s1").status is StepStatus.DONE  # type: ignore[union-attr]
        assert updated.step("s1").notes == "完成"  # type: ignore[union-attr]

    def test_mark_unknown_step_raises(self) -> None:
        service = _service(ScriptedLlm("[]"))
        with pytest.raises(PlannerError, match="没有这个步骤"):
            service.mark(Plan(goal="g", steps=(_step("s1"),)), "s9", StepStatus.DONE)

    def test_apply_outcome_records_success_and_failure(self) -> None:
        service = _service(ScriptedLlm("[]"))
        plan = Plan(goal="g", steps=(_step("s1"), _step("s2")))
        plan = service.apply_outcome(plan, StepOutcome("s1", ok=True, detail="读到 3 行"))
        plan = service.apply_outcome(plan, StepOutcome("s2", ok=False, error="文件不存在"))
        assert plan.step("s1").notes == "读到 3 行"  # type: ignore[union-attr]
        assert plan.step("s2").status is StepStatus.FAILED  # type: ignore[union-attr]
        assert plan.step("s2").notes == "文件不存在"  # type: ignore[union-attr]

    def test_a_failure_does_not_cascade_to_the_other_steps(self) -> None:
        """They stay PENDING and blocked, so a re-plan can decide what to do with
        them. Marking them SKIPPED would throw away what the re-planner needs."""
        service = _service(ScriptedLlm("[]"))
        plan = Plan(goal="g", steps=(_step("s1"), _step("s2")))
        plan = service.apply_outcome(plan, StepOutcome("s1", ok=False, error="boom"))
        assert plan.step("s2").status is StepStatus.PENDING  # type: ignore[union-attr]

    def test_ready_steps_delegates_to_the_plan(self) -> None:
        service = _service(ScriptedLlm("[]"))
        plan = Plan(goal="g", steps=(_step("s1"), _step("s2", depends_on=("s1",))))
        assert [step.step_id for step in service.ready_steps(plan)] == ["s1"]

    def test_replan_keeps_completed_steps_and_appends_new_ones(self) -> None:
        """Replaying a step that succeeded is worse than a failed plan: a step
        with a side effect would fire twice."""
        llm = ScriptedLlm('[{"title": "换个方式读配置", "action": "read_file"}]')
        service = _service(llm)
        plan = Plan(
            goal="看看配置",
            steps=(
                _step("s1", title="读取配置", status=StepStatus.DONE),
                _step("s2", title="解析配置", status=StepStatus.FAILED),
            ),
        )
        revised = service.replan(plan, "解析失败：编码不对")
        assert [step.title for step in revised.steps] == ["读取配置", "换个方式读配置"]
        assert revised.steps[0].status is StepStatus.DONE
        assert revised.revision == 2
        assert "解析配置" in llm.calls[0][-1].content

    def test_replan_that_returns_nothing_abandons_the_plan(self) -> None:
        """The prompt tells the model to answer ``[]`` when the goal has become
        impossible, so an empty re-plan is an answer, not a parse failure."""
        service = _service(ScriptedLlm("[]"))
        plan = Plan(goal="g", steps=(_step("s1", status=StepStatus.FAILED),))
        revised = service.replan(plan, "没救了")
        assert revised.status is PlanStatus.ABANDONED
        assert revised.abandoned is True

    def test_replan_without_a_failure_is_refused(self) -> None:
        service = _service(ScriptedLlm("[]"))
        plan = Plan(goal="g", steps=(_step("s1"),))
        with pytest.raises(PlannerError, match="不需要重新规划"):
            service.replan(plan, "")

    def test_abandon_keeps_the_steps_for_the_record(self) -> None:
        service = _service(ScriptedLlm("[]"))
        plan = Plan(goal="g", steps=(_step("s1"),))
        abandoned = service.abandon(plan, "用户取消了")
        assert abandoned.status is PlanStatus.ABANDONED
        assert len(abandoned.steps) == 1
        assert abandoned.rationale == "用户取消了"

    def test_stats_shape(self) -> None:
        service = _service(ScriptedLlm("[]"))
        stats = service.stats()
        assert stats["running"] is True
        assert stats["enabled"] is True
        assert stats["has_model"] is True
        assert stats["known_tools"] == len(TOOLS)

    def test_stats_works_before_start(self) -> None:
        section = _section()
        service = PlannerService(lambda: section)
        assert service.stats()["running"] is False
        assert service.stats()["has_model"] is False

    def test_tool_name_provider_failure_does_not_break_planning(self) -> None:
        """A registry bug must degrade to "no tools listed", not to no planning."""

        def boom() -> Sequence[str]:
            raise RuntimeError("registry exploded")

        section = _section()
        service = PlannerService(
            lambda: section,
            llm_provider=lambda: ScriptedLlm('[{"title": "纯推理步骤"}]'),
            tool_names_provider=boom,
        )
        service.start()
        assert service.stats()["known_tools"] == 0
        assert len(service.plan("目标").steps) == 1


class TestLlmClientProtocol:
    def test_the_scripted_double_satisfies_the_protocol(self) -> None:
        client: LlmClient = ScriptedLlm("[]")
        assert client.provider_name == "scripted"


class TestToolNamesBlock:
    def test_no_tools_says_so_rather_than_listing_nothing(self) -> None:
        llm = ScriptedLlm('[{"title": "a"}]')
        LlmDecomposer(llm, max_steps=8).decompose("目标", tool_names=())
        assert "没有可用工具" in llm.calls[0][-1].content


class TestPlannerSectionValidation:
    def test_temperature_upper_bound(self) -> None:
        from jarvis.core.exceptions import ConfigurationError

        with pytest.raises(ConfigurationError, match="temperature"):
            PlannerSection.from_mapping({"enabled": True, "max_steps": 4, "temperature": 3.0})

    def test_temperature_can_be_null(self) -> None:
        section = PlannerSection.from_mapping(
            {"enabled": True, "max_steps": 4, "temperature": None}
        )
        assert section.temperature is None

    def test_unknown_key_is_refused(self) -> None:
        from jarvis.core.exceptions import ConfigurationError

        with pytest.raises(ConfigurationError, match="unknown key"):
            PlannerSection.from_mapping({"enabled": True, "max_steps": 4, "nope": 1})
