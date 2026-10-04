"""P1-5: a cron workflow has to actually run when the clock gets there.

The break was one string. ``WorkflowService`` wrote the workflow's own name into the
job's ``action`` field, the scheduler handed that string to the composition root's
runner, and the runner looked it up in the tool registry -- where no workflow is
registered, because a workflow is not a tool. So every scheduled workflow failed at
fire time with ``未注册的工具``. Nobody saw it because the definitions folder held zero
files, and a bug that needs one YAML to surface is a bug that ships.

These tests run the *real* join: the real ``SchedulerService``, the real
``WorkflowService``, and the real ``_tool_action_runner`` from the composition root. A
hand-wired dict of handlers would have skipped the exact step that was broken.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import pytest

from jarvis.__main__ import _tool_action_runner
from jarvis.config.schema import SchedulerSection, ToolsSection, WorkflowSection
from jarvis.core.exceptions import JarvisError
from jarvis.database import SqliteStore
from jarvis.scheduler import SchedulerService
from jarvis.tools.registry import ToolRegistry
from jarvis.tools.types import ToolResult, ToolSpec
from jarvis.workflow import ACTION_PREFIX, WorkflowService
from tests.test_scheduler import FakeScheduler, _store

MORNING_REPORT = """name: 早报
description: 九点看一眼机器
trigger: cron
schedule: "0 9 * * *"
steps:
  - name: 看时间
    action: look
    arguments: {}
"""

BROKEN = """name: 坏步骤
trigger: manual
steps:
  - name: 不存在
    action: no_such_action
    arguments: {}
"""


def _tools_section() -> ToolsSection:
    return ToolsSection.from_mapping(
        {
            "enabled": True,
            "confirm_dangerous": True,
            "allow_write": False,
            "allow_shell": False,
            "file_roots": [],
            "max_result_chars": 4000,
        }
    )


class _Registry:
    """The real ``ToolRegistry``, narrowed to what the runner calls.

    ``look`` succeeds and ``no_such_action`` is simply absent -- which is what the old
    code turned into "未注册的工具" for *every* workflow, whatever its steps said.
    """

    def __init__(self) -> None:
        self.calls: list[str] = []
        self._registry = ToolRegistry(_tools_section)
        self._registry.register(
            ToolSpec(name="look", description="测试用的成功动作"),
            lambda _arguments: "看了",
        )

    def invoke(self, name: str, arguments: Any = None, *, confirmed: bool = False) -> ToolResult:
        del arguments, confirmed
        self.calls.append(name)
        return self._registry.invoke(name, {})


def _workflow_section(directory: Path) -> WorkflowSection:
    return WorkflowSection.from_mapping(
        {"enabled": True, "directory": str(directory), "max_steps": 12}
    )


def _scheduler_section() -> SchedulerSection:
    return SchedulerSection.from_mapping(
        {
            "enabled": True,
            "timezone": "Asia/Shanghai",
            "max_concurrent": 2,
            "misfire_grace_seconds": 60,
        }
    )


@pytest.fixture
def stack(tmp_path: Path) -> Iterator[dict[str, Any]]:
    directory = tmp_path / "workflows"
    directory.mkdir()
    (directory / "早报.yaml").write_text(MORNING_REPORT, encoding="utf-8")
    (directory / "坏步骤.yaml").write_text(BROKEN, encoding="utf-8")

    store: SqliteStore = _store()
    tools = _Registry()
    # Bound late, exactly as ``_register_capabilities`` does it: the runner needs the
    # engine to answer a ``workflow:`` action and the engine needs the runner to execute
    # its steps, so one of the two has to be a closure over a not-yet-built object.
    engine: dict[str, WorkflowService | None] = {"service": None}
    runner = _tool_action_runner(cast(Any, tools), lambda: engine["service"])
    fake = FakeScheduler()
    scheduler = SchedulerService(store, _scheduler_section, runner, scheduler=fake)
    scheduler.start()
    workflows = WorkflowService(
        store,
        lambda: _workflow_section(directory),
        runner,
        directory_provider=lambda: directory,
        scheduler=scheduler,
    )
    workflows.start()
    engine["service"] = workflows
    yield {
        "store": store,
        "tools": tools,
        "scheduler": scheduler,
        "fake": fake,
        "workflows": workflows,
    }
    store.stop()


class TestTheActionStringIsADispatcherNotAName:
    def test_a_cron_workflow_registers_under_the_prefixed_action(
        self, stack: dict[str, Any]
    ) -> None:
        jobs = {job.job_id: job for job in stack["scheduler"].list_jobs()}
        assert f"{ACTION_PREFIX}早报" in jobs or "workflow:早报" in jobs
        assert jobs["workflow:早报"].action == "workflow:早报"

    def test_the_bare_name_is_gone_because_that_was_the_bug(self, stack: dict[str, Any]) -> None:
        """Pinned separately: a bare action still reaches the registry and still gets
        refused, so if someone renames the prefix back this test is the one that says
        why it cannot come back."""
        jobs = {job.job_id: job for job in stack["scheduler"].list_jobs()}
        assert all(job.action != "早报" for job in jobs.values())

    def test_a_manual_workflow_registers_no_job_at_all(self, stack: dict[str, Any]) -> None:
        assert "workflow:坏步骤" not in {job.job_id for job in stack["scheduler"].list_jobs()}


class TestFiringActuallyRunsIt:
    def test_the_scheduled_tick_runs_every_step(self, stack: dict[str, Any]) -> None:
        stack["scheduler"]._run_job("workflow:早报")

        runs = stack["workflows"].history("早报", limit=1)
        assert runs and runs[0].ok, runs[0].error if runs else "没有运行记录"
        assert [step.name for step in runs[0].steps] == ["看时间"]
        assert stack["tools"].calls == ["look"]

    def test_the_failure_is_recorded_rather_than_swallowed(self, stack: dict[str, Any]) -> None:
        """A step that fails must land in the run's terminal state. A workflow that
        "ran" while a step refused is the silent-success failure this whole round is
        against."""
        stack["workflows"].run("坏步骤")
        runs = stack["workflows"].history("坏步骤", limit=1)
        assert runs and runs[0].ok is False and runs[0].error

    def test_an_unknown_workflow_fails_loudly_at_the_runner(self, stack: dict[str, Any]) -> None:
        engine = stack["workflows"]
        runner = _tool_action_runner(cast(Any, stack["tools"]), lambda: engine)
        with pytest.raises(JarvisError, match="工作流不存在"):
            runner("workflow:没这个东西", {})

    def test_a_runner_without_a_workflow_service_says_so_instead_of_calling_a_tool(
        self,
    ) -> None:
        """The prefix must never fall through to the registry: ``workflow:早报`` looked
        up as a tool name is exactly the old bug, just with a longer string."""
        runner = _tool_action_runner(cast(Any, _Registry()))
        with pytest.raises(JarvisError, match="没有工作流服务"):
            runner("workflow:早报", {})

    def test_a_plain_action_still_goes_to_the_tool_registry(self, stack: dict[str, Any]) -> None:
        """The other half of the same string: prefixing must not eat ``system_report``."""
        runner = _tool_action_runner(cast(Any, stack["tools"]), lambda: stack["workflows"])
        assert runner("look", {}) == "看了"
