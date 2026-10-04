"""pyautogui adapter for the desktop-control service.

``pyautogui`` is optional (the ``computer`` extra) and imported lazily through
``importlib``: a machine that never enables desktop control must not need it, and
the failure surfaces as a precise :class:`ComputerControlError` naming the extra
to install rather than an ``ImportError`` from deep inside a click handler.
"""

from __future__ import annotations

import importlib
import logging
from collections.abc import Mapping
from typing import Any

from jarvis.computer.app_launcher import bring_to_front, launch_target
from jarvis.computer.types import ActionKind, PlannedAction
from jarvis.core.exceptions import ComputerControlError

logger = logging.getLogger("jarvis.computer.controller")


def _missing_dependency(exc: ImportError) -> ComputerControlError:
    return ComputerControlError(
        "桌面控制需要可选的 'pyautogui' 依赖（安装：pip install jarvis-assistant[computer]）",
        details={"missing_package": "pyautogui", "extra": "computer"},
    )


def _load_pyautogui() -> Any:
    """Import ``pyautogui`` lazily; tests stub this seam instead of the package."""
    try:
        return importlib.import_module("pyautogui")
    except ImportError as exc:
        raise _missing_dependency(exc) from exc


def _int_param(params: Mapping[str, object], key: str, default: int = 0) -> int:
    """Read an integer parameter, tolerating a numeric string from JSON."""
    value = params.get(key, default)
    if isinstance(value, (int, float, str)):
        return int(value)
    return default


def _float_param(params: Mapping[str, object], key: str, default: float = 0.0) -> float:
    """Read a float parameter, tolerating an int from JSON."""
    value = params.get(key, default)
    if isinstance(value, (int, float, str)):
        return float(value)
    return default


def _str_param(params: Mapping[str, object], key: str, default: str = "") -> str:
    """Read a string parameter, falling back when it is missing or mistyped."""
    value = params.get(key)
    return value if isinstance(value, str) else default


class PyAutoGuiController:
    """Drive the local pointer and keyboard through pyautogui."""

    @property
    def name(self) -> str:
        return "pyautogui"

    def execute(self, action: PlannedAction) -> None:
        """Perform ``action``. Raises :class:`ComputerControlError` on failure."""
        pyautogui = _load_pyautogui()
        params = action.params
        try:
            match action.kind:
                case ActionKind.LAUNCH:
                    pid = _int_param(params, "pid")
                    if pid and bring_to_front(pid):
                        return
                    target = _str_param(params, "target")
                    if not target:
                        raise ComputerControlError("没有可启动的路径，也没有可置前的窗口")
                    launch_target(target)
                case ActionKind.MOVE:
                    pyautogui.moveTo(_int_param(params, "x"), _int_param(params, "y"))
                case ActionKind.CLICK:
                    pyautogui.click(
                        x=_int_param(params, "x"),
                        y=_int_param(params, "y"),
                        clicks=2 if params.get("double") else 1,
                        button=_str_param(params, "button", "left"),
                    )
                case ActionKind.DOUBLE_CLICK:
                    pyautogui.doubleClick(_int_param(params, "x"), _int_param(params, "y"))
                case ActionKind.RIGHT_CLICK:
                    pyautogui.rightClick(_int_param(params, "x"), _int_param(params, "y"))
                case ActionKind.TYPE:
                    pyautogui.typewrite(_str_param(params, "text"))
                case ActionKind.KEY:
                    pyautogui.press(_str_param(params, "key"))
                case ActionKind.SCROLL:
                    pyautogui.scroll(_int_param(params, "amount"))
                case ActionKind.DRAG:
                    pyautogui.moveTo(_int_param(params, "x1"), _int_param(params, "y1"))
                    pyautogui.dragTo(
                        _int_param(params, "x2"),
                        _int_param(params, "y2"),
                        duration=_float_param(params, "duration"),
                        button=_str_param(params, "button", "left"),
                    )
        except Exception as exc:
            raise ComputerControlError(
                f"桌面操作执行失败：{action.description}",
                details={"kind": action.kind.value, "cause": repr(exc)},
            ) from exc
