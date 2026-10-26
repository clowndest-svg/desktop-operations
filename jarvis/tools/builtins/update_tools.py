"""把"我这版是不是最新的"做成一条工具，而不是界面上多一颗按钮。

理由：这个产品的主入口是对话。买家想知道的是一句话，替他点开面板再找一颗按钮，
不如他直接问。工具回的那句话就是屏上会出现的话，所以措辞在这里判。

**这条工具只读、只发一个 GET、什么都不下载**。清单端点在作者自己的服务器上，
内容是一份静态 JSON（构建轮次、体积、SHA256、说明、下载地址）。

`tools` 这一层只许 import `config` 和 `core`（`test_architecture_layers` 逐包比对，
这条违规就是它抓出来的），所以取清单那件事由装配层从 `jarvis.app.update_check`
传进来 —— 和 `images`、`documents` 完全同一个形状，不是新规矩。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Final

from jarvis.tools.types import (
    PERMISSION_NETWORK,
    RiskLevel,
    ToolHandler,
    ToolSpec,
    object_schema,
    string_property,
)

Fetcher = Callable[..., Mapping[str, object]]
"""取一次发布清单，回一份 `ReleaseInfo` 形状的映射。由装配层注入。"""


def _words(info: Mapping[str, object]) -> str:
    """把清单那一份结果写成一句能直接上屏的话。

    在这里重写而不是 import `app.update_check.words`：分层规则不许 import。
    两份实现会漂 —— 兜住它们的是两边各自那几条措辞断言
    （`tests/test_app_update_check.py` 钉 app 那份，本模块的工具层测试钉这份）。
    """
    if not info.get("ok"):
        return f"没查到新版：{info.get('error') or '清单那边没给原因'}"
    if not info.get("newer"):
        if info.get("server_behind"):
            return (
                f"你手上这版（{info.get('current')}）比清单上的（{info.get('latest')}）还新 —— "
                "清单可能没同步，别降级，先把这句贴回去问作者。"
            )
        return f"你手上这版（{info.get('current')}）就是清单上最新的那版（{info.get('latest')}）。"
    try:
        size = float(info.get("size_mb") or 0)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        size = 0.0
    tail = f"，约 {size:.1f} MB" if size else ""
    url = str(info.get("url") or "")
    where = f" 下载地址：{url}" if url else ""
    return (
        f"有新版：{info.get('latest')}（你现在是 {info.get('current')}）{tail}。"
        f"软件不会自己下载也不会自己换 —— 这一步得你点头。{where}"
    )


_CHECK: Final[ToolSpec] = ToolSpec(
    name="check_update",
    description=(
        "问一次作者的发布清单：现在最新是哪一版、你手上这版是不是旧的、新版多大、"
        "SHA256 是多少、去哪儿拿。用户问「有没有新版本」「我这版旧不旧」「要不要重装」时用这一条。"
        "它只读：不会下载、不会自动安装、也不会替用户做任何升级动作 —— "
        "包有 10 GB，这一步必须人自己点头。查不到时把那句原因原样转出去，"
        "不要改口说「没有新版」。"
    ),
    parameters=object_schema(
        {
            "url": string_property(
                "要问的清单地址，留空用软件内置的那个（作者服务器上的 latest.json）"
            ),
        },
        required=(),
    ),
    risk=RiskLevel.SAFE,
    permissions=frozenset({PERMISSION_NETWORK}),
)


def build(fetch: Fetcher | None = None) -> list[tuple[ToolSpec, ToolHandler]]:
    """一条工具，或者什么都没有。

    `fetch=None` 就**不注册** —— 和生图、投屏同一条规矩：别留一条永远失败的路。
    真的取数由 `jarvis/__main__.py` 注入 `update_check.fetch_release`。
    """
    if fetch is None:
        return []

    def handler(arguments: Mapping[str, object]) -> str:
        raw = arguments.get("url")
        url = str(raw).strip() if isinstance(raw, str) and raw.strip() else ""
        return _words(fetch(url) if url else fetch())

    return [(_CHECK, handler)]


__all__ = ["Fetcher", "build"]
