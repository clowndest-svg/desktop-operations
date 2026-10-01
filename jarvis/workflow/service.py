"""The workflow engine: run user-defined automations step by step.

A workflow is a list of steps plus, per step, a guard (``when``). Execution is
sequential and every step's outcome is written into a context the later guards
read, which is what makes "clean the disk *if* the report said it was low"
expressible in YAML.

Like the scheduler, this package never imports a concrete tool: ``action_runner``
is injected, so the engine can be tested with a two-line fake and stays free of
``jarvis.tools``.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Mapping
from pathlib import Path

from jarvis.config.schema import WorkflowSection
from jarvis.core.exceptions import WorkflowError
from jarvis.database import SqliteStore, format_timestamp, utc_now
from jarvis.scheduler import JobSpec as SchedulerJobSpec
from jarvis.scheduler import SchedulerService
from jarvis.scheduler import TriggerKind as SchedulerTriggerKind
from jarvis.workflow.conditions import ConditionEvaluator
from jarvis.workflow.loader import WorkflowLoader
from jarvis.workflow.store import MIGRATIONS, NAMESPACE, WorkflowRepository
from jarvis.workflow.types import (
    StepResult,
    StepSpec,
    TriggerKind,
    WorkflowDef,
    WorkflowRun,
)

logger = logging.getLogger("jarvis.workflow.service")

JOB_PREFIX: str = "workflow:"
"""Scheduler job ids are namespaced so a workflow cannot collide with a job."""


class WorkflowService:
    """Lifecycle component that loads, runs and records workflows."""

    name = "workflow"

    def __init__(
        self,
        store: SqliteStore,
        settings_provider: Callable[[], WorkflowSection],
        action_runner: Callable[[str, Mapping[str, object]], str],
        *,
        directory_provider: Callable[[], Path] | None = None,
        scheduler: SchedulerService | None = None,
    ) -> None:
        """Wire the engine.

        Args:
            store: Shared SQLite store (injected for ``:memory:`` tests).
            settings_provider: Reads the ``workflow`` config section lazily.
            action_runner: Executes ``(action, arguments)`` and returns a result
                string; raising signals failure.
            directory_provider: Resolves the definition folder. ``None`` falls
                back to the configured ``directory`` relative to the process CWD.
            scheduler: Registers ``trigger: cron`` workflows. ``None`` skips
                registration entirely, which is what tests use.
        """
        self._store = store
        self._settings_provider = settings_provider
        self._action_runner = action_runner
        self._directory_provider = directory_provider
        self._scheduler = scheduler
        self._repo = WorkflowRepository(store)
        self._conditions = ConditionEvaluator()
        self._definitions: dict[str, WorkflowDef] = {}
        self._started = False

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        """Load definitions and register cron workflows (idempotent)."""
        if self._started:
            return
        self._store.migrate(NAMESPACE, MIGRATIONS)
        self._started = True
        # Created here rather than in the loader: discovery must stay read-only,
        # but an operator needs somewhere to *put* a definition. Without this the
        # only way to learn the folder name was to read a warning on every boot.
        self._ensure_directory()
        definitions = self.reload()
        logger.info("workflow service started with %d definition(s)", len(definitions))

    def _ensure_directory(self) -> None:
        """Create the definitions folder, tolerating a read-only data root."""
        try:
            self._directory().mkdir(parents=True, exist_ok=True)
        except OSError as exc:  # pragma: no cover - depends on the filesystem
            logger.warning("could not create the workflow directory: %s", exc)

    def stop(self) -> None:
        """Stop the service. Registered jobs belong to the scheduler, not us."""
        self._started = False
        logger.info("workflow service stopped")

    @property
    def running(self) -> bool:
        """Whether the component has been started."""
        return self._started

    # -- definitions -------------------------------------------------------

    def reload(self) -> list[WorkflowDef]:
        """Re-read the definition directory and return what was loaded.

        Also re-syncs the scheduler when running, so editing a workflow's
        ``schedule`` and reloading takes effect without a restart.
        """
        definitions = self._loader().discover()
        self._definitions = {definition.name: definition for definition in definitions}
        if self._started:
            self._sync_schedule()
        return definitions

    def definitions(self) -> list[WorkflowDef]:
        """Currently loaded definitions. Never raises."""
        try:
            return list(self._definitions.values())
        except Exception:  # pragma: no cover - defensive
            logger.exception("listing workflow definitions failed")
            return []

    # -- execution ---------------------------------------------------------

    def run(self, name: str, *, variables: Mapping[str, object] | None = None) -> WorkflowRun:
        """Execute a workflow by name and record the run.

        Args:
            name: Workflow name as written in its YAML ``name`` field.
            variables: Optional values exposed to guards as ``{{ variables.x }}``.

        Returns:
            The recorded run, including per-step results.

        Raises:
            WorkflowError: if the workflow is unknown, exceeds ``max_steps``, or
                a step fails without ``continue_on_error`` (the failure is also
                recorded and returned in ``WorkflowRun.error``).
        """
        definition = self._definitions.get(name)
        if definition is None:
            # A workflow may have been dropped into the folder since the last
            # reload; one cheap re-scan beats telling the user to restart.
            self.reload()
            definition = self._definitions.get(name)
        if definition is None:
            raise WorkflowError(f"工作流不存在：{name}", details={"workflow": name})

        settings = self._settings_provider()
        if len(definition.steps) > settings.max_steps:
            raise WorkflowError(
                f"工作流步骤数 {len(definition.steps)} 超过上限 {settings.max_steps}",
                details={
                    "workflow": name,
                    "steps": len(definition.steps),
                    "max_steps": settings.max_steps,
                },
            )

        step_context: dict[str, dict[str, object]] = {}
        context: dict[str, object] = {
            "variables": dict(variables or {}),
            "steps": step_context,
            "workflow": definition.name,
        }
        started_at = format_timestamp(utc_now())
        results: list[StepResult] = []
        run_ok = True
        run_error = ""

        for step in definition.steps:
            result = self._run_step(step, context, step_context)
            results.append(result)
            if result.skipped:
                continue
            if not result.ok:
                run_ok = False
                run_error = result.error
                if not step.continue_on_error:
                    break

        finished_at = format_timestamp(utc_now())
        run_id = self._repo.record_run(
            workflow=name,
            started_at=started_at,
            finished_at=finished_at,
            ok=run_ok,
            steps=results,
            error=run_error,
        )
        if run_ok:
            logger.info("workflow %s finished ok (%d step(s))", name, len(results))
        else:
            logger.warning("workflow %s failed: %s", name, run_error)
        return WorkflowRun(
            run_id=run_id,
            workflow=name,
            started_at=started_at,
            finished_at=finished_at,
            ok=run_ok,
            steps=tuple(results),
            error=run_error,
        )

    def history(self, name: str | None = None, *, limit: int = 20) -> list[WorkflowRun]:
        """Recent runs, newest first. Never raises."""
        try:
            return self._repo.history(name, limit=limit)
        except Exception:
            logger.exception("reading workflow history failed")
            return []

    def stats(self) -> dict[str, object]:
        """Counters for the HUD's automation panel. Never raises."""
        payload: dict[str, object] = {
            "name": self.name,
            "running": self._started,
            "definitions": 0,
            "scheduled": 0,
            "runs": 0,
            "failures": 0,
        }
        try:
            definitions = self._definitions.values()
            payload["definitions"] = len(self._definitions)
            payload["scheduled"] = sum(
                1 for item in definitions if item.trigger is TriggerKind.CRON
            )
            payload["runs"] = self._repo.count_runs()
            payload["failures"] = self._repo.count_runs(ok=False)
        except Exception:  # pragma: no cover - defensive
            logger.exception("computing workflow stats failed")
        return payload

    # -- internals ---------------------------------------------------------

    def _loader(self) -> WorkflowLoader:
        return WorkflowLoader(self._directory())

    def _directory(self) -> Path:
        if self._directory_provider is not None:
            return self._directory_provider()
        return Path(self._settings_provider().directory)

    def _run_step(
        self,
        step: StepSpec,
        context: Mapping[str, object],
        step_context: dict[str, dict[str, object]],
    ) -> StepResult:
        """Evaluate the guard, run the action and publish the outcome.

        The outcome is stored under ``steps.<name>`` whether the step ran,
        skipped or failed, so a later guard can tell "not run yet" (missing key)
        from "ran and said no".
        """
        start = time.monotonic()
        if step.when:
            try:
                proceed = self._conditions.evaluate(step.when, context)
            except WorkflowError as exc:
                result = StepResult(
                    name=step.name,
                    skipped=False,
                    ok=False,
                    output="",
                    error=str(exc),
                    elapsed_ms=self._elapsed_ms(start),
                )
                step_context[step.name] = self._snapshot(result)
                return result
            if not proceed:
                result = StepResult(name=step.name, skipped=True, ok=True, output="")
                step_context[step.name] = self._snapshot(result)
                return result

        try:
            output = self._action_runner(step.action, step.arguments)
        except Exception as exc:
            result = StepResult(
                name=step.name,
                skipped=False,
                ok=False,
                output="",
                error=f"{type(exc).__name__}: {exc}",
                elapsed_ms=self._elapsed_ms(start),
            )
            logger.warning("workflow step %s failed: %s", step.name, result.error)
        else:
            result = StepResult(
                name=step.name,
                skipped=False,
                ok=True,
                output=output,
                elapsed_ms=self._elapsed_ms(start),
            )
        step_context[step.name] = self._snapshot(result)
        return result

    @staticmethod
    def _elapsed_ms(start: float) -> int:
        return int((time.monotonic() - start) * 1000)

    @staticmethod
    def _snapshot(result: StepResult) -> dict[str, object]:
        """The context view a later ``when`` sees for this step."""
        return {
            "ok": result.ok,
            "skipped": result.skipped,
            "output": result.output,
            "error": result.error,
        }

    def _sync_schedule(self) -> None:
        """Register every cron workflow with the injected scheduler."""
        scheduler = self._scheduler
        if scheduler is None:
            return
        settings = self._settings_provider()
        if not settings.enabled:
            logger.info("workflow scheduling disabled by config")
            return
        for definition in self._definitions.values():
            if definition.trigger is not TriggerKind.CRON:
                continue
            try:
                scheduler.add_job(
                    SchedulerJobSpec(
                        job_id=f"{JOB_PREFIX}{definition.name}",
                        name=definition.name,
                        action=definition.name,
                        arguments={},
                        trigger=SchedulerTriggerKind.CRON,
                        expression=definition.schedule,
                    )
                )
            except Exception as exc:
                logger.warning("registering workflow %s failed: %s", definition.name, exc)


__all__ = ["JOB_PREFIX", "WorkflowService"]
