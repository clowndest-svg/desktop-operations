"""技能（SKILL.md）—— 一份说明书，不是一段会跑的代码。

这一层故意只碰**数据**：一个目录里一份 `SKILL.md`（YAML 前言 + 正文），前言说
「叫什么 / 什么时候该用」，正文是给模型看的操作说明书。装一个技能 = 往
`数据/扩展/skills/` 里放一个目录，启停它不会执行任何东西。

为什么不顺手支持"插件市场里一键装 Python 代码包"：那是从网上下代码再 import 进
助手自己的进程，等于给每台装了它的机器开一条远程代码执行通道，出了问题没人负责。
`jarvis/plugins/` 那套 `plugin.yaml` 仍然能用 —— 但它只能是**人自己往目录里放**的
一个可见动作，不是页面上一个搜索结果是。

包内只许依赖 `core` / `config` / `tools`（`docs/架构分层.md` 的表，
`tests/test_architecture_layers.py` 逐边校验）。
"""

from __future__ import annotations

from jarvis.skills.loader import (
    SkillParseError,
    discover,
    parse_skill_file,
    read_directory,
    slugify,
)
from jarvis.skills.service import SkillService
from jarvis.skills.types import (
    ENABLED_KEY,
    MAX_BODY_CHARS,
    MAX_INDEX_ITEMS,
    SkillManifest,
    index_block,
)

__all__ = [
    "ENABLED_KEY",
    "MAX_BODY_CHARS",
    "MAX_INDEX_ITEMS",
    "SkillManifest",
    "SkillParseError",
    "SkillService",
    "discover",
    "index_block",
    "parse_skill_file",
    "read_directory",
    "slugify",
]
