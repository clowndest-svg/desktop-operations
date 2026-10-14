"""r110 追加的测试：推荐、判词、更新、按条目删。

判据挑的是"这一轮最容易做错的那几件事"，不是"新函数都调用一遍"：

1. **推荐里的条目必须能直接装** —— 装只认"我们给出去的 id"，如果 `recommended()` 忘了
   把 id 登记进去，那一排按钮就永远灰着，而页面看起来是正常功能；
2. **清单读不到 ≠ 没有推荐**（这一层第三次为这句话写测试）；
3. **更新判断不了的时候要拒绝并说清原因**，不给一颗点了什么也不会变的按钮；
4. **内容没变就不许写盘**（mtime 不变）—— 否则"更新"会变成"每次都用同一份内容重写一遍"，
   用户看不出它到底有没有起作用；
5. **删的是后端解析出来的那一个**，删错/删多了测试要红。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

from jarvis.extensions import ExtensionsService
from jarvis.extensions.installer import SOURCE_FILENAME, read_provenance
from tests.test_extensions import (
    SKILL_TEXT,
    _FakeNet,
    _registry,
    _skills,
)

POSTER_ROW: dict[str, Any] = {
    "kind": "skill",
    "id": "curated:poster",
    "name": "poster",
    "summary": "做海报的说明书",
    "description": "先想清楚要说什么，再排版。这一栏是详情页那一段长说明。",
    "icon": "🎨",
    "version": "1.2.0",
    "ref": "a1b2c3d",
    "homepage": "https://github.com/example/author-skills",
    "needs": [],
    "install": {"repo": "example/author-skills", "ref": "a1b2c3d", "path": "skills/poster"},
}
FS_ROW: dict[str, Any] = {
    "kind": "mcp",
    "id": "curated:filesystem",
    "name": "filesystem",
    "summary": "把文件夹读给她看",
    "icon": "https://avatars.githubusercontent.com/u/100976983?s=400",
    "needs": ["npx"],
    "install": {
        "name": "filesystem",
        "transport": "stdio",
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-filesystem"],
    },
}
PG_ROW: dict[str, Any] = {
    "kind": "mcp",
    "id": "curated:postgres",
    "name": "postgres",
    "summary": "连一个本地库",
    "needs": [],
    # 用 python 起（不是 npx/uvx 那种"每次现拉"的启动器），这样"清单没变"和
    # "启动器每次现拉"两条拒绝的理由才分得开 —— 混在一条上就测不出各自的话。
    "install": {
        "name": "postgres",
        "transport": "stdio",
        "command": "python",
        "args": ["-m", "mcp_pg"],
    },
}

_LISTING = [{"name": "SKILL.md", "type": "file"}]


def _net(extra: dict[str, object] | None = None, *, skill_text: str = SKILL_TEXT) -> _FakeNet:
    routes: dict[str, object] = {
        "latest-extensions.json": {"version": 1, "items": [POSTER_ROW, FS_ROW, PG_ROW]},
        "contents/skills/poster": _LISTING,
        "raw.githubusercontent.com": skill_text.encode("utf-8"),
    }
    routes.update(extra or {})
    return _FakeNet(routes)


def _sub(row: dict[str, object], key: str) -> dict[str, object]:
    """从 `dict[str, object]` 里取一格嵌套。

    mypy 严格模式不认"object 还能再索引"，而这里的值确实是映射 —— 后端那份形状
    就是这么给的（`verdicts[id].installed`）。就地 cast 一次，比在断言里写一串
    看不懂的类型标注干净。
    """
    return cast("dict[str, object]", row[key])


def _service(tmp_path: Path, net: _FakeNet) -> ExtensionsService:
    skills = _skills(tmp_path)
    skills.start()
    return ExtensionsService(
        skills=skills,
        extensions_dir_provider=lambda: tmp_path,
        registry_provider=_registry,
        search_fetch=net,
        environ={},
        audit_file_provider=lambda: tmp_path / "extensions.jsonl",
    )


class TestRecommendations:
    def test_a_recommended_row_can_be_installed_without_ever_searching(
        self, tmp_path: Path
    ) -> None:
        """推荐那一格最容易做错的就是"忘了登记 id"：表现是按钮永远灰着，页面却说这是功能。"""
        net = _net()
        service = _service(tmp_path, net)
        service.start()
        rows = service.recommended()
        assert [row["id"] for row in cast("list[dict[str, object]]", rows["items"])] == [
            "curated:poster",
            "curated:filesystem",
            "curated:postgres",
        ]
        assert rows["error"] == ""
        answer = service.install("curated:poster")
        assert answer["error"] == "", answer["error"]
        assert cast("dict[str, object]", answer["install"])["ok"] is True

    def test_a_catalog_that_could_not_be_read_says_so(self, tmp_path: Path) -> None:
        """空列表和"这份清单没读到"必须是两句话 —— 用户下一步改的是不同的东西。"""
        service = _service(tmp_path, _FakeNet({}))
        service.start()
        rows = service.recommended()
        assert rows["items"] == []
        assert str(rows["error"]).strip(), "读不到却没说原因"

    def test_the_curated_row_keeps_icon_description_and_version(self, tmp_path: Path) -> None:
        rows = _service(tmp_path, _net()).recommended()
        first = cast("list[dict[str, object]]", rows["items"])[0]
        assert first["icon"] == "🎨"
        assert "长说明" in str(first["description"])
        assert first["version"] == "1.2.0"

    def test_an_avatar_url_from_another_host_never_reaches_the_page(self) -> None:
        """图标是页面自己去加载的一张图：白名单外的一律在后端丢掉。"""
        from jarvis.extensions.sources import safe_icon

        assert safe_icon("https://avatars.githubusercontent.com/x/y.png").startswith("https://")
        for evil in (
            "http://127.0.0.1:8188/x.png",
            # 主机对但走 http：一样不许 —— 明文请求能被中间人换成任何一张图
            "http://avatars.githubusercontent.com/u/1.png",
            "https://evil-githubusercontent.com/x.png",
            "https://avatars.githubusercontent.com.evil.test/x.png",
        ):
            assert safe_icon(evil) == "", f"这条过了白名单：{evil}"


class TestVerdictsRideAlongWithState:
    def test_every_id_the_service_handed_out_has_a_verdict(self, tmp_path: Path) -> None:
        service = _service(tmp_path, _net())
        service.start()
        service.recommended()
        verdicts = cast("dict[str, dict[str, object]]", service.state()["verdicts"])
        assert set(verdicts) == {"curated:poster", "curated:filesystem", "curated:postgres"}
        for row in verdicts.values():
            assert {"installed", "install", "update", "remove"} <= set(row)

    def test_a_row_still_on_disk_reads_as_installed(self, tmp_path: Path) -> None:
        service = _service(tmp_path, _net())
        service.start()
        service.recommended()
        for row in cast("dict[str, dict[str, object]]", service.state()["verdicts"]).values():
            assert _sub(row, "installed")["yes"] is False
        service.install("curated:poster")
        verdict = cast("dict[str, dict[str, object]]", service.state()["verdicts"])[
            "curated:poster"
        ]
        assert _sub(verdict, "installed")["yes"] is True
        assert _sub(verdict, "install")["can"] is False
        assert "已经装了" in str(_sub(verdict, "install")["words"])
        assert _sub(verdict, "update")["can"] is True


class TestUpdatingMeansWhatItSays:
    def test_identical_content_writes_nothing(self, tmp_path: Path) -> None:
        """内容一模一样就不许动盘：否则"更新"每次都在重写，用户看不出有没有新东西。"""
        service = _service(tmp_path, _net())
        service.start()
        service.recommended()
        service.install("curated:poster")
        target = tmp_path / "skills" / "poster" / "SKILL.md"
        before = target.stat().st_mtime_ns
        answer = service.update("curated:poster")
        assert answer["error"] == "", answer["error"]
        detail = str(cast("dict[str, object]", answer["update"])["detail"])
        assert "没动" in detail, detail
        assert target.stat().st_mtime_ns == before, "说没动，文件却被重写了"

    def test_changed_content_lands_and_says_so(self, tmp_path: Path) -> None:
        fresh = "---\nname: poster\ndescription: 做海报用的说明书\n---\n\n# 步骤\n先画，再想。\n"
        service = _service(tmp_path, _net())
        service.start()
        service.recommended()
        service.install("curated:poster")
        # 换一份内容：同一个地址，第二次取回来的是新的。
        # 这里另起一个服务实例而不是改私有属性 —— 它得能从盘上读回上一轮装的那一份。
        again = _service(tmp_path, _net(skill_text=fresh))
        again.start()
        again.recommended()
        answer = again.update("curated:poster")
        row = cast("dict[str, object]", answer["update"])
        assert row["ok"] is True and "换成新内容" in str(row["detail"]), row
        body = (tmp_path / "skills" / "poster" / "SKILL.md").read_text(encoding="utf-8")
        assert "先画，再想" in body

    def test_reinstalling_over_an_existing_one_survives_a_failed_fetch(
        self, tmp_path: Path
    ) -> None:
        """**重装**那一条路才是顺序判据待的地方：更新本来就"先取后拆"，看不见这段。

        表现如果反了：装第二次时网络断了 → 盘上那一份被拆掉、新的又没取回来，
        用户两头空。
        """
        service = _service(tmp_path, _net())
        service.start()
        service.recommended()
        service.install("curated:poster")
        broken = _FakeNet(
            {
                "latest-extensions.json": {"version": 1, "items": [POSTER_ROW, FS_ROW, PG_ROW]},
                "contents/skills/poster": _LISTING,
                "raw.githubusercontent.com": ValueError("第二次取的时候断了"),
            }
        )
        again = _service(tmp_path, broken)
        again.start()
        again.recommended()
        answer = again.install("curated:poster")
        assert "取文件失败" in str(answer["error"]), answer["error"]
        assert (tmp_path / "skills" / "poster" / "SKILL.md").is_file(), "重装失败把旧的那一份拆了"

    def test_a_failed_fetch_leaves_the_old_copy_alone(self, tmp_path: Path) -> None:
        """更新失败不许把原来那一份一起带走。

        顺序判据：会失败的那几步（取文件、验 ref）必须排在"动手拆盘上那一份"之前。
        反过来的表现是"更新没成功，可旧的那份也没了"—— 用户两头空。
        """
        service = _service(tmp_path, _net())
        service.start()
        service.recommended()
        service.install("curated:poster")
        broken = _FakeNet(
            {
                "latest-extensions.json": {"version": 1, "items": [POSTER_ROW, FS_ROW, PG_ROW]},
                "contents/skills/poster": _LISTING,
                "raw.githubusercontent.com": ValueError("那边 503 了"),
            }
        )
        again = _service(tmp_path, broken)
        again.start()
        again.recommended()
        answer = again.update("curated:poster")
        assert "取文件失败" in str(answer["error"]), answer["error"]
        assert (tmp_path / "skills" / "poster" / "SKILL.md").is_file(), "更新失败把旧的那一份删了"

    def test_an_install_without_provenance_refuses_and_explains(self, tmp_path: Path) -> None:
        """更早那版装的东西没记来历。这里只能拒绝 —— 给一颗点了什么也不会变的按钮是假能力。"""
        service = _service(tmp_path, _net())
        service.start()
        service.recommended()
        service.install("curated:poster")
        (tmp_path / "skills" / "poster" / SOURCE_FILENAME).unlink()
        verdicts = cast("dict[str, dict[str, object]]", service.state()["verdicts"])
        verdict = _sub(verdicts["curated:poster"], "update")
        # 钉判词，不只钉那句拒绝的话：话是两处共用的，只比话就分不出
        # "按钮本来就是灰的"和"按钮亮着、点下去才说不行"
        assert verdict["can"] is False, "没来历记录却把「更新」点亮了"
        answer = service.update("curated:poster")
        # 拒绝走的是 error 那一格（和装 / 删同一套说法），不另造一个形状
        reason = str(answer["error"])
        assert "没留下来历记录" in reason, reason
        # 逐字对：这句话给的是"删掉这一份，再装一次"，不是"重装"那种含糊说法
        assert "先删掉这一份" in reason and "再装一次" in reason, reason

    def test_an_npx_row_has_no_update_step(self, tmp_path: Path) -> None:
        """`npx -y` 每次起都现拉最新版 —— 对它说"可以更新"就是假话。"""
        service = _service(tmp_path, _net())
        service.start()
        service.recommended()
        service.install("curated:filesystem")
        verdict = cast("dict[str, dict[str, object]]", service.state()["verdicts"])[
            "curated:filesystem"
        ]
        assert _sub(verdict, "update")["can"] is False
        assert "现拉" in str(_sub(verdict, "update")["words"])

    def test_an_unchanged_row_says_there_is_nothing_to_rewrite(self, tmp_path: Path) -> None:
        """清单没变就别谎称"更新过了"。"""
        service = _service(tmp_path, _net())
        service.start()
        service.recommended()
        service.install("curated:postgres")
        answer = service.update("curated:postgres")
        assert "一模一样" in str(answer["error"]), "没变却说要重写" + str(answer["error"])

    def test_a_changed_row_is_rewritten_and_keeps_its_switch(self, tmp_path: Path) -> None:
        """清单上那条真变了才重写；重写不许顺手把人关掉的 server 打开。"""
        service = _service(tmp_path, _net())
        service.start()
        service.recommended()
        service.install("curated:postgres")
        service.set_server_enabled("postgres", False)
        moved = json.loads(json.dumps(PG_ROW))
        moved["install"]["args"] = ["mcp-pg", "--new-flag"]
        again = _service(
            tmp_path,
            _net({"latest-extensions.json": {"version": 1, "items": [POSTER_ROW, FS_ROW, moved]}}),
        )
        again.start()
        again.recommended()
        verdict = cast("dict[str, dict[str, object]]", again.state()["verdicts"])[
            "curated:postgres"
        ]
        assert _sub(verdict, "installed")["changed"] is True, "清单变了却没比出来"
        assert _sub(verdict, "update")["can"] is True, verdict["update"]
        answer = again.update("curated:postgres")
        assert answer["error"] == "", answer["error"]
        servers = cast("list[dict[str, object]]", again.state()["servers"])
        row = next(one for one in servers if one["name"] == "postgres")
        assert row["enabled"] is False, "更新把停用的那条偷偷打开了"
        assert "--new-flag" in json.dumps(row, ensure_ascii=False)

    def test_updating_something_never_installed_is_not_a_success(self, tmp_path: Path) -> None:
        service = _service(tmp_path, _net())
        service.start()
        service.recommended()
        answer = service.update("curated:poster")
        assert "还没装" in str(answer["error"])


class TestRemovingByItem:
    def test_it_deletes_exactly_the_one_the_row_points_at(self, tmp_path: Path) -> None:
        service = _service(tmp_path, _net())
        service.start()
        service.recommended()
        service.install("curated:poster")
        other = tmp_path / "skills" / "keepme"
        other.mkdir(parents=True)
        (other / "SKILL.md").write_text(SKILL_TEXT, encoding="utf-8")
        answer = service.remove_item("curated:poster", confirmed=True)
        assert answer["error"] == "", answer["error"]
        assert not (tmp_path / "skills" / "poster").exists()
        assert other.is_dir(), "删多了"

    def test_without_confirmation_nothing_is_touched(self, tmp_path: Path) -> None:
        service = _service(tmp_path, _net())
        service.start()
        service.recommended()
        service.install("curated:poster")
        answer = service.remove_item("curated:poster", confirmed=False)
        assert "确认" in str(answer["error"])
        assert (tmp_path / "skills" / "poster").is_dir()

    def test_removing_a_row_that_was_never_installed_says_so(self, tmp_path: Path) -> None:
        service = _service(tmp_path, _net())
        service.start()
        service.recommended()
        answer = service.remove_item("curated:poster", confirmed=True)
        assert "本来就没装" in str(answer["error"])


class TestDetailShape:
    def test_an_unknown_id_still_comes_back_whole(self, tmp_path: Path) -> None:
        service = _service(tmp_path, _net())
        service.start()
        card = service.detail("never-handed-out")
        assert card["found"] is False and card["error"]
        for key in ("item", "installed", "install", "update", "remove", "runtime", "log"):
            assert key in card, f"详情卡少了 {key}：界面那一块就不画，为什么也没人说"

    def test_the_card_says_what_the_icon_really_is(self, tmp_path: Path) -> None:
        service = _service(tmp_path, _net())
        service.start()
        service.recommended()
        card = service.detail("curated:poster")
        item = cast("dict[str, object]", card["item"])
        assert item["icon"] == "🎨"
        assert _sub(card, "installed")["words"] == "还没装"
        assert _sub(card, "update")["can"] is False
        assert "还没装" in str(_sub(card, "update")["words"])


class TestProvenanceFile:
    def test_the_sidecar_records_what_was_fetched(self, tmp_path: Path) -> None:
        service = _service(tmp_path, _net())
        service.start()
        service.recommended()
        service.install("curated:poster")
        row = read_provenance(tmp_path / "skills" / "poster")
        assert row["catalog_id"] == "curated:poster"
        assert row["repo"] == "example/author-skills"
        assert row["ref"] == "a1b2c3d"
        assert row["digest"] and row["files"] == ["SKILL.md"]
        assert json.dumps(row, ensure_ascii=False)

    def test_the_sidecar_is_not_read_as_a_skill(self, tmp_path: Path) -> None:
        """来历记录躺在技能目录里，但不能被当成第二份技能读进提示。"""
        service = _service(tmp_path, _net())
        service.start()
        service.recommended()
        service.install("curated:poster")
        slugs = [
            str(row["slug"]) for row in cast("list[dict[str, object]]", service.state()["skills"])
        ]
        assert slugs == ["poster"], f"来历记录被当成第二份技能读进来了：{slugs}"
