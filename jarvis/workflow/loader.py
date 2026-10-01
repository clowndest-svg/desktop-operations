"""Load workflow definitions from a directory of YAML files.

The file format is deliberately flat and forgiving — a user edits these by hand:

.. code-block:: yaml

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
      - name: 通知
        action: notify
        arguments: { text: 早报完成 }
        continue_on_error: true

A malformed file is skipped with a warning rather than aborting discovery: one
bad hand-edit must not take every other workflow offline.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from pathlib import Path

import yaml

from jarvis.core.exceptions import WorkflowError
from jarvis.workflow.types import StepSpec, TriggerKind, WorkflowDef

logger = logging.getLogger("jarvis.workflow.loader")

_SUFFIXES: frozenset[str] = frozenset({".yaml", ".yml"})
_TRIGGERS: dict[str, TriggerKind] = {kind.value: kind for kind in TriggerKind}


def _require_str(data: Mapping[str, object], key: str, path: Path) -> str:
    """Read a required non-empty string field.

    Raises:
        WorkflowError: if the field is missing, empty or not a string.
    """
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise WorkflowError(
            f"工作流字段 {key!r} 必须是非空字符串",
            details={"file": path.name, "field": key},
        )
    return value.strip()


class WorkflowLoader:
    """Reads ``*.yaml``/``*.yml`` definitions from one directory."""

    def __init__(self, directory: Path) -> None:
        self._directory = directory

    @property
    def directory(self) -> Path:
        """The directory this loader scans."""
        return self._directory

    def discover(self) -> list[WorkflowDef]:
        """Parse every definition file, skipping the ones that fail.

        Never raises: a missing directory (first run) and a broken file both
        degrade to "fewer workflows" with a warning, because discovery runs at
        boot and must not be able to prevent the app from starting.
        """
        if not self._directory.is_dir():
            logger.warning("workflow directory does not exist: %s", self._directory)
            return []
        found: list[WorkflowDef] = []
        for path in self._candidate_files():
            try:
                found.append(self.load(path))
            except WorkflowError as exc:
                logger.warning("skipping workflow %s: %s", path.name, exc)
            except Exception:  # pragma: no cover - unexpected IO/YAML failure
                logger.exception("skipping unreadable workflow %s", path.name)
        return found

    def load(self, path: Path) -> WorkflowDef:
        """Parse one definition file.

        Raises:
            WorkflowError: if the file cannot be read or does not match the schema.
        """
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise WorkflowError(
                f"无法读取工作流文件：{path.name}", details={"file": str(path)}
            ) from exc
        try:
            raw = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            raise WorkflowError(
                f"工作流文件不是合法 YAML：{path.name}",
                details={"file": str(path), "reason": str(exc)},
            ) from exc
        if not isinstance(raw, Mapping):
            raise WorkflowError(
                "工作流文件顶层必须是映射（key: value）",
                details={"file": str(path)},
            )
        data: dict[str, object] = {str(key): value for key, value in raw.items()}
        return self._build(data, path)

    # -- internals ---------------------------------------------------------

    def _candidate_files(self) -> list[Path]:
        files = [
            path
            for path in self._directory.iterdir()
            if path.is_file() and path.suffix.lower() in _SUFFIXES
        ]
        return sorted(files, key=lambda item: item.name)

    def _build(self, data: Mapping[str, object], path: Path) -> WorkflowDef:
        name = _require_str(data, "name", path)
        description = data.get("description", "")
        trigger = self._parse_trigger(data.get("trigger", TriggerKind.MANUAL.value), path)
        schedule = data.get("schedule", "")
        if not isinstance(schedule, str):
            raise WorkflowError(
                "工作流字段 'schedule' 必须是字符串",
                details={"file": path.name},
            )
        steps = self._parse_steps(data.get("steps"), path)
        if trigger is TriggerKind.CRON and not schedule.strip():
            raise WorkflowError(
                "trigger 为 cron 的工作流必须提供 schedule",
                details={"file": path.name, "workflow": name},
            )
        return WorkflowDef(
            name=name,
            description=str(description),
            trigger=trigger,
            schedule=schedule.strip(),
            steps=steps,
            path=str(path),
        )

    @staticmethod
    def _parse_trigger(value: object, path: Path) -> TriggerKind:
        if not isinstance(value, str) or value.strip().lower() not in _TRIGGERS:
            raise WorkflowError(
                "工作流字段 'trigger' 必须是 manual / cron / event 之一",
                details={"file": path.name, "trigger": str(value)},
            )
        return _TRIGGERS[value.strip().lower()]

    def _parse_steps(self, value: object, path: Path) -> tuple[StepSpec, ...]:
        if (
            not isinstance(value, Sequence)
            or isinstance(value, (str, bytes, bytearray))
            or not value
        ):
            raise WorkflowError(
                "工作流必须包含至少一个步骤（steps 列表）",
                details={"file": path.name},
            )
        return tuple(self._parse_step(item, index, path) for index, item in enumerate(value))

    @staticmethod
    def _parse_step(value: object, index: int, path: Path) -> StepSpec:
        if not isinstance(value, Mapping):
            raise WorkflowError(
                f"第 {index + 1} 个步骤必须是映射",
                details={"file": path.name, "index": index},
            )
        step: dict[str, object] = {str(key): item for key, item in value.items()}
        name = _require_str(step, "name", path)
        action = _require_str(step, "action", path)
        arguments = step.get("arguments", {})
        if arguments is None:
            arguments = {}
        if not isinstance(arguments, Mapping):
            raise WorkflowError(
                f"步骤 {name!r} 的 arguments 必须是映射",
                details={"file": path.name, "step": name},
            )
        when = step.get("when", "")
        if when is None:
            when = ""
        if not isinstance(when, str):
            raise WorkflowError(
                f"步骤 {name!r} 的 when 必须是字符串",
                details={"file": path.name, "step": name},
            )
        continue_on_error = step.get("continue_on_error", False)
        if not isinstance(continue_on_error, bool):
            raise WorkflowError(
                f"步骤 {name!r} 的 continue_on_error 必须是布尔值",
                details={"file": path.name, "step": name},
            )
        return StepSpec(
            name=name,
            action=action,
            arguments={str(key): item for key, item in arguments.items()},
            when=when.strip(),
            continue_on_error=continue_on_error,
        )


__all__ = ["WorkflowLoader"]
