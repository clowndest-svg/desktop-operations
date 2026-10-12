"""``SkillService`` —— 把盘上的技能目录变成"她能看见、人能管"的一格能力。

三件事，都不执行任何代码：

1. **发现**：读 `<数据根>/扩展/skills/`，坏的那份带着原因进列表（不消失）；
2. **进提示**：已启用技能的「名字 + 什么时候用它」组成一小段索引（有硬上限），正文不进 ——
   模型要用的时候调 :func:`_use_skill_handler`，把那一份说明书读回来照着做；
3. **启停/删除**：偏好里存的是**启用清单**，所以删一个技能、装一个新技能都不动别的。

工具是在有技能可看的时候才注册的（`SKILL_SOURCE`），一个都不装时不占工具表；
删空之后注销。注册一条永远只能回"没有技能"的工具，是这个仓库反复被要求停止生产的那类失败。
"""

from __future__ import annotations

import logging
import shutil
from collections.abc import Callable, Mapping
from dataclasses import replace
from pathlib import Path
from typing import Final

from jarvis.skills.loader import SkillParseError, discover, parse_skill_file, slugify
from jarvis.skills.types import ENABLED_KEY, SkillManifest, index_block
from jarvis.tools import RiskLevel, ToolRegistry, ToolSpec
from jarvis.tools.types import object_schema, string_property

logger = logging.getLogger(__name__)

SKILL_SOURCE: Final = "skill:installed"
"""`ToolSpec.source` 的值。前缀 `skill:` 是路由那一侧认的标记（见 `tool_routing`）。"""

USE_SKILL = "use_skill"

_SPEC = ToolSpec(
    name=USE_SKILL,
    description=(
        "把某一份已安装技能的完整说明书读回来（系统提示里只有它的一行简介）。"
        "要照某个技能的做法办事时**先调这一条**，别凭那一行简介猜步骤；"
        "name 用索引里给的那个名字或目录名。技能不是工具，它读完还要你自己动手。"
    ),
    parameters=object_schema(
        {"name": string_property("技能的 name 或目录名（slug）")},
        required=("name",),
    ),
    risk=RiskLevel.SAFE,
    # 只读盘上的一份 markdown，不执行、不写、不出网。
    permissions=frozenset(),
    # source 必须自己写：默认值是 'builtin'，那样 unregister_source(SKILL_SOURCE) 就
    # 什么都摘不掉（删空技能之后工具还在表里），路由那一侧也认不出它是扩展带来的。
    source=SKILL_SOURCE,
)


class SkillService:
    """技能这一层的生命周期组件（`app.register` 认识的那形状）。"""

    name = "skills"

    def __init__(
        self,
        *,
        directory_provider: Callable[[], Path],
        registry_provider: Callable[[], ToolRegistry] | None = None,
        preferences: object | None = None,
        audit: Callable[[str], None] | None = None,
    ) -> None:
        """`directory_provider` 给技能根（`<数据根>/扩展/skills`，装配点算），
        `registry_provider` 给共享工具注册表；后者不在时这一层只读盘不注册工具。
        """
        self._directory_provider = directory_provider
        self._registry_provider = registry_provider
        self._preferences = preferences
        self._audit = audit
        self._skills: list[SkillManifest] = []
        self._broken: list[tuple[str, str]] = []
        self._started = False

    # -- 生命周期 ----------------------------------------------------------

    def start(self) -> None:
        self._rescan()
        self._started = True
        self._sync_tools()
        logger.info(
            "技能层就绪（%d 份可用、%d 份读不了、启用 %d 份）",
            len([s for s in self._skills if not s.error]),
            len(self._broken),
            len(self.enabled_slugs()),
        )

    def stop(self) -> None:
        self._unregister()
        self._started = False

    @property
    def running(self) -> bool:
        return self._started

    def reload(self) -> None:
        """装/删之后重新读盘并同步工具表。

        热更新是必要的：装了技能要重启才能用，用户会以为没装上 —— 而这一层没有任何
        句柄要清理（读的是文件），所以重载的代价只是重读目录。
        """
        self._rescan()
        self._sync_tools()

    # -- 查询 --------------------------------------------------------------

    def installed(self) -> list[SkillManifest]:
        """读成功的 + 读不了的（后者 `error` 有内容，`slug` 用目录名）。"""
        enabled = set(self.enabled_slugs())
        rows = [replace(skill, enabled=skill.slug in enabled) for skill in self._skills]
        broken = [
            SkillManifest(
                slug=name,
                name=name,
                description="",
                path=self.root / name,
                error=reason,
                enabled=False,
            )
            for name, reason in self._broken
        ]
        return rows + broken

    @property
    def root(self) -> Path:
        return Path(self._directory_provider())

    def enabled_slugs(self) -> list[str]:
        """启用清单 = 偏好里那份；**偏好从没写过这个键时，装上的就是启用的**。

        为什么默认"全亮"：装一个技能这个动作本身就是人在按按钮，装完还得再找一颗开关
        才会生效，表现就是"装了没用"—— 而这一类页面问题用户只会当成软件坏掉。
        关掉之后键里存的是剩下的那份（可能是空表），所以"全关"和"从没动过"仍然是两种状态，
        用 `None` 区分它们：`get(key, None)` 拿到 `None` 才是没写过。
        """
        getter = getattr(self._preferences, "get", None)
        if not callable(getter):
            return [skill.slug for skill in self._skills]
        raw = getter(ENABLED_KEY, None)
        if raw is None:
            return [skill.slug for skill in self._skills]
        return [str(item) for item in raw] if isinstance(raw, list) else []

    def prompt_index(self) -> str:
        """给系统提示的那一小段；没启用的东西时回空串（提示里一行都不加）。"""
        return index_block(self.installed())

    def body(self, name: str) -> str:
        """按 name 或 slug 取正文。取不到抛 :class:`SkillParseError`，让调用方决定怎么说。"""
        wanted = str(name or "").strip().lower()
        for skill in self._skills:
            if skill.slug.lower() == wanted or skill.name.lower() == wanted:
                return parse_skill_file(skill.path)
        known = "、".join(skill.slug for skill in self._skills) or "（一个都没装）"
        raise SkillParseError(f"没有这份技能：{name!r}。已装的是 {known}")

    def stats(self) -> dict[str, object]:
        """桥面与告警共用的一格快照。**永远回整份形状**，缺状态也回空列表而不是 None。"""
        rows = self.installed()
        return {
            "directory": str(self.root),
            "installed": [row.to_dict() for row in rows],
            "enabled": len([row for row in rows if row.enabled]),
            "broken": len([row for row in rows if row.error]),
            "index_chars": len(self.prompt_index()),
            "error": "",
        }

    # -- 操作（都从界面按下来，模型碰不到） --------------------------------

    def enable(self, slug: str) -> bool:
        """点亮某一份技能（装完就调用它）。

        为什么装完默认是亮的：装这个动作本身就是人在按按钮，装完还要再找一颗开关才生效，
        表现就是"装了没用"，而这一类页面问题用户只会当成软件坏掉。关掉的权力同样在这一页上，
        而且关一次就写一次偏好 —— 所以"人关掉的"和"从没动过"这两种状态在盘上是分得开的。
        """
        target = str(slug or "").strip()
        if not target:
            return False
        current = set(self.enabled_slugs())
        current.add(target)
        return self._write_enabled(sorted(current))

    def set_enabled(self, slug: str, on: object) -> dict[str, object]:
        target = str(slug or "").strip()
        rows = [row for row in self.installed() if row.slug == target]
        if not rows:
            return self._fail(f"没有这份技能：{target}")
        if rows[0].error:
            return self._fail(f"这份技能读不了，先修好它再启用：{rows[0].error}")
        current = set(self.enabled_slugs())
        if bool(on):
            current.add(target)
        else:
            current.discard(target)
        if not self._write_enabled(sorted(current)):
            return self._fail("启用状态没写进偏好文件（数据目录只读、满、或被别的程序占着）")
        self.reload()
        return self.stats()

    def remove(self, slug: str, *, confirmed: object) -> dict[str, object]:
        """删一个技能目录。**只**允许删 `<数据根>/扩展/skills/` 的直接子目录。

        没有 `confirmed` 就一口回绝：这一层是"人按的删除"，不是"看起来像删除的按钮"。
        边界检查走 `is_relative_to`，因为 `slug` 来自界面（也就是来自用户/远端清单），
        `..\\..\\config` 这种东西一旦当目录名拼上去，删除就变成删数据根。
        """
        if confirmed is not True:
            return self._fail("删除技能需要明确的确认（confirmed），这一步不会替你决定")
        target = str(slug or "").strip()
        if not target or target in {".", ".."} or "/" in target or "\\" in target:
            return self._fail(f"技能名不合法：{target!r}")
        root = self.root.resolve()
        victim = (root / target).resolve()
        if not victim.is_relative_to(root) or not victim.is_dir() or victim == root:
            return self._fail(f"那不是扩展目录里的一个技能目录：{target}")
        try:
            shutil.rmtree(victim)
        except OSError as exc:
            return self._fail(f"删不掉：{exc}")
        remaining = [row.slug for row in self.installed() if row.slug in set(self.enabled_slugs())]
        self._write_enabled(remaining)
        self._note(f"删掉技能 {target}（{victim}）")
        self.reload()
        return self.stats()

    def install_dir(self, slug: str) -> Path:
        """给安装器用的目标目录（会 mkdir）。名字在这里就被消毒。"""
        root = self.root
        root.mkdir(parents=True, exist_ok=True)
        return root / slugify(slug)

    # -- 内部 --------------------------------------------------------------

    def _rescan(self) -> None:
        root = self.root
        try:
            self._skills, self._broken = discover(root)
        except OSError as exc:  # pragma: no cover - 数据根整个读不动
            logger.warning("技能目录扫不了：%s", exc)
            self._skills, self._broken = [], []

    def _sync_tools(self) -> None:
        provider = self._registry_provider
        if provider is None:
            return
        registry = provider()
        self._unregister(registry)
        if not self._skills:
            return
        registry.register(_SPEC, self._use_skill_handler())

    def _unregister(self, registry: ToolRegistry | None = None) -> None:
        target = (
            registry
            if registry is not None
            else (self._registry_provider() if self._registry_provider is not None else None)
        )
        if target is None:
            return
        try:
            target.unregister_source(SKILL_SOURCE)
        except Exception:  # pragma: no cover - 注册表是我们自己的
            logger.warning("注销技能工具失败", exc_info=True)

    def _use_skill_handler(self) -> Callable[[Mapping[str, object]], str]:
        def handler(arguments: Mapping[str, object]) -> str:
            try:
                return self.body(str(arguments.get("name") or ""))
            except SkillParseError as exc:
                # 工具不许抛：抛了模型只看到"调用失败"，看不到"名字给错了、已装的是这几个"。
                return f"读不到那份说明书：{exc}"

        return handler

    def _write_enabled(self, slugs: list[str]) -> bool:
        setter = getattr(self._preferences, "set", None)
        if not callable(setter):
            return True  # 没接偏好 = 只读装配（测试与浏览器预览），不假装失败
        result = setter(ENABLED_KEY, list(slugs))
        return bool(result)

    def _note(self, text: str) -> None:
        if self._audit is not None:
            try:
                self._audit(text)
            except Exception:  # pragma: no cover - 记账不能拖住删除
                logger.warning("扩展审计写不进去：%s", text, exc_info=True)

    def _fail(self, reason: str) -> dict[str, object]:
        snapshot = self.stats()
        snapshot["error"] = reason
        logger.warning("技能操作没做成：%s", reason)
        return snapshot


__all__ = ["ENABLED_KEY", "SKILL_SOURCE", "USE_SKILL", "SkillService"]
