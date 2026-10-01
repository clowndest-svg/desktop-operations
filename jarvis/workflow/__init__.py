"""Workflow engine: user-defined automations.

Responsibility (delivered in phase 16):
    * Workflow models (triggers, conditions, actions), execution engine
      and the Workflow Designer backing ("every day at 9", "when boss
      mails, summarise", ...).

Allowed dependencies: ``core``, ``config``, ``scheduler``, ``tools``,
``database``.

The public surface is the service, the YAML loader, the safe condition
evaluator and the value types they exchange.
"""

from jarvis.workflow.conditions import ConditionEvaluator
from jarvis.workflow.loader import WorkflowLoader
from jarvis.workflow.service import JOB_PREFIX, WorkflowService
from jarvis.workflow.store import MIGRATIONS, NAMESPACE, WorkflowRepository
from jarvis.workflow.types import (
    StepResult,
    StepSpec,
    TriggerKind,
    WorkflowDef,
    WorkflowRun,
)

__all__ = [
    "JOB_PREFIX",
    "MIGRATIONS",
    "NAMESPACE",
    "ConditionEvaluator",
    "StepResult",
    "StepSpec",
    "TriggerKind",
    "WorkflowDef",
    "WorkflowLoader",
    "WorkflowRepository",
    "WorkflowRun",
    "WorkflowService",
]
