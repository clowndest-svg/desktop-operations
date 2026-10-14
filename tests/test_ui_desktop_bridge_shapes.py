"""桥面那份「什么都没有」的形状，必须和服务自己给的那一份**键完全一致**。

`_extensions_empty` 的注释写着"键必须和 `ExtensionsService.state()` 完全一致"，
可这句话从来没有测试盯着 —— 而少一个键的表现是界面那一块**不画**，正是这一层最贵的那笔账
（"能力在、屏上看不见"）。r110 给 state 加了 `verdicts`、新开了详情与推荐两扇门，
就是它会漂的那一刻。

判据用集合相等，不用"包含"：多一个键也算漂 —— 页面按合同渲染，合同之外的键是
另一份没人读的形状。
"""

from __future__ import annotations

from pathlib import Path
from typing import cast

from jarvis.extensions import ExtensionsService
from jarvis.ui.desktop import _detail_empty, _extensions_empty, _recommended_empty
from tests.test_extensions import _FakeNet, _registry, _skills
from tests.test_extensions_verdicts import FS_ROW, PG_ROW, POSTER_ROW


def _sub(row: dict[str, object], key: str) -> dict[str, object]:
    """嵌套那一格：后端给的是 `dict[str, object]`，严格模式要 cast 一次才肯再索引。"""
    return cast("dict[str, object]", row[key])


def _service(tmp_path: Path) -> ExtensionsService:
    net = _FakeNet(
        {
            "latest-extensions.json": {"version": 1, "items": [POSTER_ROW, FS_ROW, PG_ROW]},
        }
    )
    skills = _skills(tmp_path)
    skills.start()
    service = ExtensionsService(
        skills=skills,
        extensions_dir_provider=lambda: tmp_path,
        registry_provider=_registry,
        search_fetch=net,
        environ={},
    )
    service.start()
    return service


class TestEmptyShapesTrackTheRealOnes:
    def test_the_state_skeleton_has_exactly_the_services_keys(self, tmp_path: Path) -> None:
        service = _service(tmp_path)
        real = set(service.state())
        blank = set(_extensions_empty("这个进程没有接扩展这一层"))
        assert blank == real, f"桥面多了 {blank - real}、少了 {real - blank}"

    def test_the_nested_runtime_and_mcp_shapes_agree_too(self, tmp_path: Path) -> None:
        """外层对上、里层少一格，界面同样是"这一块不画"。"""
        service = _service(tmp_path)
        state = service.state()
        for key in ("mcp", "runtime", "search"):
            real_row = cast("dict[str, object]", state[key])
            blank_row = cast("dict[str, object]", _extensions_empty("x")[key])
            assert set(real_row) == set(blank_row), key

    def test_the_detail_skeleton_matches_a_real_card(self, tmp_path: Path) -> None:
        service = _service(tmp_path)
        service.recommended()
        real = service.detail("curated:poster")
        blank = _detail_empty("curated:poster", "这个进程没有接扩展这一层")
        assert set(blank) == set(real), "详情卡的骨架和真卡不一致"
        assert set(_sub(blank, "installed")) == set(_sub(real, "installed")), "卡上那一格"
        for key in ("install", "update", "remove"):
            assert set(_sub(blank, key)) == set(_sub(real, key)), key

    def test_an_unknown_id_still_comes_back_with_the_nested_shape(self, tmp_path: Path) -> None:
        """服务自己那条"id 不认识"的路也要回**完整骨架**。

        上一轮这条测试只比了外层键，结果把 `installed` 里少一格 `yes` 的变异放过去了 ——
        界面读到 undefined 就是那一块不画，外层键还在。
        """
        service = _service(tmp_path)
        # 先读一次推荐：不登记 id 的话 `detail("curated:poster")` 走的也是"不认识"那条路，
        # 两边都是同一份骨架 —— 这条判据就变成恒真，什么变异都放过去。
        service.recommended()
        real = service.detail("curated:poster")
        blank = service.detail("这条没给出去")
        assert real["found"] is True, "对照的那一张本身就没走到真卡那条路"
        assert blank["found"] is False
        assert set(_sub(blank, "installed")) == set(_sub(real, "installed"))
        for key in ("install", "update", "remove"):
            assert set(_sub(blank, key)) == set(_sub(real, key)), key

    def test_the_recommended_skeleton_matches_a_real_read(self, tmp_path: Path) -> None:
        service = _service(tmp_path)
        assert set(_recommended_empty("没接")) == set(service.recommended())

    def test_a_missing_verdicts_key_would_have_been_caught(self, tmp_path: Path) -> None:
        """这条是给上面那条看的：verdicts 这一格如果哪天只在服务里、没进桥面骨架，
        第一条测试就得红。判据不能是"看着像会红"。"""
        service = _service(tmp_path)
        assert "verdicts" in service.state()
        assert "verdicts" in _extensions_empty("x")
