"""扩展市场这一层：搜索结果、落盘、运行环境，以及两条"不许把页面当命令输入框"的闸。

测试的分工写在这儿，因为容易写歪：

- **搜索不碰网**：opener 一律是假的。真联网那三条在 `test_extensions_live.py` 里按标记跑
  （那几条会在限流时自己跳，不拖整个套件下水）。
- **安装用真文件系统**（tmp_path），假的是"远端返回什么"。装出来的东西必须能被技能层
  自己读回来 —— 只测"写了几个字节"是测不出装坏的。
- **两条闸要往死里测**：① 不在搜索结果里的 id 装不了（否则桥面等于接受任意命令行）；
  ② 路径类输入（ref / 文件名 / server 键）不许带着 `..` 或盘符进来。
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import pytest

from jarvis.config.schema import McpSection, ToolsSection
from jarvis.extensions import ExtensionsService, detect, install_mcp, install_skill
from jarvis.extensions.installer import safe_ref, safe_relpath, skill_files
from jarvis.extensions.runtime import RuntimeState, missing_for
from jarvis.extensions.servers import (
    ServerStoreError,
    entries,
    load,
    merge,
    normalise_key,
    path_for,
    remove,
    resolve_env,
    save,
    set_entry_enabled,
    set_layer_enabled,
    upsert,
    validate,
)
from jarvis.extensions.sources import (
    SearchCache,
    curated_items,
    github_items,
    npm_items,
    search_all,
)
from jarvis.extensions.types import KIND_MCP, KIND_SKILL, CatalogItem
from jarvis.skills.loader import SKILL_FILENAME
from jarvis.skills.service import ENABLED_KEY, SkillService
from jarvis.tools.registry import ToolRegistry


def _runtime(present: list[str]) -> RuntimeState:
    """装一条之前只看 present/missing 这两格，其余按真实形状补齐，不偷懒塞半个 dict。"""
    return {
        "present": present,
        "paths": {one: f"/fake/{one}" for one in present},
        "labels": {},
        "missing": [],
        "error": "",
    }


SKILL_TEXT = "---\nname: poster\ndescription: 做海报用的说明书\n---\n\n# 步骤\n先想，再画。\n"


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


class _Prefs:
    def __init__(self) -> None:
        self.values: dict[str, Any] = {}

    def get(self, key: str, default: Any = None) -> Any:
        return self.values.get(key, default)

    def set(self, key: str, value: Any) -> bool:
        if key != ENABLED_KEY:
            return False
        self.values[key] = value
        return True


def _skills(root: Path) -> SkillService:
    return SkillService(
        directory_provider=lambda: root / "skills", registry_provider=None, preferences=_Prefs()
    )


class _FakeNet:
    """(url, timeout) → (最终地址, 正文)。按 URL 里的关键字配响应，认不出的地址直接失败。"""

    def __init__(self, routes: dict[str, object], *, final: str | None = None) -> None:
        self.routes = routes
        self.calls: list[str] = []
        self.final = final

    def __call__(self, url: str, timeout: float = 8.0) -> tuple[str, bytes]:
        self.calls.append(url)
        for needle, payload in self.routes.items():
            if needle in url:
                if isinstance(payload, Exception):
                    raise payload
                body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
                return (self.final or url, body)
        raise ValueError(f"假网络没配这个地址：{url[:70]}")


# ───────────────────────────────────────────────────────── 搜索结果的形状


class TestSourcesParseWhatRealRegistriesReturn:
    def test_the_curated_list_is_read_item_by_item(self) -> None:
        net = _FakeNet(
            {
                "latest-extensions.json": {
                    "items": [
                        {
                            "kind": "skill",
                            "id": "c1",
                            "name": "poster",
                            "summary": "做海报",
                            "install": {"repo": "a/b", "path": "skills/poster", "ref": "main"},
                            "homepage": "https://github.com/a/b",
                        },
                        {
                            "kind": "mcp",
                            "id": "c2",
                            "name": "fs",
                            "install": {"command": "npx"},
                            "needs": ["npx"],
                        },
                        {
                            "kind": "plugin",
                            "id": "c3",
                            "name": "会跑的代码",
                        },  # 市场不卖这种，直接丢
                        "不是对象",
                    ]
                }
            }
        )
        rows = curated_items(net)
        assert [row.id for row in rows] == ["c1", "c2"]
        assert rows[0].kind == KIND_SKILL and rows[0].needs == ()
        assert rows[1].needs == ("npx",)

    def test_a_curated_list_without_items_says_so(self) -> None:
        with pytest.raises(ValueError, match="items"):
            curated_items(_FakeNet({"latest-extensions.json": {"version": 1}}))

    def test_npm_only_keeps_packages_that_look_like_mcp_servers(self) -> None:
        """npm 的搜索是模糊的：`text=mcp` 能返回一堆名字里含 mcp 的无关包。

        装一个不是 MCP server 的包 = 起一个永远连不上的进程，那比搜不到更坏。
        """
        net = _FakeNet(
            {
                "registry.npmjs.org": {
                    "objects": [
                        {
                            "package": {
                                "name": "@modelcontextprotocol/server-filesystem",
                                "description": "fs",
                                "statistics": {"monthly": 1234},
                                "url": "https://x",
                                "version": "2026.1.0",
                            }
                        },
                        {"package": {"name": "lodash", "description": "helpers"}},
                    ]
                }
            }
        )
        rows = npm_items("filesystem", net)
        assert [row.name for row in rows] == ["@modelcontextprotocol/server-filesystem"]
        assert rows[0].install["command"] == "npx" and rows[0].needs == ("npx",)
        assert rows[0].downloads == 1234

    def test_github_flattens_a_skills_folder_into_installable_rows(self) -> None:
        net = _FakeNet(
            {
                "search/repositories": {
                    "items": [
                        {
                            "full_name": "anthropics/skills",
                            "description": "公共技能仓库",
                            "default_branch": "main",
                            "stargazers_count": 5000,
                        }
                    ]
                },
                "/contents/skills": [
                    {"name": "poster", "type": "dir"},
                    {"name": "resume", "type": "dir"},
                    {"name": "README.md", "type": "file"},
                ],
            }
        )
        rows = github_items("海报", net)
        assert len(rows) == 2
        assert rows[0].install["path"] == "skills/poster"
        assert rows[0].name == "anthropics/skills · poster"

    def test_a_repo_that_is_one_big_skill_is_kept_as_the_repo_row(self) -> None:
        net = _FakeNet(
            {
                "search/repositories": {"items": [{"full_name": "a/b", "default_branch": "main"}]},
                "/contents/skills": ValueError("404"),
                "/contents/plugins/skills": ValueError("404"),
                "/contents/.claude/skills": ValueError("404"),
                "/contents/?ref=main": [{"name": "SKILL.md", "type": "file"}],
            }
        )
        rows = github_items("", net)
        assert len(rows) == 1 and rows[0].install["path"] == ""

    def test_a_repo_with_no_standard_folder_still_shows_up_explaining_itself(self) -> None:
        """从列表里**消失**是最坏的：用户会以为搜索没做好，而实际是那个仓库形状不对。"""
        net = _FakeNet(
            {
                "search/repositories": {
                    "items": [
                        {"full_name": "a/b", "description": "随便什么", "default_branch": "main"}
                    ]
                },
                "/contents": ValueError("404"),
            }
        )
        rows = github_items("", net)
        assert rows and "没在里面找到标准形状的技能目录" in rows[0].summary

    def test_search_all_reports_each_source_separately_and_never_hides_a_failure(
        self,
    ) -> None:
        net = _FakeNet(
            {
                "latest-extensions.json": {
                    "items": [{"kind": "skill", "id": "c", "name": "n", "install": {}}]
                },
                "registry.npmjs.org": ValueError("那边回了 503"),
            }
        )
        found = search_all("随便搜", fetch=net)
        rows = {row.source: row for row in found.sources}
        assert rows["curated"].error == "" and len(rows["curated"].items) == 1
        assert "503" in rows["npm"].error
        assert rows["github"].error  # 没配假路由 = 也是失败，不许是"零结果"
        assert len(found.failures) == 2
        shape = found.to_dict()
        sources = cast("list[dict[str, object]]", shape["sources"])
        assert sources[0]["hits"] == 1 and "milliseconds" in sources[0]

    def test_an_empty_query_does_not_burn_the_github_quota(self) -> None:
        """GitHub 未认证只有 60 次/小时：空搜索词必须不打它。"""
        net = _FakeNet({"latest-extensions.json": {"items": []}})
        search_all("", fetch=net)
        assert not any("api.github.com" in call for call in net.calls)

    def test_the_cache_serves_the_same_query_inside_its_ttl(self) -> None:
        clock = [100.0]
        cache = SearchCache(ttl=300.0, clock=lambda: clock[0])
        from jarvis.extensions.types import SearchResult

        made = SearchResult(query="a", sources=(), items=())
        cache.put(("a", ()), made)
        assert cache.get(("a", ())) is made
        clock[0] += 301.0
        assert cache.get(("a", ())) is None

    def test_a_source_that_never_answers_is_cut_off_with_words_not_a_hang(self) -> None:
        def slow(_url: str, _timeout: float = 8.0) -> tuple[str, bytes]:
            time.sleep(2.0)
            raise AssertionError("不该走到这儿")

        started = time.monotonic()
        found = search_all("x", fetch=slow, budget_seconds=0.2)
        spent = time.monotonic() - started
        assert spent < 5.0, "预算不生效就会变成整页挂住"
        assert all(row.error for row in found.sources)
        assert any(
            "先不等它" in row.error for row in found.sources
        ), "超预算那路要写明是超时，不是零结果"


# ───────────────────────────────────────────────────────── servers.json


class TestTheServerStore:
    def test_a_missing_file_is_an_empty_store_not_an_error(self, tmp_path: Path) -> None:
        assert load(path_for(tmp_path)) == {"version": 1, "enabled": False, "servers": {}}

    def test_a_failed_save_leaves_the_previous_file_intact(self, tmp_path: Path) -> None:
        """原子写真正的判据：中途炸了，上一次那份还得读得出来。

        只断言"没留下 .partial"是断不出这件事的 —— 直接写目标文件也会留下同样的结果。
        """
        path = path_for(tmp_path)
        keep, _ = upsert(load(path), {"name": "keep", "command": "c"})
        save(path, keep)
        before = path.read_text(encoding="utf-8")

        real = Path.write_text

        def explode(self: Path, *args: object, **kwargs: object) -> int:
            if self.name.endswith(".partial"):
                raise OSError("磁盘满了")
            return int(real(self, *args, **kwargs))  # type: ignore[arg-type]

        monkey = pytest.MonkeyPatch()
        monkey.setattr(Path, "write_text", explode)
        try:
            with pytest.raises(OSError):
                save(path, upsert(load(path), {"name": "new", "command": "c"})[0])
        finally:
            monkey.undo()
        assert path.read_text(encoding="utf-8") == before, "半份写入毁掉了原来那份 = 整层读不了"

    def test_a_truncated_file_says_so_instead_of_pretending_nothing_is_installed(
        self, tmp_path: Path
    ) -> None:
        path = path_for(tmp_path)
        path.write_text('{"version": 1, "servers": {', encoding="utf-8")
        with pytest.raises(ServerStoreError, match="不是合法 JSON"):
            load(path)

    def test_writing_is_atomic_and_the_file_is_human_readable(self, tmp_path: Path) -> None:
        path = path_for(tmp_path)
        data, name = upsert(load(path), {"name": "fs", "command": "npx", "args": ["-y", "p"]})
        save(path, data)
        assert name == "fs" and not list(tmp_path.glob("*.partial"))
        assert '"fs"' in path.read_text(encoding="utf-8")

    def test_a_stdio_entry_without_a_command_is_refused_before_it_hits_disk(
        self, tmp_path: Path
    ) -> None:
        with pytest.raises(ServerStoreError):
            validate({"name": "坏的", "transport": "stdio", "command": ""})
        assert not path_for(tmp_path).exists()

    def test_an_unknown_transport_is_named_not_silently_coerced(self) -> None:
        with pytest.raises(ServerStoreError, match="传输方式"):
            validate({"name": "x", "transport": "carrier-pigeon", "command": "npx"})

    @pytest.mark.parametrize("raw", ["", "..", "  ", "...", "//"])
    def test_a_key_that_would_be_an_empty_tool_prefix_is_rejected(self, raw: str) -> None:
        with pytest.raises(ServerStoreError):
            normalise_key(raw)

    @pytest.mark.parametrize("raw", ["../etc", "My Server!", "有中文 server", "a/b/c"])
    def test_a_key_is_neutralised_into_something_that_cannot_carry_path_meaning(
        self, raw: str
    ) -> None:
        """保证是"出来的东西只能当名字用"，不是"我拒了"：空格转连字符是好事，路径段被吃掉是安全。"""
        made = normalise_key(raw)
        assert "/" not in made and ".." not in made and " " not in made
        assert made and made[0].isalnum()

    def test_reinstalling_replaces_the_whole_entry_including_its_env_list(
        self, tmp_path: Path
    ) -> None:
        data, _ = upsert(
            load(path_for(tmp_path)), {"name": "s", "command": "c", "env_from": ["OLD_KEY"]}
        )
        data, _ = upsert(data, {"name": "s", "command": "c", "env_from": ["NEW_KEY"]})
        row = entries(data)["s"]
        assert row["env_from"] == [
            "NEW_KEY"
        ], "重装还留着上一次的 key 名字，页面就会显示一个没在用的变量"

    def test_removal_reports_whether_it_actually_removed_anything(self, tmp_path: Path) -> None:
        data, _ = upsert(load(path_for(tmp_path)), {"name": "s", "command": "c"})
        merged, existed = remove(data, "s")
        assert existed and entries(merged) == {}
        _, again = remove(merged, "s")
        assert not again

    def test_toggling_survives_a_round_trip(self, tmp_path: Path) -> None:
        data, _ = upsert(load(path_for(tmp_path)), {"name": "s", "command": "c"})
        save(path_for(tmp_path), set_layer_enabled(set_entry_enabled(data, "s", False), True))
        back = load(path_for(tmp_path))
        assert back["enabled"] is True and entries(back)["s"]["enabled"] is False


class TestMergingIntoTheConfigSection:
    def _section(self, **fields: Any) -> McpSection:
        return McpSection(
            enabled=fields.get("enabled", False),
            connect_timeout_seconds=2.0,
            call_timeout_seconds=5.0,
            servers=fields.get("servers", {}),
        )

    def test_config_yaml_wins_on_a_name_collision(self, tmp_path: Path) -> None:
        """人手写下去的那条比市场装的那条更有理解释这台机器。"""
        from jarvis.config.schema import McpServerSection

        mine = McpServerSection(
            name="fs", transport="stdio", command="my-own", args=(), url="", env={}, enabled=True
        )
        data, _ = upsert(
            load(path_for(tmp_path)), {"name": "fs", "command": "npx", "args": ["-y", "p"]}
        )
        save(path_for(tmp_path), data)
        merged, _missing, error = merge(self._section(servers={"fs": mine}), path_for(tmp_path))
        assert error == ""
        assert merged.servers["fs"].command == "my-own"

    def test_enabling_one_installed_server_opens_the_layer(self, tmp_path: Path) -> None:
        data, _ = upsert(load(path_for(tmp_path)), {"name": "fs", "command": "npx"})
        save(path_for(tmp_path), set_layer_enabled(data, True))
        merged, _missing, _ = merge(self._section(), path_for(tmp_path))
        assert merged.enabled is True and "fs" in merged.servers

    def test_a_missing_env_variable_is_reported_and_not_invented(self, tmp_path: Path) -> None:
        data, _ = upsert(
            load(path_for(tmp_path)), {"name": "fs", "command": "npx", "env_from": ["NEEDED"]}
        )
        save(path_for(tmp_path), data)
        merged, missing, _ = merge(self._section(), path_for(tmp_path), environ={"OTHER": "x"})
        assert missing["fs"] == ["NEEDED"]
        assert merged.servers["fs"].env == {}

    def test_env_values_come_from_the_environment_never_from_the_file(self, tmp_path: Path) -> None:
        data, _ = upsert(
            load(path_for(tmp_path)), {"name": "fs", "command": "npx", "env_from": ["API_KEY"]}
        )
        save(path_for(tmp_path), data)
        text = path_for(tmp_path).read_text(encoding="utf-8")
        assert "sekret" not in text
        merged, missing, _ = merge(
            self._section(), path_for(tmp_path), environ={"API_KEY": "sekret"}
        )
        assert missing == {} and merged.servers["fs"].env == {"API_KEY": "sekret"}

    def test_a_broken_file_is_surfaced_instead_of_dropping_everything_silently(
        self, tmp_path: Path
    ) -> None:
        path_for(tmp_path).parent.mkdir(parents=True, exist_ok=True)
        path_for(tmp_path).write_text("{坏", encoding="utf-8")
        merged, _missing, error = merge(self._section(), path_for(tmp_path))
        assert error and merged.servers == {}

    def test_resolve_env_keeps_the_order_and_separates_present_from_absent(self) -> None:
        given, missing = resolve_env(("B", "A", "  "), {"A": "1", "B": "2"})
        assert given == {"A": "1", "B": "2"} and missing == []
        assert resolve_env(("NOPE",), {})[1] == ["NOPE"]


# ───────────────────────────────────────────────────────── 运行环境


class TestRuntimeIsCheckedNotAssumed:
    def test_this_interpreter_is_always_reported(self) -> None:
        state = detect()
        assert "this-runtime" in state["present"] and state["error"] == ""

    def test_missing_list_names_things_that_are_not_present(self) -> None:
        state = detect()
        assert set(state["missing"]).isdisjoint(set(state["present"]))

    def test_missing_for_is_the_only_thing_the_installer_consults(self) -> None:
        assert missing_for(("npx", "uvx"), ["npx"]) == ["uvx"]
        assert missing_for((), ["npx"]) == []

    def test_a_need_that_is_present_is_not_reported_as_missing(self) -> None:
        """装一个已经在本机有的东西被拒 = 这一页彻底没法用，而且没人知道为什么。"""
        present = list(detect()["present"])
        assert missing_for(("python",), [*present, "python"]) == []


# ───────────────────────────────────────────────────────── 安装：技能


class TestSkillInstallWritesThingsTheSkillLayerCanRead:
    def _routes(self, **extra: object) -> _FakeNet:
        """按**完整路径**配响应，不给"任何 raw 地址都回同一份"的宽容。

        上一版这里写的是 `"raw.githubusercontent.com": SKILL_TEXT`，于是安装器把 SKILL.md
        的取回路径拼错（少了仓库里那一层子目录）也照样"装成功"，测试全绿 —— 真跑 GitHub 时
        那条地址是 404。**假体太宽容会造出假绿**，这一条是对着那次返工写的。
        """
        routes: dict[str, object] = {
            "/contents/skills/poster": [
                {"name": "SKILL.md", "type": "file"},
                {"name": "guide.md", "type": "file"},
                {"name": "evil.exe", "type": "file"},
            ],
            "/main/skills/poster/SKILL.md": SKILL_TEXT.encode(),
            "/main/skills/poster/guide.md": "随附说明".encode(),
            # 那个 .exe 也要**给得出内容**：假网络如果回不了它，"跳不跳过二进制"这条判据
            # 就变成在测"网络挂了"，装错了也照样绿。
            "/main/skills/poster/evil.exe": b"MZ-that-would-be-an-executable",
        }
        routes.update(extra)
        return _FakeNet(routes)

    def _item(self, **fields: Any) -> CatalogItem:
        return CatalogItem(
            kind=KIND_SKILL,
            id="github:a/b:poster",
            name="a/b · poster",
            summary="",
            source="github",
            install={"repo": "a/b", "ref": "main", "path": "skills/poster"},
            **fields,
        )

    def test_the_skill_lands_and_reads_back_through_the_real_parser(self, tmp_path: Path) -> None:
        skills = _skills(tmp_path)
        skills.start()
        result = install_skill(self._item(), skills, fetch=self._routes())
        assert result.ok, result.reason
        directory = tmp_path / "skills" / "poster"
        assert (directory / SKILL_FILENAME).is_file()
        assert (directory / "guide.md").is_file()
        rows = {row.slug: row for row in skills.installed()}
        assert rows["poster"].enabled and rows["poster"].body_chars > 0

    def test_binaries_in_the_repo_are_never_downloaded(self, tmp_path: Path) -> None:
        """技能应该只是说明书。"顺手把仓库里的文件都拉下来"是给执行留门。"""
        skills = _skills(tmp_path)
        skills.start()
        install_skill(self._item(), skills, fetch=self._routes())
        assert not (tmp_path / "skills" / "poster" / "evil.exe").exists()

    def test_a_skill_that_cannot_be_parsed_after_writing_is_removed(self, tmp_path: Path) -> None:
        skills = _skills(tmp_path)
        skills.start()
        result = install_skill(
            self._item(),
            skills,
            fetch=self._routes(**{"/main/skills/poster/SKILL.md": "没有前言" + chr(10)}),
        )
        assert not result.ok and "回滚" in result.reason
        assert not (tmp_path / "skills" / "poster").exists(), "半个技能是最难查的残留"

    def test_a_missing_skill_file_names_the_real_reason(self, tmp_path: Path) -> None:
        skills = _skills(tmp_path)
        skills.start()
        result = install_skill(
            self._item(),
            skills,
            fetch=_FakeNet({"/contents/skills/poster": [{"name": "README.md", "type": "file"}]}),
        )
        assert not result.ok and "SKILL.md" in result.reason

    def test_repo_and_ref_are_vetted_before_any_url_is_built(self) -> None:
        with pytest.raises(ValueError, match="仓库"):
            skill_files(
                CatalogItem(
                    kind=KIND_SKILL,
                    id="x",
                    name="x",
                    summary="",
                    source="github",
                    install={"path": "skills/a"},
                ),
                _FakeNet({}),
            )

    @pytest.mark.parametrize("bad", ["../../main", "a b;rm", "..", "main;curl x|sh"])
    def test_a_ref_that_is_not_a_branch_or_commit_is_rejected(self, bad: str) -> None:
        with pytest.raises(ValueError):
            safe_ref(bad)

    def test_a_real_commit_sha_still_passes(self) -> None:
        assert safe_ref("8f3a1b2c") == "8f3a1b2c"

    @pytest.mark.parametrize("bad", ["../../windows/a.md", "/etc/passwd", "C:/x.md", "", "."])
    def test_paths_that_would_escape_the_skill_dir_never_come_out_escaped(self, bad: str) -> None:
        """保证是"结果一定还在技能目录里"，不是"一定报错"。

        `../../windows/a.md` 被中和成 `windows/a.md` 是可以接受的（它写不出去）；
        带盘符和空路径必须直接拒 —— 那种东西一旦拼出目录，删技能就会删到别处。
        """
        try:
            made = safe_relpath(bad)
        except ValueError:
            return
        assert ".." not in made and not made.startswith("/") and ":" not in made
        assert made  # 绝不会解成"当前目录本身"

    def test_normal_nested_paths_survive(self) -> None:
        assert safe_relpath("./sub/dir/file.md") == "sub/dir/file.md"


# ───────────────────────────────────────────────────────── 安装：MCP


class TestMcpInstallRefusesWhatCannotRun:
    def test_missing_runtime_means_nothing_is_written(self, tmp_path: Path) -> None:
        item = CatalogItem(
            kind=KIND_MCP,
            id="npm:x",
            name="@a/b",
            summary="",
            source="npm",
            install={"transport": "stdio", "command": "npx", "args": ["-y", "@a/b"]},
            needs=("npx-not-a-real-command",),
        )
        result = install_mcp(item, tmp_path, environ={}, runtime=_runtime(["python"]))
        assert not result.ok and "npx-not-a-real-command" in result.reason
        assert not path_for(tmp_path).exists()

    def test_a_needed_env_variable_that_is_absent_refuses_the_install(self, tmp_path: Path) -> None:
        item = CatalogItem(
            kind=KIND_MCP,
            id="npm:x",
            name="svc",
            summary="",
            source="curated",
            install={"transport": "stdio", "command": "run", "args": [], "env_from": ["NEEDED"]},
        )
        result = install_mcp(item, tmp_path, environ={}, runtime=_runtime(["run"]))
        assert not result.ok and "NEEDED" in result.reason and "环境变量" in result.reason
        assert not path_for(tmp_path).exists()

    def test_a_good_entry_lands_with_its_source_and_note(self, tmp_path: Path) -> None:
        item = CatalogItem(
            kind=KIND_MCP,
            id="npm:x",
            name="svc",
            summary="做一件事",
            source="npm",
            install={"transport": "stdio", "command": "npx", "args": ["-y", "@a/b"]},
            homepage="https://npmjs.com/x",
        )
        result = install_mcp(item, tmp_path, environ={}, runtime=_runtime(["npx"]))
        assert result.ok, result.reason
        row = entries(load(path_for(tmp_path)))["svc"]
        assert row["source"] == "npm" and row["note"] == "做一件事" and row["command"] == "npx"

    def test_the_stored_file_never_contains_a_secret_value(self, tmp_path: Path) -> None:
        """密钥只进环境变量：文件里存的是**变量名**。

        这条不是洁癖：数据目录会被打包、被网盘同步、被用户截图发群里问问题。
        """
        item = CatalogItem(
            kind=KIND_MCP,
            id="npm:x",
            name="svc",
            summary="",
            source="curated",
            install={"transport": "stdio", "command": "run", "env_from": ["MY_KEY"]},
        )
        install_mcp(
            item, tmp_path, environ={"MY_KEY": "sk-abcdefghijklmnop"}, runtime=_runtime(["run"])
        )
        text = path_for(tmp_path).read_text(encoding="utf-8")
        assert "sk-abcdefghijklmnop" not in text and "MY_KEY" in text


# ───────────────────────────────────────────────────────── 服务门面


class TestTheFacadeKeepsThePageFromChoosingCommands:
    def _service(self, tmp_path: Path, net: _FakeNet) -> ExtensionsService:
        return ExtensionsService(
            skills=_skills(tmp_path),
            extensions_dir_provider=lambda: tmp_path,
            registry_provider=_registry,
            search_fetch=net,
            environ={},
            audit_file_provider=lambda: tmp_path / "extensions.jsonl",
        )

    def test_state_always_carries_the_whole_shape(self, tmp_path: Path) -> None:
        service = self._service(tmp_path, _FakeNet({}))
        service.start()
        state = service.state()
        for key in (
            "directory",
            "layer_enabled",
            "servers",
            "skills",
            "mcp",
            "runtime",
            "search",
            "store_error",
            "log",
            "error",
        ):
            assert key in state, f"少了 {key}：界面读到 undefined 就整块不画"

    def test_installing_an_id_that_was_never_searched_is_refused(self, tmp_path: Path) -> None:
        """桥面只接受"我刚搜出来的那条"。这是这一层最重要的一道闸。"""
        service = self._service(tmp_path, _FakeNet({}))
        service.start()
        state = service.install("npm:@evil/whatever")
        assert "不在刚才的搜索结果里" in str(state["error"])
        assert not path_for(tmp_path).exists()

    def test_install_after_a_search_uses_the_cached_spec_not_the_page(self, tmp_path: Path) -> None:
        net = _FakeNet(
            {
                "latest-extensions.json": {
                    "items": [
                        {
                            "kind": "mcp",
                            "id": "c1",
                            "name": "svc",
                            "summary": "s",
                            "install": {
                                "transport": "stdio",
                                "command": "the-real-command",
                                "args": [],
                            },
                        }
                    ]
                }
            }
        )
        service = self._service(tmp_path, net)
        service.start()
        service.search("")
        state = service.install("c1")
        assert path_for(tmp_path).exists()
        assert entries(load(path_for(tmp_path)))["svc"]["command"] == "the-real-command"
        assert state["install"]["ok"] is True  # type: ignore[index]

    def test_the_http_only_manual_add_rejects_non_http_and_private_hosts(
        self, tmp_path: Path
    ) -> None:
        service = self._service(tmp_path, _FakeNet({}))
        service.start()
        for url in ("ftp://x/y", "http://127.0.0.1:9/mcp", "http://192.168.1.1/mcp", ""):
            state = service.add_http_server("svc", url)
            assert state["error"], f"{url} 本该被拒"
        assert not path_for(tmp_path).exists()

    def test_a_good_http_url_lands_in_the_store(self, tmp_path: Path) -> None:
        service = self._service(tmp_path, _FakeNet({}))
        service.start()
        state = service.add_http_server("hosted", "https://example.com/mcp")
        assert state["error"] == ""
        row = entries(load(path_for(tmp_path)))["hosted"]
        assert row["transport"] == "http" and row["url"] == "https://example.com/mcp"

    def test_the_tool_reports_installed_things_in_a_sentence(self, tmp_path: Path) -> None:
        from jarvis.extensions.service import LIST_TOOL

        registry = _registry()
        service = ExtensionsService(
            skills=_skills(tmp_path),
            extensions_dir_provider=lambda: tmp_path,
            registry_provider=lambda: registry,
            search_fetch=_FakeNet({}),
            environ={},
        )
        service.start()
        assert LIST_TOOL in registry.names()
        answer = registry.invoke(LIST_TOOL, {})
        assert answer.ok, answer.error
        assert "技能" in answer.output and "MCP server" in answer.output

    def test_removal_needs_an_explicit_confirmation(self, tmp_path: Path) -> None:
        service = self._service(tmp_path, _FakeNet({}))
        service.start()
        service.add_http_server("hosted", "https://example.com/mcp")
        assert service.remove_server("hosted", confirmed=False)["error"]
        assert service.remove_server("hosted", confirmed=True)["error"] == ""
        assert entries(load(path_for(tmp_path))) == {}

    def test_the_audit_line_is_written_after_a_real_action(self, tmp_path: Path) -> None:
        service = self._service(tmp_path, _FakeNet({}))
        service.start()
        service.add_http_server("hosted", "https://example.com/mcp")
        rows = service.log_tail(5)
        assert rows and "http MCP" in str(rows[0]["event"])

    def test_mcp_status_is_read_through_the_injected_provider(self, tmp_path: Path) -> None:
        calls: list[str] = []

        def status() -> dict[str, object]:
            calls.append("asked")
            return {"enabled": True, "running": True, "servers": [], "errors": {"fs": "起不来"}}

        service = ExtensionsService(
            skills=_skills(tmp_path),
            extensions_dir_provider=lambda: tmp_path,
            mcp_status_provider=status,
            search_fetch=_FakeNet({}),
        )
        service.start()
        mcp = cast("dict[str, object]", service.state()["mcp"])
        assert mcp["errors"] == {"fs": "起不来"} and calls == ["asked"]

    def test_stop_unregisters_the_tool(self, tmp_path: Path) -> None:
        from jarvis.extensions.service import LIST_TOOL

        registry = _registry()
        service = ExtensionsService(
            skills=_skills(tmp_path),
            extensions_dir_provider=lambda: tmp_path,
            registry_provider=lambda: registry,
            search_fetch=_FakeNet({}),
        )
        service.start()
        assert LIST_TOOL in registry.names()
        service.stop()
        assert LIST_TOOL not in registry.names()


class TestInstallingRightAwayConnectsIt:
    """§4.4 的后半：装完不必重启，页面上当场就该知道它连没连上。"""

    def _net(self) -> _FakeNet:
        return _FakeNet(
            {
                "latest-extensions.json": {
                    "items": [
                        {
                            "kind": "mcp",
                            "id": "c1",
                            "name": "svc",
                            "summary": "s",
                            "install": {
                                "transport": "stdio",
                                "command": "the-real-command",
                                "args": [],
                            },
                        }
                    ]
                }
            }
        )

    def _service(self, tmp_path: Path, connect: Callable[[str], str] | None) -> ExtensionsService:
        return ExtensionsService(
            skills=_skills(tmp_path),
            extensions_dir_provider=lambda: tmp_path,
            registry_provider=_registry,
            search_fetch=self._net(),
            environ={},
            mcp_connect_provider=connect,
        )

    def _install_one(
        self, tmp_path: Path, connect: Callable[[str], str] | None
    ) -> dict[str, object]:
        service = self._service(tmp_path, connect)
        service.start()
        service.search("")
        return service.install("c1")

    def _verdict(self, state: dict[str, object]) -> str:
        return str(cast("dict[str, object]", state["install"])["connect"])

    def test_the_install_reports_the_verdict_of_that_one_connection(self, tmp_path: Path) -> None:
        asked: list[str] = []

        def connect(name: str) -> str:
            asked.append(name)
            return ""

        state = self._install_one(tmp_path, connect)
        assert asked == ["svc"], f"装完没有当场连一次：{asked}"
        assert "连上了" in self._verdict(state)

    def test_a_connection_failure_stays_visible_and_keeps_the_registration(
        self, tmp_path: Path
    ) -> None:
        state = self._install_one(tmp_path, lambda name: "启动命令找不到 npx")
        assert "找不到 npx" in self._verdict(state)
        assert path_for(tmp_path).is_file(), "连不上就把登记删掉 = 用户丢了他刚装的东西"

    def test_no_mcp_layer_connected_says_why_loudly(self, tmp_path: Path) -> None:
        state = self._install_one(tmp_path, None)
        assert "没接上 MCP 服务" in self._verdict(state)

    def test_a_connect_call_that_raises_does_not_take_the_page_down(self, tmp_path: Path) -> None:
        def boom(name: str) -> str:
            raise RuntimeError("子进程炸了")

        state = self._install_one(tmp_path, boom)
        assert "子进程炸了" in self._verdict(state)
