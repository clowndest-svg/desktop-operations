"""Desktop control as a tool set the model can call.

The capability itself lives in :mod:`jarvis.computer`; this file only translates
"the assistant wants to click something" into that service and decides what the
model is allowed to ask for. Four rules, in order of how much they matter:

* **``confirmed`` is not a parameter.** The service has a confirmation gate for
  risky actions, and a tool that let the caller pass ``confirmed=true`` would make
  that gate decorative -- a model always passes it. Everything here goes through
  unconfirmed, so a dangerous action comes back as text naming the setting the
  human has to change.
* **Refusals are answers, not crashes.** A policy veto is the *designed* result of
  ``computer.allow_mouse=false``. The model needs to read it and tell the user, so
  it is returned as the tool result instead of raised.
* **Nothing is on by default.** ``enabled``, ``allow_mouse``, ``allow_keyboard`` and
  ``dry_run`` all start closed, so shipping these tools changes nothing until the
  operator opens each gate deliberately.
* **Coordinates are absolute pixels and the model cannot see the screen.** That is
  the honest limit: this is good for "打开开始菜单，输入，回车", and useless for
  "点那个蓝色按钮" until a screenshot reaches a vision model.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from typing import Final, Protocol

from jarvis.core.exceptions import JarvisError
from jarvis.tools.types import (
    RiskLevel,
    ToolHandler,
    ToolSpec,
    boolean_property,
    integer_property,
    object_schema,
    string_property,
)

logger = logging.getLogger("jarvis.tools.builtins.computer_tools")


class DesktopControl(Protocol):
    """What the tools need from a desktop-control service, and nothing more.

    Declared structurally instead of imported: ``tools`` is not allowed to depend
    on ``computer`` (the layering table in ``docs/architecture.md``, enforced by
    ``tests/test_architecture_layers.py``), and the composition root is the only
    place allowed to know both sides. A protocol keeps the type checking without
    the import -- the same seam ``ui/audio_bridge.py`` uses to read pipeline
    events it is not permitted to name.
    """

    def move(self, x: int, y: int) -> object: ...

    def click(self, x: int, y: int, *, button: str = "left", double: bool = False) -> object: ...

    def scroll(self, amount: int) -> object: ...

    def press_key(self, key: str) -> object: ...

    def type_text(self, text: str) -> object: ...

    def stats(self) -> dict[str, object]: ...


MAX_TYPED_CHARS: Final[int] = 400
"""Refuse to type a novel. Every character lands somewhere real, and a model that
dumps a page of context into a field has usually misunderstood the task."""

_BUTTONS: Final[frozenset[str]] = frozenset({"left", "right", "middle"})


def _describe(result: object) -> str:
    """One action's outcome, phrased so a model can decide what to do next."""
    if not bool(getattr(result, "ok", False)):
        return f"未执行：{getattr(result, 'detail', '') or '桌面控制器报告失败'}"
    if not bool(getattr(result, "executed", False)):
        # dry_run is where most operators start, and "done" would be a lie about a
        # cursor that never moved.
        return f"未真实执行（演练模式或需人工确认）：{getattr(result, 'detail', '')}"
    return f"已执行：{getattr(result, 'detail', '')}"


class _ArgumentRefusedError(Exception):
    """A bad argument, reported to the model as text rather than as a traceback."""


def _perform(service: DesktopControl, kind: str, arguments: Mapping[str, object]) -> object:
    if kind in ("move", "click"):
        x = arguments.get("x")
        y = arguments.get("y")
        if not isinstance(x, (int, float)) or not isinstance(y, (int, float)):
            raise _ArgumentRefusedError("x 和 y 必须是数字像素坐标。")
        if kind == "move":
            return service.move(int(x), int(y))
        button = str(arguments.get("button") or "left").strip().lower()
        if button not in _BUTTONS:
            raise _ArgumentRefusedError(f"不支持的按键：{button}（只认 left / right / middle）")
        return service.click(int(x), int(y), button=button, double=bool(arguments.get("double")))
    if kind == "scroll":
        amount = arguments.get("amount")
        if not isinstance(amount, (int, float)):
            raise _ArgumentRefusedError("amount 必须是数字。")
        return service.scroll(int(amount))
    if kind == "press":
        key = str(arguments.get("key") or "").strip()
        if not key:
            raise _ArgumentRefusedError("key 不能为空。")
        return service.press_key(key)
    text = str(arguments.get("text") or "")
    if not text.strip():
        raise _ArgumentRefusedError("text 不能为空。")
    if len(text) > MAX_TYPED_CHARS:
        raise _ArgumentRefusedError(
            f"要输入的文字超过 {MAX_TYPED_CHARS} 字，已拒绝；请拆成明确的几步。"
        )
    return service.type_text(text)


def _tool(service_factory: Callable[[], DesktopControl], kind: str) -> ToolHandler:
    """One action, with every failure mode turned into something the model can say."""

    def handler(arguments: Mapping[str, object]) -> str:
        try:
            return _describe(_perform(service_factory(), kind, arguments))
        except _ArgumentRefusedError as exc:
            return f"参数不对：{exc}"
        except JarvisError as exc:
            # The policy's own words are the useful part: they name the config key.
            logger.info("desktop control refused: %s", exc)
            return f"被安全策略拒绝：{exc}"
        except Exception as exc:
            logger.exception("desktop control failed unexpectedly")
            return f"桌面控制出错：{type(exc).__name__}: {exc}"

    return handler


_STATUS = ToolSpec(
    name="computer_status",
    description=(
        "查看桌面控制是否可用：总开关、演练模式、控制器、已执行与失败次数。"
        "要操作电脑之前先调这个；被拒之后也用它确认用户有没有打开开关。只读。"
    ),
    parameters=object_schema({}),
)


def _action(name: str, description: str, parameters: Mapping[str, object]) -> ToolSpec:
    """One tool that moves something on the real desktop.

    ``CAUTION``, not ``SAFE`` -- a click is not read-only -- and not ``DANGEROUS``
    either: the registry's dangerous gate asks the *caller* for a ``confirmed``
    argument, and no tool in this file may take one. The gate that does apply is the
    service's own (``computer.enabled`` / ``allow_mouse`` / ``allow_keyboard`` /
    ``dry_run``), and a refusal from it already comes back as text naming the key.
    """
    return ToolSpec(
        name=name, description=description, parameters=parameters, risk=RiskLevel.CAUTION
    )


def _status(service_factory: Callable[[], DesktopControl]) -> ToolHandler:
    def handler(arguments: Mapping[str, object]) -> str:
        del arguments
        stats = service_factory().stats()
        enabled = "已启用" if stats.get("enabled") else "未启用（computer.enabled=false）"
        return (
            f"桌面控制：{enabled}；"
            f"演练模式={'是（只报告不真的动）' if stats.get('dry_run') else '否'}；"
            f"控制器={stats.get('controller') or '未加载'}；"
            f"已执行 {stats.get('executed')} 次，失败 {stats.get('failed')} 次。"
        )

    return handler


def build(service_factory: Callable[[], DesktopControl]) -> list[tuple[ToolSpec, ToolHandler]]:
    """Return the desktop-control tools.

    ``service_factory`` is called per action rather than taken as a live service:
    the component may not have started when the registry is built, and a click that
    arrives before then should be refused by the policy with a reason, not crash the
    registry at import time.
    """
    return [
        (_STATUS, _status(service_factory)),
        (
            _action(
                "mouse_move",
                "把指针移动到屏幕绝对坐标 (x, y)，不点击。",
                object_schema(
                    {
                        "x": integer_property("横坐标，像素"),
                        "y": integer_property("纵坐标，像素"),
                    },
                    required=("x", "y"),
                ),
            ),
            _tool(service_factory, "move"),
        ),
        (
            _action(
                "mouse_click",
                "在屏幕绝对坐标 (x, y) 点击，可指定按键与是否双击。",
                object_schema(
                    {
                        "x": integer_property("横坐标，像素"),
                        "y": integer_property("纵坐标，像素"),
                        "button": string_property(
                            "left / right / middle，默认 left", default="left"
                        ),
                        "double": boolean_property("是否双击", default=False),
                    },
                    required=("x", "y"),
                ),
            ),
            _tool(service_factory, "click"),
        ),
        (
            _action(
                "mouse_scroll",
                "滚动滚轮。正数向上，负数向下。",
                object_schema({"amount": integer_property("滚动格数")}, required=("amount",)),
            ),
            _tool(service_factory, "scroll"),
        ),
        (
            _action(
                "key_press",
                (
                    "按一个键或组合键，例如 win、ctrl+s、alt+tab、enter、esc。"
                    "这是目前最有用的一条：不依赖看见屏幕就能打开开始菜单、"
                    "切换窗口、确认对话框。"
                ),
                object_schema({"key": string_property("键名或加号组合")}, required=("key",)),
            ),
            _tool(service_factory, "press"),
        ),
        (
            _action(
                "type_text",
                (
                    f"向当前焦点输入文字（不超过 {MAX_TYPED_CHARS} 字）。默认会被安全策略"
                    "拒绝，因为输入的内容很可能是密码或消息正文；被拒时把原因转达给用户，"
                    "不要反复重试。"
                ),
                object_schema({"text": string_property("要输入的文字")}, required=("text",)),
            ),
            _tool(service_factory, "type"),
        ),
    ]


__all__ = ["build"]
