"""The page's calls must survive how pywebview *generates* them.

``webview/js/api.js`` turns every exposed Python method into
``new Function(<那方法的参数名>, body)`` where the body says
``window.pywebview._jsApiCallback(...)``. So a parameter literally named ``window``
shadows the global inside the stub and the call dies with
``TypeError: Cannot read properties of undefined (reading '_jsApiCallback')`` --
while in the browser's mock mode the very same door answers perfectly.

That is exactly how r95's 「显示方式」 shipped dead: 136 service tests green, mock-mode
clicks green, and the packaged app could not call it at all. These two checks are what
would have said so before packaging.
"""

from __future__ import annotations

import inspect
import re
from pathlib import Path
from typing import Any

from jarvis.ui.desktop import HudBridge

BRIDGE_TS = Path(__file__).resolve().parents[1] / "frontend" / "src" / "api" / "bridge.ts"

#: Identifiers the generated stub body itself uses. A parameter with one of these names
#: replaces the stub's own binding, and the call breaks in the page, not in Python.
STUB_BODY_NAMES = frozenset({"window", "promise", "__id", "resolve", "reject", "arguments"})


def declared_methods() -> list[str]:
    """Every bridge method the TypeScript side declares it may call."""
    text = BRIDGE_TS.read_text(encoding="utf-8")
    start = text.index("interface PywebviewApi")
    body = text[start : text.index("\n}", start)]
    return re.findall(r"^  ([a-z_][a-z0-9_]*)\(", body, re.M)


def test_the_page_declares_a_bridge_surface() -> None:
    names = declared_methods()
    assert len(names) > 80, f"从 bridge.ts 里只认出 {len(names)} 扇门，八成是那个接口改了写法"


def test_every_declared_door_exists_on_the_bridge() -> None:
    """Typo or deleted door: the page gets ``undefined`` and pywebview never answers."""
    missing = [name for name in declared_methods() if not callable(getattr(HudBridge, name, None))]
    assert not missing, f"bridge.ts 声明了但这些门不在 HudBridge 上：{missing}"


def test_no_door_shadows_the_stub_body_names() -> None:
    offenders: list[tuple[str, list[str]]] = []
    for name in declared_methods():
        method = getattr(HudBridge, name, None)
        if method is None:
            continue
        params = list(inspect.signature(method).parameters)
        hit = [param for param in params if param in STUB_BODY_NAMES]
        if hit:
            offenders.append((name, hit))
    assert not offenders, (
        f"这些门的参数名会遮蔽 pywebview 生成的 stub 内部标识符：{offenders}；"
        "打包版里那一扇门根本调不动（浏览器 mock 看不出来）"
    )


def test_the_mirror_display_door_takes_a_mode_not_a_window() -> None:
    """r95 的那次事故就是这一格：参数名从 ``window`` 改成 ``window_mode``。"""
    params: dict[str, Any] = dict(inspect.signature(HudBridge.mirror_display).parameters)
    assert "window" not in params
    assert "window_mode" in params
