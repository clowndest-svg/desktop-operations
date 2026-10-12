"""技能这一层：读得懂别人的 SKILL.md，坏的看得见，装完对话里真拿得到正文。

判据按三条硬规矩写（`jarvis/skills/__init__.py` 的模块注释）：

- **坏的那一份必须出现在列表里带着原因**，不是被发现时静默消失 —— 消失 = 用户以为自己没装，
  而盘上确实多了一份东西；
- **路径类输入一律当恶意**：技能目录名来自远端清单，`..\\..\\config` 一旦被当名字用，
  "装技能"就变成往数据根外面写、"删技能"就变成删数据根；
- **工具只在有技能时存在**（注册一条只能回"没有技能"的工具 = 本仓库反复要停止生产的那类失败）。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from jarvis.config.schema import ToolsSection
from jarvis.skills import (
    SkillManifest,
    SkillParseError,
    SkillService,
    discover,
    index_block,
    read_directory,
    slugify,
)
from jarvis.skills.loader import SKILL_FILENAME
from jarvis.skills.service import ENABLED_KEY, SKILL_SOURCE, USE_SKILL
from jarvis.tools.registry import ToolRegistry


def _registry() -> ToolRegistry:
    return ToolRegistry(
        lambda: ToolsSection(
            enabled=True,
            confirm_dangerous=True,
            allow_write=False,
            allow_shell=False,
            file_roots=(),
            max_result_chars=10_000,
        )
    )


CANVAS_DESIGN = """---
name: canvas-design
description: Create beautiful visual art in .png and .pdf documents using design philosophy. \
Use when the user asks for a poster or static design.
license: Complete terms in LICENSE.txt
---

These are instructions for creating design philosophies.

## Steps
1. Write the philosophy as .md
2. Express it on a canvas
"""


class _FakePrefs:
    """只认已注册键的偏好假体 —— 真的 `set()` 会拒绝未登记的键并返回 False。

    故意保留这条行为：r107 就是被"set 静默拒绝"坑过（表现是启停存不住，界面上没有任何原因）。
    """

    def __init__(self, registered: tuple[str, ...] = (ENABLED_KEY,)) -> None:
        self._values: dict[str, object] = {}
        self._registered = registered
        self.refused: list[str] = []

    def get(self, key: str, default: object = None) -> object:
        return self._values.get(key, default)

    def set(self, key: str, value: object) -> bool:
        if key not in self._registered:
            self.refused.append(key)
            return False
        self._values[key] = value
        return True


def _write_skill(root: Path, slug: str, text: str = CANVAS_DESIGN) -> Path:
    directory = root / slug
    directory.mkdir(parents=True, exist_ok=True)
    (directory / SKILL_FILENAME).write_text(text, encoding="utf-8")
    return directory


def _service(
    root: Path,
    registry: ToolRegistry | None = None,
    prefs: object | None = None,
) -> SkillService:
    return SkillService(
        directory_provider=lambda: root,
        registry_provider=(lambda: registry) if registry is not None else None,
        preferences=prefs,
    )


class TestReadingWhatOthersShip:
    def test_the_community_frontmatter_shape_parses(self, tmp_path: Path) -> None:
        directory = _write_skill(tmp_path, "canvas-design")
        manifest = read_directory(directory)
        assert manifest.name == "canvas-design"
        assert manifest.description.startswith("Create beautiful visual art")
        assert manifest.license == "Complete terms in LICENSE.txt"
        assert manifest.body_chars > 40 and not manifest.error

    def test_crlf_and_leading_blank_line_still_parse(self, tmp_path: Path) -> None:
        """下载来的文件带着 Windows 换行是常态，不是例外。"""
        directory = tmp_path / "crlf"
        directory.mkdir()
        (directory / SKILL_FILENAME).write_bytes(
            CANVAS_DESIGN.replace("\n", "\r\n").encode("utf-8")
        )
        assert read_directory(directory).name == "canvas-design"

    def test_a_skill_without_a_description_is_an_error_not_a_blank_row(
        self, tmp_path: Path
    ) -> None:
        """没说明 = 模型永远不知道该不该用它。装了就等于没装，还白占一行提示。"""
        directory = tmp_path / "silent"
        directory.mkdir()
        (directory / SKILL_FILENAME).write_text(
            "---\nname: silent\n---\n正文很厚，但没人知道什么时候该用。\n", encoding="utf-8"
        )
        with pytest.raises(SkillParseError, match="description"):
            read_directory(directory)

    def test_a_missing_frontmatter_is_named_not_guessed(self, tmp_path: Path) -> None:
        directory = tmp_path / "plain"
        directory.mkdir()
        (directory / SKILL_FILENAME).write_text("# 只是 markdown，没有前言\n", encoding="utf-8")
        with pytest.raises(SkillParseError, match="前言"):
            read_directory(directory)

    def test_an_oversized_body_is_refused_instead_of_being_read_in_halves(
        self, tmp_path: Path
    ) -> None:
        """半本说明书比没有更坏：模型会照那半本做完，而没有人知道少读了一半。"""
        directory = tmp_path / "huge"
        directory.mkdir()
        (directory / SKILL_FILENAME).write_text(
            "---\nname: huge\ndescription: 很大\n---\n" + "字" * 200_001, encoding="utf-8"
        )
        with pytest.raises(SkillParseError, match="不截一半"):
            read_directory(directory)

    def test_a_directory_without_a_skill_file_reports_which_name_is_missing(
        self, tmp_path: Path
    ) -> None:
        (tmp_path / "empty-dir").mkdir()
        with pytest.raises(SkillParseError, match=SKILL_FILENAME):
            read_directory(tmp_path / "empty-dir")

    def test_symlinks_are_refused_for_both_the_directory_and_the_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """这一层只读数据。跟软链接 = 一份"技能"可以把任何文件变成模型上下文。

        不写真软链：这台 Windows 建符号链接要特权（WinError 1314），真链那条会在没权限的机器上
        **报 error 而不是 fail**，看着像测试没问题。被测的是"拒绝"这个动作，所以换掉判定本身，
        最后恢复一次，证明正常目录仍然读得进来 —— 不是把所有读取都拒了。
        """
        directory = _write_skill(tmp_path, "trick")

        monkeypatch.setattr(Path, "is_symlink", lambda self: self.name == SKILL_FILENAME)
        with pytest.raises(SkillParseError, match="软链接"):
            read_directory(directory)

        monkeypatch.setattr(Path, "is_symlink", lambda self: self == directory)
        with pytest.raises(SkillParseError, match="软链接"):
            read_directory(directory)

        monkeypatch.undo()
        assert read_directory(directory).slug == "trick"

    def test_discover_keeps_the_good_ones_and_reports_the_bad_by_name(self, tmp_path: Path) -> None:
        _write_skill(tmp_path, "good")
        bad = tmp_path / "bad"
        bad.mkdir()
        (bad / SKILL_FILENAME).write_text("没有前言\n", encoding="utf-8")
        skills, broken = discover(tmp_path)
        assert [skill.slug for skill in skills] == ["good"]
        assert broken and broken[0][0] == "bad"

    def test_a_missing_directory_is_not_an_error(self, tmp_path: Path) -> None:
        """新装机的扩展目录本来就该是空的。"""
        assert discover(tmp_path / "不存在的目录") == ([], [])


class TestSlugIsTreatedAsHostileInput:
    @pytest.mark.parametrize(
        ("raw", "want"),
        [
            ("Canvas Design", "canvas-design"),
            ("../../windows/system32", "system32"),
            ("..%2f..%2fetc", "2f..-2fetc"),
            ("a/b/c/SKILL.md", "skill.md"),
            ("  ", "skill"),
            ("", "skill"),
            ("中文技能", "skill"),
            ("..", "skill"),
            ("ok_name.v1", "ok_name.v1"),
        ],
    )
    def test_path_parts_and_junk_never_survive_into_a_directory_name(
        self, raw: str, want: str
    ) -> None:
        made = slugify(raw)
        assert made == want
        assert "/" not in made and "\\" not in made and made not in {".", ".."}

    def test_long_names_are_bounded(self) -> None:
        assert len(slugify("x" * 400)) <= 64


class TestTheIndexThatGoesIntoThePrompt:
    def _row(self, slug: str, chars: int = 40) -> SkillManifest:
        return SkillManifest(
            slug=slug,
            name=slug,
            description="说" * chars,
            path=Path(f"/tmp/{slug}"),
            enabled=True,
        )

    def test_disabled_skills_are_not_advertised(self, tmp_path: Path) -> None:
        row = SkillManifest(
            slug="off", name="off", description="不该出现", path=tmp_path, enabled=False
        )
        assert index_block([row]) == ""

    def test_broken_skills_never_enter_the_prompt(self, tmp_path: Path) -> None:
        row = SkillManifest(
            slug="broken", name="broken", description="", path=tmp_path, error="坏了", enabled=True
        )
        assert index_block([row]) == ""

    def test_the_block_tells_the_model_to_read_the_body_before_acting(self) -> None:
        block = index_block([self._row("one")])
        assert "use_skill" in block and "不要凭这一行猜内容" in block

    def test_a_hundred_skills_do_not_put_a_hundred_lines_in_the_prompt(self) -> None:
        """4096 窗口的随包模型：工具表一个人就 4.7k token（r88 实测），索引必须有闸。

        两道闸分别钉：这里用**短说明**，让字符预算根本没机会触发 —— 否则测到的只是
        字符预算，条数那一道删掉也不会红（本轮变异就是这么抓出来的）。
        """
        from jarvis.skills.types import MAX_INDEX_ITEMS

        long_block = index_block([self._row(f"s{index}") for index in range(100)])
        assert len(long_block) <= 2200 and "没列进这一段" in long_block

        skinny = [
            SkillManifest(
                slug=f"s{index}",
                name=f"s{index}",
                description="短",
                path=Path(f"/tmp/s{index}"),
                enabled=True,
            )
            for index in range(100)
        ]
        rows = [line for line in index_block(skinny).splitlines() if line.startswith("- ")]
        assert len(rows) <= MAX_INDEX_ITEMS, "短说明绕过字符预算时，条数闸必须还挡住"
        assert "没列进这一段" in index_block(skinny)

    def test_clipping_says_it_clipped(self) -> None:
        from jarvis.skills.types import clip

        assert clip("短" * 500, 60).endswith("…")
        assert clip("正好", 60) == "正好"


class TestTheServiceAndItsTool:
    def test_a_fresh_install_registers_no_tool_at_all(self, tmp_path: Path) -> None:
        registry = _registry()
        service = _service(tmp_path, registry, _FakePrefs())
        service.start()
        assert USE_SKILL not in registry.names()
        service.stop()

    def test_with_one_skill_the_tool_exists_and_answers_the_real_body(self, tmp_path: Path) -> None:
        registry = _registry()
        prefs = _FakePrefs()
        _write_skill(tmp_path, "canvas-design")
        service = _service(tmp_path, registry, prefs)
        service.start()
        assert USE_SKILL in registry.names()
        result = registry.invoke(USE_SKILL, {"name": "canvas-design"})
        assert result.ok, result.error
        assert "Express it on a canvas" in result.output
        assert "name: canvas-design" not in result.output, "前言不该混进正文（它已经在索引里了）"

    def test_an_unknown_skill_name_comes_back_as_a_sentence_not_an_exception(
        self, tmp_path: Path
    ) -> None:
        """工具抛出去 = 模型只看到"调用失败"；回一句"已装的是这几个"才是它能改的东西。"""
        registry = _registry()
        _write_skill(tmp_path, "canvas-design")
        service = _service(tmp_path, registry, _FakePrefs())
        service.start()
        answer = registry.invoke(USE_SKILL, {"name": "没这份"})
        assert "没有这份技能" in answer.output and "canvas-design" in answer.output

    def test_a_missing_name_is_rejected_by_the_real_argument_check(self, tmp_path: Path) -> None:
        """走 registry.invoke 而不是直接调 handler：参数校验、策略、记账都在那条路上。"""
        registry = _registry()
        _write_skill(tmp_path, "canvas-design")
        _service(tmp_path, registry, _FakePrefs()).start()
        refused = registry.invoke(USE_SKILL, {})
        assert not refused.ok and "name" in (refused.error + refused.output)

    def test_removal_of_the_last_skill_unregisters_the_tool(self, tmp_path: Path) -> None:
        registry = _registry()
        service = _service(tmp_path, registry, _FakePrefs())
        _write_skill(tmp_path, "one")
        service.start()
        assert USE_SKILL in registry.names()
        (tmp_path / "one" / SKILL_FILENAME).unlink()
        (tmp_path / "one").rmdir()
        service.reload()
        assert USE_SKILL not in registry.names()

    def test_the_skill_tool_is_marked_safe_and_carries_its_own_source(self, tmp_path: Path) -> None:
        registry = _registry()
        _write_skill(tmp_path, "one")
        _service(tmp_path, registry, _FakePrefs()).start()
        spec = next(row for row in registry.specs() if row.name == USE_SKILL)
        assert spec.source == SKILL_SOURCE and spec.name.startswith("use_skill")
        assert str(spec.risk) in {"RiskLevel.SAFE", "safe"}

    def test_enabled_state_round_trips_through_the_preferences_object(self, tmp_path: Path) -> None:
        prefs = _FakePrefs()
        _write_skill(tmp_path, "one")
        service = _service(tmp_path, None, prefs)
        service.start()
        assert service.enabled_slugs() == ["one"], "装上就是启用的（按钮上写着这句）"
        state = service.set_enabled("one", False)
        assert state["error"] == "" and service.enabled_slugs() == []
        assert state["enabled"] == 0
        assert service.prompt_index() == ""

    def test_a_key_the_preferences_registry_does_not_know_about_fails_loudly(
        self, tmp_path: Path
    ) -> None:
        """真的 Preferences 会拒绝未登记键并只 log 一句 —— 假体必须留出这个失败面。"""
        prefs = _FakePrefs(registered=())
        _write_skill(tmp_path, "one")
        service = _service(tmp_path, None, prefs)
        service.start()
        state = service.set_enabled("one", False)
        assert "没写进偏好" in str(state["error"]) and prefs.refused == [ENABLED_KEY]

    def test_the_broken_skill_can_be_seen_but_not_enabled(self, tmp_path: Path) -> None:
        _write_skill(tmp_path, "good")
        bad = tmp_path / "bad"
        bad.mkdir()
        (bad / SKILL_FILENAME).write_text("没有前言\n", encoding="utf-8")
        service = _service(tmp_path, None, _FakePrefs())
        service.start()
        rows = {row.slug: row for row in service.installed()}
        assert rows["bad"].error and rows["good"].enabled
        state = service.set_enabled("bad", True)
        assert "读不了" in str(state["error"])

    def test_removing_needs_an_explicit_confirmation_and_stays_inside_the_root(
        self, tmp_path: Path
    ) -> None:
        service = _service(tmp_path, None, _FakePrefs())
        service.start()
        outside = tmp_path.parent / "别的东西"
        outside.mkdir(exist_ok=True)
        for slug in ("../../别的东西", "..", "", "带/斜杠", "..\\..\\x"):
            state = service.remove(slug, confirmed=True)
            assert state["error"], f"{slug!r} 本该被拒绝"
        assert outside.exists()
        assert tmp_path.exists(), "扩展根自己绝不能被删掉"

    def test_the_root_boundary_is_the_only_gate_when_the_name_looks_clean(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """上面那条试的名字全带斜杠，走的是"名字不合法"那一支，根边界判定一步没被走到。

        边界判定只在一种情形里是唯一的闸：名字干净、解析完却落在根外。真软链接在这台机器上
        要特权（WinError 1314），建不出来就会静默走正常分支，看着像测过了 —— 所以换掉
        ``resolve`` 本身，让根里一个真目录"指"到根外一份带文件的真目录。判据不是报错文案，
        是**外面那份还在**。
        """
        outside = tmp_path.parent / "根外的一份"
        outside.mkdir(exist_ok=True)
        (outside / "重要.txt").write_text("别动", encoding="utf-8")
        inside = tmp_path / "escape"
        inside.mkdir(parents=True, exist_ok=True)

        real_resolve = Path.resolve

        def fake_resolve(self: Path, *_a: object, **_k: bool) -> Path:
            if self.name == "escape":
                return outside
            return real_resolve(self)

        monkeypatch.setattr(Path, "resolve", fake_resolve)
        try:
            service = _service(tmp_path, None, _FakePrefs())
            state = service.remove("escape", confirmed=True)
        finally:
            monkeypatch.undo()

        assert state["error"], "解析到根外的目录被当成技能目录放行了"
        assert outside.is_dir() and (outside / "重要.txt").is_file(), "删掉了用户从没点过的东西"
        assert "扩展目录" in str(state["error"]), f"理由要点名边界：{state['error']}"

    def test_a_real_skill_stays_on_disk_without_an_explicit_confirmation(
        self, tmp_path: Path
    ) -> None:
        """上面那条试的是 `../别的东西` —— 那种名字先被"不许带斜杠"挡下了，
        确认这一道根本没被走到。所以这里拿一份**真装好的**技能来试：
        没有 confirmed 就不许少一个文件。
        """
        _write_skill(tmp_path, "keepme")
        service = _service(tmp_path, None, _FakePrefs())
        service.start()
        assert (tmp_path / "keepme").is_dir()
        refused = service.remove("keepme", confirmed=False)
        assert "确认" in str(refused["error"])
        assert (tmp_path / "keepme").is_dir(), "没确认就删掉了 = 用户删了个他没说要让删的东西"
        assert service.remove("keepme", confirmed=True)["error"] == ""
        assert not (tmp_path / "keepme").exists()

    def test_removal_actually_deletes_the_directory_and_leaves_the_others(
        self, tmp_path: Path
    ) -> None:
        _write_skill(tmp_path, "keep")
        _write_skill(tmp_path, "drop")
        service = _service(tmp_path, _registry(), _FakePrefs())
        service.start()
        state = service.remove("drop", confirmed=True)
        assert state["error"] == ""
        assert not (tmp_path / "drop").exists() and (tmp_path / "keep").exists()
        assert [row.slug for row in service.installed()] == ["keep"]

    def test_stats_always_carries_the_whole_shape(self, tmp_path: Path) -> None:
        """界面把 undefined 当"没这一格"就会整块不画 —— 用户于是永远看不见这条能力存在过。"""
        service = _service(tmp_path, None, _FakePrefs())
        service.start()
        stats = service.stats()
        for key in ("directory", "installed", "enabled", "broken", "index_chars", "error"):
            assert key in stats, f"少了 {key}"
        assert json.dumps(stats, ensure_ascii=False), "桥面要能序列化回页面"
