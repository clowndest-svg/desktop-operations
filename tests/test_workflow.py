"""Tests for the workflow engine (jarvis.workflow).

The engine is exercised end to end with real YAML on ``tmp_path`` and a
recording ``action_runner``, so the tests cover the parts that are actually
risky: condition safety, guard-driven step skipping and error policy.
"""

from __future__ import annotations

import textwrap
from collections.abc import Mapping
from pathlib import Path
from typing import cast

import pytest

from jarvis.config.schema import WorkflowSection
from jarvis.core.exceptions import WorkflowError
from jarvis.database import SqliteStore
from jarvis.scheduler import JobSpec, SchedulerService
from jarvis.scheduler import TriggerKind as SchedulerTriggerKind
from jarvis.workflow import ConditionEvaluator, WorkflowLoader, WorkflowService

_MAIN = """
    name: 每日早报
    description: 早上汇总磁盘与系统状态
    trigger: cron
    schedule: "0 9 * * *"
    steps:
      - name: 检查磁盘
        action: system_report
        arguments: {}
      - name: 磁盘紧张时清理
        action: disk_scan
        when: "{{ steps.检查磁盘.output }} contains 低"
"""


def _section(**overrides: object) -> WorkflowSection:
    raw: dict[str, object] = {"enabled": True, "directory": "workflows", "max_steps": 10}
    raw.update(overrides)
    return WorkflowSection.from_mapping(raw)


def _store() -> SqliteStore:
    store = SqliteStore(Path(":memory:"), journal_mode="MEMORY")
    store.start()
    return store


def _write(directory: Path, filename: str, text: str) -> Path:
    path = directory / filename
    path.write_text(textwrap.dedent(text), encoding="utf-8")
    return path


class RecordingRunner:
    """An ``action_runner`` with scripted outputs and switchable failures."""

    def __init__(self, outputs: Mapping[str, str] | None = None) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.outputs = dict(outputs or {})
        self.failures: set[str] = set()

    def __call__(self, action: str, arguments: Mapping[str, object]) -> str:
        self.calls.append((action, dict(arguments)))
        if action in self.failures:
            raise RuntimeError(f"{action} 失败")
        return self.outputs.get(action, f"{action} 完成")


class FakeSchedulerService:
    """Records the ``JobSpec`` values the workflow service registers."""

    def __init__(self) -> None:
        self.specs: dict[str, JobSpec] = {}

    def add_job(self, spec: JobSpec) -> JobSpec:
        self.specs[spec.job_id] = spec
        return spec


def _service(
    directory: Path,
    *,
    runner: RecordingRunner | None = None,
    scheduler: FakeSchedulerService | None = None,
    **overrides: object,
) -> tuple[WorkflowService, RecordingRunner, FakeSchedulerService]:
    runner = runner or RecordingRunner()
    fake = scheduler or FakeSchedulerService()
    service = WorkflowService(
        _store(),
        lambda: _section(**overrides),
        runner,
        directory_provider=lambda: directory,
        scheduler=cast("SchedulerService", fake),
    )
    return service, runner, fake


def _daily_runner(output: str) -> RecordingRunner:
    """A runner whose ``system_report`` step says ``output``."""
    return RecordingRunner({"system_report": output})


class TestLoader:
    def test_parses_a_definition(self, tmp_path: Path) -> None:
        _write(tmp_path, "daily.yaml", _MAIN)
        definitions = WorkflowLoader(tmp_path).discover()
        assert len(definitions) == 1
        definition = definitions[0]
        assert definition.name == "每日早报"
        assert definition.trigger.value == "cron"
        assert definition.schedule == "0 9 * * *"
        assert [step.name for step in definition.steps] == ["检查磁盘", "磁盘紧张时清理"]
        assert definition.steps[1].when == "{{ steps.检查磁盘.output }} contains 低"

    def test_bad_yaml_is_skipped_not_fatal(self, tmp_path: Path) -> None:
        """One broken hand-edit must not take every other workflow offline."""
        _write(tmp_path, "good.yaml", _MAIN)
        _write(tmp_path, "broken.yaml", "name: [unclosed\n")
        definitions = WorkflowLoader(tmp_path).discover()
        assert [item.name for item in definitions] == ["每日早报"]

    def test_missing_required_field_is_skipped(self, tmp_path: Path) -> None:
        _write(tmp_path, "good.yaml", _MAIN)
        _write(tmp_path, "nameless.yaml", "description: 没有名字\nsteps: []\n")
        assert len(WorkflowLoader(tmp_path).discover()) == 1

    def test_cron_without_schedule_is_rejected(self, tmp_path: Path) -> None:
        path = _write(
            tmp_path,
            "bad.yaml",
            """
            name: 无计划
            trigger: cron
            steps:
              - name: 一步
                action: noop
            """,
        )
        with pytest.raises(WorkflowError):
            WorkflowLoader(tmp_path).load(path)

    def test_unknown_trigger_is_rejected(self, tmp_path: Path) -> None:
        path = _write(
            tmp_path,
            "bad.yaml",
            """
            name: 奇怪触发
            trigger: telepathy
            steps:
              - name: 一步
                action: noop
            """,
        )
        with pytest.raises(WorkflowError):
            WorkflowLoader(tmp_path).load(path)

    def test_step_without_action_is_rejected(self, tmp_path: Path) -> None:
        path = _write(
            tmp_path,
            "bad.yaml",
            """
            name: 缺动作
            steps:
              - name: 一步
            """,
        )
        with pytest.raises(WorkflowError):
            WorkflowLoader(tmp_path).load(path)

    def test_missing_directory_yields_nothing(self, tmp_path: Path) -> None:
        """First run has no folder yet; discovery must not raise."""
        assert WorkflowLoader(tmp_path / "nope").discover() == []


class TestConditions:
    def setup_method(self) -> None:
        self.evaluator = ConditionEvaluator()

    def test_comparison_operators(self) -> None:
        context: Mapping[str, object] = {"variables": {"count": 5, "mode": "fast"}}
        assert self.evaluator.evaluate("{{ variables.count }} > 3", context) is True
        assert self.evaluator.evaluate("{{ variables.count }} <= 3", context) is False
        assert self.evaluator.evaluate('{{ variables.mode }} == "fast"', context) is True
        assert self.evaluator.evaluate('{{ variables.mode }} != "slow"', context) is True

    def test_contains(self) -> None:
        context: Mapping[str, object] = {"steps": {"检查磁盘": {"output": "磁盘剩余 低"}}}
        assert self.evaluator.evaluate("{{ steps.检查磁盘.output }} contains 低", context) is True
        assert self.evaluator.evaluate("{{ steps.检查磁盘.output }} contains 高", context) is False

    def test_boolean_operators(self) -> None:
        context: Mapping[str, object] = {"steps": {"a": {"ok": True}, "b": {"ok": False}}}
        assert self.evaluator.evaluate("{{ steps.a.ok }} and not {{ steps.b.ok }}", context)
        assert self.evaluator.evaluate("{{ steps.b.ok }} or {{ steps.a.ok }}", context)
        assert not self.evaluator.evaluate("{{ steps.a.ok }} and {{ steps.b.ok }}", context)

    def test_missing_path_is_falsy(self) -> None:
        """A step that has not run yet is a missing key, not an error."""
        assert self.evaluator.evaluate("{{ steps.nothing.output }}", {}) is False

    def test_false_like_strings_are_falsy(self) -> None:
        assert self.evaluator.evaluate("{{ variables.x }}", {"variables": {"x": "false"}}) is False
        assert self.evaluator.evaluate("{{ variables.x }}", {"variables": {"x": "否"}}) is False

    def test_empty_expression_is_true(self) -> None:
        """An empty guard means "no guard", which must not skip the step."""
        assert self.evaluator.evaluate("   ", {}) is True

    def test_import_reference_resolves_to_nothing(self) -> None:
        """``__import__`` is just a dict key that is not there — never a call."""
        assert self.evaluator.evaluate('{{ __import__("os") }}', {}) is False

    def test_arbitrary_file_write_is_not_executed(self, tmp_path: Path) -> None:
        """The strongest form of the claim: prove no side effect happened."""
        marker = tmp_path / "pwned.txt"
        expression = '{{ __import__("pathlib").Path("' + str(marker) + '").write_text("x") }}'
        assert self.evaluator.evaluate(expression, {}) is False
        assert not marker.exists()

    @pytest.mark.parametrize(
        "expression",
        [
            'eval("1+1")',
            '__import__("os").system("echo pwned")',
            "{{ variables.x }} ; rm -rf /",
            "os.system('whoami')",
        ],
    )
    def test_call_syntax_is_rejected(self, expression: str) -> None:
        """Anything that looks like a call is a parse error, not a call."""
        with pytest.raises(WorkflowError):
            self.evaluator.evaluate(expression, {"variables": {"x": 1}})


class TestExecution:
    def test_steps_run_in_order(self, tmp_path: Path) -> None:
        _write(tmp_path, "daily.yaml", _MAIN)
        service, runner, _ = _service(tmp_path, runner=_daily_runner("磁盘充足"))
        service.start()
        service.run("每日早报")
        assert [action for action, _ in runner.calls] == ["system_report"]

    def test_guard_skips_a_step_when_false(self, tmp_path: Path) -> None:
        _write(tmp_path, "daily.yaml", _MAIN)
        service, runner, _ = _service(tmp_path, runner=_daily_runner("磁盘充足"))
        service.start()
        run = service.run("每日早报")
        assert run.ok is True
        assert [step.skipped for step in run.steps] == [False, True]
        assert runner.calls == [("system_report", {})]

    def test_guard_lets_a_step_run_when_true(self, tmp_path: Path) -> None:
        _write(tmp_path, "daily.yaml", _MAIN)
        service, runner, _ = _service(tmp_path, runner=_daily_runner("磁盘剩余 低"))
        service.start()
        run = service.run("每日早报")
        assert [action for action, _ in runner.calls] == ["system_report", "disk_scan"]
        assert run.ok is True

    def test_continue_on_error_true_runs_later_steps(self, tmp_path: Path) -> None:
        _write(
            tmp_path,
            "chain.yaml",
            """
            name: 容错链
            steps:
              - name: 会失败
                action: boom
                continue_on_error: true
              - name: 之后
                action: later
            """,
        )
        runner = RecordingRunner()
        runner.failures.add("boom")
        service, _, _ = _service(tmp_path, runner=runner)
        service.start()
        run = service.run("容错链")
        assert run.ok is False
        assert [action for action, _ in runner.calls] == ["boom", "later"]
        assert len(run.steps) == 2

    def test_continue_on_error_false_stops_the_run(self, tmp_path: Path) -> None:
        _write(
            tmp_path,
            "chain.yaml",
            """
            name: 严格链
            steps:
              - name: 会失败
                action: boom
              - name: 之后
                action: later
            """,
        )
        runner = RecordingRunner()
        runner.failures.add("boom")
        service, _, _ = _service(tmp_path, runner=runner)
        service.start()
        run = service.run("严格链")
        assert run.ok is False
        assert [action for action, _ in runner.calls] == ["boom"]
        assert len(run.steps) == 1
        assert "RuntimeError" in run.error

    def test_context_is_passed_to_later_guards(self, tmp_path: Path) -> None:
        """The whole feature: step 2 reacts to what step 1 actually said."""
        _write(
            tmp_path,
            "chain.yaml",
            """
            name: 上下文
            steps:
              - name: 探测
                action: probe
              - name: 反应
                action: react
                when: "{{ steps.探测.output }} contains 危险"
            """,
        )
        service, runner, _ = _service(tmp_path, runner=RecordingRunner({"probe": "发现 危险 文件"}))
        service.start()
        run = service.run("上下文")
        assert [action for action, _ in runner.calls] == ["probe", "react"]
        assert run.steps[0].output == "发现 危险 文件"

    def test_variables_are_exposed_to_guards(self, tmp_path: Path) -> None:
        _write(
            tmp_path,
            "vars.yaml",
            """
            name: 变量
            steps:
              - name: 条件步
                action: react
                when: '{{ variables.mode }} == "fast"'
            """,
        )
        service, runner, _ = _service(tmp_path)
        service.start()
        service.run("变量", variables={"mode": "fast"})
        assert [action for action, _ in runner.calls] == ["react"]

    def test_exceeding_max_steps_raises(self, tmp_path: Path) -> None:
        """The cap is a guard against a runaway definition, enforced before step 1."""
        _write(
            tmp_path,
            "long.yaml",
            """
            name: 很长
            steps:
              - name: 一
                action: a
              - name: 二
                action: b
              - name: 三
                action: c
            """,
        )
        service, _, _ = _service(tmp_path, max_steps=2)
        service.start()
        with pytest.raises(WorkflowError):
            service.run("很长")

    def test_unknown_workflow_raises(self, tmp_path: Path) -> None:
        service, _, _ = _service(tmp_path)
        service.start()
        with pytest.raises(WorkflowError):
            service.run("不存在")

    def test_malformed_guard_fails_the_step(self, tmp_path: Path) -> None:
        """A broken condition is a definition bug the user must see."""
        _write(
            tmp_path,
            "bad.yaml",
            """
            name: 坏条件
            steps:
              - name: 一步
                action: noop
                when: 'os.system("whoami")'
            """,
        )
        service, runner, _ = _service(tmp_path)
        service.start()
        run = service.run("坏条件")
        assert run.ok is False
        assert run.steps[0].ok is False
        assert runner.calls == []

    def test_run_is_recorded_in_history(self, tmp_path: Path) -> None:
        _write(tmp_path, "daily.yaml", _MAIN)
        service, _, _ = _service(tmp_path, runner=_daily_runner("磁盘充足"))
        service.start()
        run = service.run("每日早报")
        history = service.history("每日早报")
        assert [item.run_id for item in history] == [run.run_id]
        assert history[0].steps[0].name == "检查磁盘"


class TestScheduling:
    def test_cron_workflow_is_registered_with_the_scheduler(self, tmp_path: Path) -> None:
        _write(tmp_path, "daily.yaml", _MAIN)
        service, _, fake = _service(tmp_path)
        service.start()
        spec = fake.specs["workflow:每日早报"]
        assert spec.trigger is SchedulerTriggerKind.CRON
        assert spec.expression == "0 9 * * *"
        assert spec.action == "每日早报"

    def test_manual_workflow_is_not_registered(self, tmp_path: Path) -> None:
        _write(
            tmp_path,
            "manual.yaml",
            """
            name: 手动
            steps:
              - name: 一步
                action: noop
            """,
        )
        service, _, fake = _service(tmp_path)
        service.start()
        assert fake.specs == {}

    def test_no_scheduler_skips_registration(self, tmp_path: Path) -> None:
        """The engine must be usable standalone, which is what tests rely on."""
        _write(tmp_path, "daily.yaml", _MAIN)
        service = WorkflowService(
            _store(),
            lambda: _section(),
            RecordingRunner(),
            directory_provider=lambda: tmp_path,
        )
        service.start()
        assert [item.name for item in service.definitions()] == ["每日早报"]

    def test_disabled_config_skips_registration(self, tmp_path: Path) -> None:
        _write(tmp_path, "daily.yaml", _MAIN)
        service, _, fake = _service(tmp_path, enabled=False)
        service.start()
        assert fake.specs == {}

    def test_reload_registers_a_newly_added_workflow(self, tmp_path: Path) -> None:
        service, _, fake = _service(tmp_path)
        service.start()
        _write(tmp_path, "daily.yaml", _MAIN)
        service.reload()
        assert "workflow:每日早报" in fake.specs


class TestStatsAndHistory:
    def test_stats_counts_definitions_and_runs(self, tmp_path: Path) -> None:
        _write(tmp_path, "daily.yaml", _MAIN)
        _write(
            tmp_path,
            "manual.yaml",
            """
            name: 手动
            steps:
              - name: 一步
                action: noop
            """,
        )
        service, _, _ = _service(tmp_path, runner=_daily_runner("磁盘充足"))
        service.start()
        service.run("每日早报")
        stats = service.stats()
        assert stats["definitions"] == 2
        assert stats["scheduled"] == 1
        assert stats["runs"] == 1

    def test_history_is_scoped_and_limited(self, tmp_path: Path) -> None:
        _write(tmp_path, "daily.yaml", _MAIN)
        service, _, _ = _service(tmp_path, runner=_daily_runner("磁盘充足"))
        service.start()
        service.run("每日早报")
        service.run("每日早报")
        assert len(service.history("每日早报", limit=1)) == 1
        assert service.history("别的") == []

    def test_read_methods_never_raise_when_the_store_is_closed(self, tmp_path: Path) -> None:
        store = _store()
        service = WorkflowService(
            store,
            lambda: _section(),
            RecordingRunner(),
            directory_provider=lambda: tmp_path,
        )
        service.start()
        store.stop()
        assert service.history() == []
        assert service.stats()["runs"] == 0
