"""投屏控制条：跟着画面走的几何、点哪一颗走哪扇门、被拒的原因必须落在条上。

这条小窗是纯 Win32 + PIL（复用气泡那套 ``AlphaWindow``），所以这里测的是**它的决定**：
在哪儿、什么时候露脸、按一下走到哪扇门、那句话写不写。真窗口和真手机在
``E:\\DaiMa\\_qoder_scratch\\probe_mirror_bar.py`` 那一类台子上量，不在这儿。
"""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any

import pytest

from jarvis.app.screen_mirror import PHONE_KEYS, SWIPES
from jarvis.ui import mirror_bar as mb
from jarvis.ui.compositor import AlphaWindow, BubblePalette

REPO = Path(__file__).resolve().parents[1]

MIRROR = (1000, 120, 1540, 920)
"""投屏画面在屏幕上的矩形：宽 540、高 800，下面还剩 160 px 够贴一条。"""

WORK = (0, 0, 1920, 1080)


def status_keys() -> list[dict[str, Any]]:
    return [{"id": key.id, "label": key.label, "note": key.note} for key in PHONE_KEYS]


def status_swipes() -> list[dict[str, Any]]:
    return [{"id": row.id, "label": row.label, "note": row.note} for row in SWIPES]


class FakeWindow:
    """``AlphaWindow`` 的那一面：只记调用，不碰 Win32。"""

    def __init__(self) -> None:
        self.rect = (mb.OFF_SCREEN, mb.OFF_SCREEN, 418, 84)
        self.frames: list[str] = []
        self.spots: tuple[Any, ...] = ()
        self.shown: list[bool] = []
        self.moved: list[tuple[int, int]] = []
        self.topmost = 0
        self.click_through: bool | None = None
        self.alive = True

    def start(self, **kwargs: Any) -> bool:
        self.rect = (kwargs["x"], kwargs["y"], kwargs["width"], kwargs["height"])
        return True

    def present(self, data_url: str) -> bool:
        self.frames.append(data_url)
        return True

    def set_hot_spots(self, buttons: Any) -> None:
        self.spots = tuple(buttons)

    def set_visible(self, visible: bool) -> None:
        self.shown.append(visible)

    def move_to(self, x: int, y: int) -> None:
        self.rect = (x, y, self.rect[2], self.rect[3])
        self.moved.append((x, y))

    def keep_topmost(self) -> None:
        self.topmost += 1

    def set_click_through(self, on: bool) -> bool:
        self.click_through = on
        return True

    def close(self) -> None:
        self.alive = False


class FakeMirror:
    def __init__(self, *, phase: str = "running", error: str = "") -> None:
        self.phase = phase
        self.error = error
        self.keys: list[str] = []
        self.swipes: list[str] = []

    def status(self) -> dict[str, Any]:
        return {
            "phase": self.phase,
            "serial": "10AE1F1RQE003N3",
            "error": self.error,
            "keys": status_keys(),
            "swipes": status_swipes(),
        }

    def press_key(self, key_id: str = "") -> dict[str, Any]:
        self.keys.append(key_id)
        return self.status()

    def swipe(self, direction_id: str = "") -> dict[str, Any]:
        self.swipes.append(direction_id)
        return self.status()


def build(mirror: FakeMirror, window: FakeWindow, rect: Any = MIRROR) -> mb.MirrorControlBar:
    bar = mb.MirrorControlBar(
        mirror,
        mirror_rect=lambda: rect,
        work_area=lambda _mirror: WORK,
        window=window,
    )
    assert bar.start() is True
    return bar


class TestDocking:
    def test_it_stays_hidden_while_nothing_is_mirrored(self) -> None:
        window = FakeWindow()
        bar = build(FakeMirror(phase="off"), window)
        bar.refresh()
        assert True not in window.shown, "没投屏就摆一条没有归属的按钮在桌面上"
        assert window.frames == []

    def test_it_docks_under_the_picture(self) -> None:
        window = FakeWindow()
        bar = build(FakeMirror(), window)
        bar.refresh()
        assert window.moved == [(1061, 930)], "画面下面居中那格"
        assert window.shown[-1] is True, "起窗先藏、投屏开了才露脸"
        assert window.click_through is False, "这一条上的每一格都是要点的，不许点穿"
        assert window.topmost == 1

    def test_it_follows_the_picture_when_the_picture_moves(self) -> None:
        window = FakeWindow()
        moved = [(900, 120, 1440, 920)]
        bar = mb.MirrorControlBar(
            FakeMirror(),
            mirror_rect=lambda: moved[0],
            work_area=lambda _mirror: WORK,
            window=window,
        )
        assert bar.start() is True
        bar.refresh()
        first = window.moved[-1]
        bar.refresh()
        assert window.moved[-1] == first, "画面没动就不该再挪一次（每帧挪窗是看得见的抖）"
        moved[0] = (900, 400, 1440, 1200)
        bar.refresh()
        assert window.moved[-1] != first, "画面挪走了，条子还留在原地就是各说各的"

    def test_a_dragged_bar_stops_following_until_the_next_mirror(self) -> None:
        """拖走之后只认"还动没动"，不比落点 —— 硬贴回去的坐标和原来那个是同一个值。"""
        window = FakeWindow()
        mirror = FakeMirror()
        bar = build(mirror, window)
        bar.refresh()
        parked = len(window.moved)
        window.rect = (300, 300, window.rect[2], window.rect[3])  # 用户把它拖走了
        bar.refresh()
        assert len(window.moved) == parked, "拖走之后还按画面去贴，就是抢用户放下的位置"
        assert window.rect[:2] == (300, 300), "被抢回去了"

        mirror.phase = "off"
        bar.refresh()
        mirror.phase = "running"
        bar.refresh()
        assert len(window.moved) > parked, "新开一次投屏，条子该回到画面旁边"
        assert window.moved[-1] == (1061, 930)
        assert window.rect[:2] == (1061, 930)

    def test_it_hides_when_the_picture_window_disappears(self) -> None:
        window = FakeWindow()
        seen: list[tuple[int, int, int, int] | None] = [MIRROR]
        bar = mb.MirrorControlBar(
            FakeMirror(), mirror_rect=lambda: seen[0], work_area=lambda _m: WORK, window=window
        )
        assert bar.start() is True
        bar.refresh()
        assert window.shown[-1] is True
        seen[0] = None  # 投屏窗口被用户关了
        bar.refresh()
        assert window.shown[-1] is False

    def test_it_flips_above_when_there_is_no_room_below(self) -> None:
        assert mb.dock_origin((100, 200, 640, 1000), 418, 84, WORK) == (161, 106)

    def test_it_moves_beside_the_picture_instead_of_covering_it(self) -> None:
        """真机撞到的那条：1080 高的屏上画面占 974 px，上下都放不下。

        第一版把它夹到 ``y=0`` —— 正好盖住手机画面的上半截，而这条存在的理由就是别挡画面。
        """
        assert mb.dock_origin((742, 17, 1178, 991), 418, 84, WORK) == (1188, 462)
        # 右边也放不下（画面贴着右边缘）就翻到左边。
        assert mb.dock_origin((1500, 0, 1920, 1080), 418, 84, WORK) == (1072, 498)

    def test_a_picture_on_the_second_monitor_stays_on_that_monitor(self) -> None:
        assert mb.dock_origin((2000, 100, 2400, 900), 418, 84, (1920, 0, 3840, 1080)) == (1991, 910)


class TestPressing:
    def test_each_cell_opens_the_door_it_was_drawn_for(self) -> None:
        window = FakeWindow()
        mirror = FakeMirror()
        bar = build(mirror, window)
        bar.refresh()
        bar._on_tap("key:vol_up")
        bar._on_tap("swipe:next")
        assert mirror.keys == ["vol_up"] and mirror.swipes == ["next"]

    def test_a_press_on_something_unknown_presses_nothing(self) -> None:
        window = FakeWindow()
        mirror = FakeMirror()
        bar = build(mirror, window)
        bar.refresh()
        bar._on_tap("key:")
        bar._on_tap("nonsense")
        assert mirror.keys == [] and mirror.swipes == []

    def test_the_refusal_reason_is_drawn_on_the_bar(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """被拒不能只进日志：屏上没理由，用户只会认为按钮坏了。"""
        seen: list[str] = []
        real = mb.build_bar

        def spy(actions: Any, note: str, palette: Any = None) -> tuple[str, int, int]:
            seen.append(note)
            return real(actions, note)

        monkeypatch.setattr(mb, "build_bar", spy)
        window = FakeWindow()
        mirror = FakeMirror(error="这一下手机没接：SecurityException")
        bar = build(mirror, window)
        bar.refresh()
        assert any("SecurityException" in note for note in seen)

    def test_the_note_prefers_refusal_then_the_last_press_then_the_hint(self) -> None:
        now = time.time()
        assert mb.bar_note({"error": "手机不让注入"}, ("返回", now), now) == "手机不让注入"
        assert mb.bar_note({"error": ""}, ("返回", now - 3), now) == "已经发给这台：返回"
        assert mb.bar_note({"error": ""}, ("返回", now - 999), now) == mb.DEFAULT_NOTE
        assert mb.bar_note({"error": ""}, ("", 0.0), now) == mb.DEFAULT_NOTE


class TestShape:
    def test_the_cells_come_from_the_service_and_not_a_second_list(self) -> None:
        actions = mb.bar_actions(status_keys(), status_swipes())
        assert [row.name for row in actions] == [
            "key:back",
            "key:home",
            "key:recents",
            "key:vol_up",
            "key:vol_down",
            "swipe:next",
            "swipe:prev",
        ]
        assert [row.label for row in actions] == [
            "返回",
            "主页",
            "多任务",
            "音量 +",
            "音量 −",
            "下一个",
            "上一个",
        ], "括号里那半句留给面板，条子上不许变宽"

    def test_every_cell_hits_where_it_was_drawn(self) -> None:
        actions = mb.bar_actions(status_keys(), status_swipes())
        window = AlphaWindow()
        spots = mb.hot_spots(actions)
        window.set_hot_spots(spots)
        width, height = mb.bar_size(len(actions))
        assert (width, height) == (418, 84)
        for spot in spots:
            centre = (spot.x + spot.size // 2, spot.y + spot.size // 2)
            assert window.button_at(*centre) == spot.name
        assert window.button_at(mb.PAD + 54 + 1, 30) is None, "格缝不该命中任何一颗"

    def test_it_follows_the_skin_instead_of_keeping_its_own_colors(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """换肤之后这条必须跟着换色 —— 它和气泡一样是 Python 画的卡片。"""
        from jarvis.ui.compositor import DEFAULT_BUBBLE, bubble_palette

        state = {"palette": DEFAULT_BUBBLE}
        seen: list[BubblePalette] = []
        real = mb.build_bar

        def spy(
            actions: Any, note: str, palette: BubblePalette = DEFAULT_BUBBLE
        ) -> tuple[str, int, int]:
            seen.append(palette)
            return real(actions, note, palette)

        monkeypatch.setattr(mb, "build_bar", spy)
        window = FakeWindow()
        bar = mb.MirrorControlBar(
            FakeMirror(),
            mirror_rect=lambda: MIRROR,
            work_area=lambda _mirror: WORK,
            palette=lambda: state["palette"],
            window=window,
        )
        assert bar.start() is True
        bar.refresh()
        state["palette"] = bubble_palette(0x2F6B3F, 0x9FE8B0, 0xE6FFEF)
        bar.refresh()
        assert seen[0] is not seen[-1], "屏上留了两份颜色：皮肤一份，这条小窗一份"

    def test_a_machine_without_the_font_still_gets_the_shapes(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(mb, "font_at", lambda _px: None)
        actions = mb.bar_actions(status_keys(), status_swipes())
        url, width, height = mb.build_bar(actions, "随便一句话")
        assert url.startswith("data:image/png;base64,") and (width, height) == mb.bar_size(
            len(actions)
        )

    def test_an_unknown_id_still_draws_a_cell(self) -> None:
        actions = mb.bar_actions([{"id": "newthing", "label": "新按钮", "note": ""}], [])
        url, _, _ = mb.build_bar(actions, "")
        assert url.startswith("data:image/png;base64,")

    def test_the_shell_starts_it_and_stops_it(self) -> None:
        source = (REPO / "jarvis" / "ui" / "desktop.py").read_text(encoding="utf-8")
        assert (
            "MirrorControlBar(mirror, palette=bridge.current_bubble_palette)" in source
        ), "外壳没接这条小窗"
        assert "mirror_bar.start()" in source, "接了没起"
        assert "mirror_bar.stop()" in source, "退出时不关它，桌面上会留一条没人认领的按钮"

    def test_the_browser_mock_lists_the_same_buttons(self) -> None:
        """浏览器预览那份 ``MIRROR_KEYS`` 必须和服务的一份一致。

        不然浏览器里点的是三颗、真机上是五颗 —— 预览越像真的，漂了越看不出来。
        """
        source = (REPO / "frontend" / "src" / "api" / "bridge.ts").read_text(encoding="utf-8")

        def pairs(block: str) -> list[tuple[str, str]]:
            body = source.split(f"const {block}: MirrorAction[] = [", 1)[1].split("\n]", 1)[0]
            return [
                (found[0], found[1])
                for found in re.findall(r"\{ id: '([^']+)', label: '([^']+)'", body)
            ]

        assert pairs("MIRROR_KEYS") == [(key.id, key.label) for key in PHONE_KEYS]
        assert pairs("MIRROR_SWIPES") == [(row.id, row.label) for row in SWIPES]
