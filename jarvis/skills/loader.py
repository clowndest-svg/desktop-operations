"""读一个技能目录：`SKILL.md` 的前言与正文。

解析规则照 anthropics/skills 那批真文件（本轮从 GitHub 抓回来对着写的）：

```
---
name: canvas-design
description: 一句话说这是什么、什么时候该用
license: …
---
正文（给模型看的说明书）
```

三条硬规矩，都是为了"坏东西不许静默变成能力缺口或惊喜"：

1. **前言解析失败 / 缺必填字段 → 这一份带着错误进列表**，不是跳过。跳过 = 用户装了个东西
   而界面说"你什么都没装"，而盘上那份还在影响提示（本仓库反复在治的那一类失败）。
2. **正文超大不当技能读**（见 `MAX_BODY_CHARS` 的理由）。截一半的说明书比没有更坏。
3. 读盘只读 `SKILL.md` 这一个文件，**绝不跟随软链接、绝不读目录外的路径**：这一层是数据，
   而数据层最容易被一个精心构造的目录变成任意文件读取。
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

import yaml

from jarvis.config.paths import AppPaths
from jarvis.core.constants import DEFAULT_ENCODING
from jarvis.skills.types import MAX_BODY_CHARS, SkillManifest

logger = logging.getLogger(__name__)

SKILL_FILENAME = "SKILL.md"
"""每个技能目录里那份说明书的文件名（社区惯例，Qoder/Claude/Codex 同一个）。"""

_SLUG = re.compile(r"[^a-z0-9._-]+")
_FRONTMATTER = re.compile(r"\A---[ \t]*\r?\n(.*?)\r?\n---[ \t]*\r?\n?", re.DOTALL)


class SkillParseError(ValueError):
    """这份 SKILL.md 不合规矩，原因要能直接上屏。"""


def slugify(raw: str, fallback: str = "skill") -> str:
    """把任意作者起的名字压成一个安全、稳定的目录名。

    必须**先剥掉任何路径成分**再消毒：技能目录名来自远端仓库，`..%2F..%2Fwindows` 这种
    东西一旦被当目录名用，装技能就变成往任意位置写文件。分隔符、点、纯空格都单独处理，
    消毒后为空就退回 `fallback`，不拿空串当目录名（那会解回父目录）。
    """
    text = str(raw or "").strip().replace("\\", "/").rsplit("/", 1)[-1]
    cleaned = _SLUG.sub("-", text.lower()).strip("-.")
    return cleaned[:64] or fallback


def read_directory(directory: Path) -> SkillManifest:
    """读一个目录里的 `SKILL.md`。

    Raises:
        SkillParseError: 文件不在、不是 UTF-8 可解、前言不是映射、缺 `name`/`description`，
            或正文超过 :data:`MAX_BODY_CHARS`。
    """
    if directory.is_symlink():
        raise SkillParseError(f"技能目录是软链接，拒绝读取：{directory}")
    skill_file = directory / SKILL_FILENAME
    if not skill_file.is_file():
        raise SkillParseError(f"目录里没有 {SKILL_FILENAME}：{directory.name}")
    if skill_file.is_symlink():
        raise SkillParseError(f"{SKILL_FILENAME} 是软链接，拒绝读取：{directory.name}")
    try:
        text = skill_file.read_text(encoding=DEFAULT_ENCODING)
    except OSError as exc:
        raise SkillParseError(f"读不到 {SKILL_FILENAME}：{exc}") from exc

    match = _FRONTMATTER.match(text)
    if match is None:
        raise SkillParseError(
            f"{directory.name}：{SKILL_FILENAME} 开头没有 YAML 前言（--- 围起来的那段）"
        )
    body = text[match.end() :].strip()
    try:
        raw: object = yaml.safe_load(match.group(1))
    except yaml.YAMLError as exc:
        raise SkillParseError(f"{directory.name}：前言不是合法 YAML —— {exc}") from exc
    if not isinstance(raw, dict):
        raise SkillParseError(
            f"{directory.name}：前言必须是键值映射，读到的是 {type(raw).__name__}"
        )

    name = _text(raw.get("name")) or directory.name
    description = _text(raw.get("description"))
    if not description:
        # 没有说明的技能，模型永远不知道该在什么时候用它 —— 装了就等于没装，还占一行提示。
        # 这不算"格式小瑕疵"，所以是错误而不是空字符串。
        raise SkillParseError(f"{directory.name}：前言里缺 description（模型靠这句决定用不用它）")
    if len(body) > MAX_BODY_CHARS:
        raise SkillParseError(
            f"{directory.name}：正文有 {len(body)} 字，超过单份技能上限 {MAX_BODY_CHARS}。"
            "不截一半读进来 —— 半本说明书比没有更容易让模型照着做错。"
        )
    return SkillManifest(
        slug=directory.name,
        name=name,
        description=description,
        path=directory,
        body_chars=len(body),
        version=_text(raw.get("version")),
        license=_text(raw.get("license")),
        homepage=_text(raw.get("homepage")) or _text(raw.get("source")),
    )


def parse_skill_file(path: Path) -> str:
    """只要正文（`use_skill` 用）。找不到或不合规矩都抛 :class:`SkillParseError`。"""
    read_directory(path)
    text = (path / SKILL_FILENAME).read_text(encoding=DEFAULT_ENCODING)
    match = _FRONTMATTER.match(text)
    return text[match.end() :].strip() if match else text.strip()


def discover(root: Path) -> tuple[list[SkillManifest], list[tuple[str, str]]]:
    """扫一个目录，返回（读成功的技能，读失败的 (目录名, 原因)）。

    目录不存在**不是错误**：一个从没装过东西的机器就该是空的。
    """
    if not root.is_dir():
        return [], []
    good: list[SkillManifest] = []
    bad: list[tuple[str, str]] = []
    for child in sorted(root.iterdir()):
        if not child.is_dir():
            continue
        try:
            good.append(read_directory(child))
        except SkillParseError as exc:
            logger.warning("技能 %s 读不了：%s", child.name, exc)
            bad.append((child.name, str(exc)))
        except OSError as exc:  # pragma: no cover - 权限/IO 层面
            logger.warning("技能目录 %s 打不开：%s", child.name, exc)
            bad.append((child.name, f"目录打不开：{exc}"))
    return good, bad


def skills_root(paths: AppPaths) -> Path:
    """技能目录：`<数据根>/扩展/skills`。"""
    return paths.extensions_dir / "skills"


def _text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


__all__ = [
    "SKILL_FILENAME",
    "SkillParseError",
    "discover",
    "parse_skill_file",
    "read_directory",
    "skills_root",
    "slugify",
]
