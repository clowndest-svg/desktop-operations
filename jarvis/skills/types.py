"""技能的数据形状，和"索引怎么写进提示"这一格算术。

`SkillManifest` 是**盘上声明的那份**（目录名 + SKILL.md 的前言），`index_block()` 是把若干份
压成一小段能进系统提示的文字。分开放是因为这两件事的约束完全不同：前者要宽容（别人的技能
目录里可能还带字体和 LICENSE），后者要吝啬 —— 随包那个 4B 只有 4096 的窗口，工具表一个人
就吃掉 4.7k token（r88 实测），提示里再塞十九份说明书就是把别的能力挤没。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Final

ENABLED_KEY: Final = "skills.enabled"
"""偏好里那个"启用了哪几份技能"的键（存 list of slug）。

放在这个纯数据模块里，是因为 `app/preferences.py` 的注册表要写同一个键名，而 L2 不许反过来
import L4：字符串在两处各写一遍迟早会漂（漂了的后果是 `set()` 静默拒绝 —— 装完的技能一刷新
就全灭）。它 import 的只有 `dataclasses`/`pathlib`，不会把 `jarvis.tools` 拖进配置层。
"""

MAX_BODY_CHARS: int = 200_000

"""一份 SKILL.md 正文最多读这么多字。

上限存在的理由不是"省内存"，是**一本写错的书会把整轮对话吃掉**：技能正文是要进上下文给
模型看的，一份 80 万字的说明书会让这一句除了"读完了"以外什么都干不了。超了不截一半 ——
截一半的说明书比没有更坏，模型会照那半本做。
"""

MAX_INDEX_ITEMS: int = 12
"""系统提示里最多列几条技能索引（名字 + 一句什么时候用它）。"""

MAX_DESCRIPTION_CHARS: int = 160
"""每条索引行里说明文字的硬截断。"""

MAX_INDEX_CHARS: int = 1800
"""整段索引的总预算。

三个数是一起定的：12 条 × 160 字 ≈ 1900 字 ≈ 600 token 上下，留了一条总闸 ——
万一某个作者的 description 写得像散文，超总闸时是**少列几条**，不是把每条削成两个字。
"""


@dataclass(frozen=True, slots=True)
class SkillManifest:
    """一个技能目录（`SKILL.md` 的前言部分）。"""

    slug: str
    """目录名，稳定 id：启停、删除、`use_skill` 都按它寻址。"""

    name: str
    """前言里作者写的名字。"""

    description: str
    """「这是什么、什么时候该用它」。空串会被显示成一句明确的话，不装作有。"""

    path: Path
    """技能目录。"""

    body_chars: int = 0
    """正文长度（字），用来在页面上说清这份说明书有多大。"""

    version: str = ""
    license: str = ""
    homepage: str = ""

    error: str = ""
    """读这份清单时的毛病（缺字段、正文超大……）。

    **坏的那一个技能仍然要出现在列表里带着它的错**，而不是被发现时直接消失 ——
    界面看不见 = 用户以为自己没装，而盘上确实有一份会进提示的东西。
    """

    enabled: bool = False
    """由偏好决定；装上新技能默认点亮（装这个动作本身就是人在按）。"""

    def to_dict(self) -> dict[str, object]:
        return {
            "slug": self.slug,
            "name": self.name,
            "description": self.description,
            "path": str(self.path),
            "body_chars": self.body_chars,
            "version": self.version,
            "license": self.license,
            "homepage": self.homepage,
            "error": self.error,
            "enabled": self.enabled,
        }


def clip(text: str, limit: int) -> str:
    """按字截断，并在真截过时**说明被截了**。

    截断必须留痕：页面上一个被削掉的句子和用户自己写的句子长得一模一样，
    而用户会拿削掉的那半句去判断这个技能能不能用。
    """
    clean = " ".join(str(text or "").split())
    if len(clean) <= limit:
        return clean
    return clean[: limit - 1].rstrip() + "…"


def index_block(skills: list[SkillManifest]) -> str:
    """把已启用的技能写成一段进系统提示的文字；没有就回空串。

    形状是「名字 — 什么时候用（看 use_skill 取正文）」。**正文一律不进提示**，那是
    `use_skill(name)` 按需取的 —— 渐进披露，和 Qoder / Claude 的技能同一个做法。
    """
    usable = [skill for skill in skills if skill.enabled and not skill.error]
    if not usable:
        return ""
    lines: list[str] = []
    used = 0
    for skill in usable[:MAX_INDEX_ITEMS]:
        description = clip(skill.description, MAX_DESCRIPTION_CHARS) or "（作者没写说明）"
        line = f"- {clip(skill.name, 60)}：{description}"
        if lines and used + len(line) > MAX_INDEX_CHARS:
            break
        lines.append(line)
        used += len(line)
    hidden = len(usable) - len(lines)
    tail = f"（另有 {hidden} 个已启用但太长没列进这一段）" if hidden > 0 else ""
    header = (
        "可用的技能（这些是操作说明书，不是工具）：需要用到某一份时，"
        "先调 use_skill 把那一份的正文读回来，再照着做；不要凭这一行猜内容。"
    )
    body = "\n".join(lines)
    return f"{header}\n{body}{tail}" if body else ""


__all__ = [
    "ENABLED_KEY",
    "MAX_BODY_CHARS",
    "MAX_DESCRIPTION_CHARS",
    "MAX_INDEX_CHARS",
    "MAX_INDEX_ITEMS",
    "SkillManifest",
    "clip",
    "index_block",
]
