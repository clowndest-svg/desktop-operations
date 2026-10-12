"""扩展市场：搜索、安装、启停，以及"她装了些什么"这一格。

这一层**只卖两种货**：技能（= 文本说明书，`jarvis/skills/` 那一层读）和 MCP server
（= 一条进程外或网络外的服务，`jarvis/mcp/` 那一层连）。`plugin.yaml` 那种 Python 代码包
不在市场里 —— 从网上下代码 import 进助手自己的进程，等于给每台装了它的机器开一条远程代码
执行通道。完整理由与这条边界的其他三处（搜索结果才允许 stdio、密钥不进扩展文件、
安装是人按的不是模型按的）在 `docs/扩展市场与技能方案.md` §4。

包内只许依赖 `core` / `config` / `tools` / `skills`（见 `docs/架构分层.md`）。
"""

from __future__ import annotations

from jarvis.extensions.installer import install_mcp, install_skill
from jarvis.extensions.runtime import detect, missing_for
from jarvis.extensions.servers import ServerStoreError, merge, path_for
from jarvis.extensions.service import ExtensionsService
from jarvis.extensions.sources import guarded_fetch, search_all
from jarvis.extensions.types import (
    CURATED_URL,
    KIND_MCP,
    KIND_SKILL,
    CatalogItem,
    InstallResult,
    SearchResult,
    SourceOutcome,
)

__all__ = [
    "CURATED_URL",
    "KIND_MCP",
    "KIND_SKILL",
    "CatalogItem",
    "ExtensionsService",
    "InstallResult",
    "SearchResult",
    "ServerStoreError",
    "SourceOutcome",
    "detect",
    "guarded_fetch",
    "install_mcp",
    "install_skill",
    "merge",
    "missing_for",
    "path_for",
    "search_all",
]
