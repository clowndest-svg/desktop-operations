"""The pet window's own arithmetic: where it goes, what is clickable, what it costs.

The ctypes calls are the one part that cannot run meaningfully in a test, so the
fake window below deliberately has no ``native`` handle: everything under test is the
decision (is the pointer over the handle? is this rect usable?), and the decision is
made from numbers the page reported -- not from a live window.
"""

from __future__ import annotations

import ctypes
import sys
import threading
import types
from typing import Any, cast

import pytest

from jarvis.ui.compositor import BUBBLE_ADD, BUBBLE_STOP, bubble_palette
from jarvis.ui.pet import (
    GRIP_FALLBACK,
    PARK_FLAGS,
    PET_HEIGHT,
    PET_TITLE,
    PET_WIDTH,
    SWP_NOACTIVATE,
    SWP_SHOWWINDOW,
    PetController,
    _four_floats,
    _inside_grip,
    _restore_foreground,
    _scale_of,
    handle_of,
    pet_origin,
)


class _Signal:
    """The one part of pywebview's event object the shell touches: ``+=``."""

    def __init__(self) -> None:
        self.handlers: list[Any] = []

    def __iadd__(self, handler: Any) -> _Signal:
        self.handlers.append(handler)
        return self

    def fire(self) -> None:
        for handler in self.handlers:
            handler()


class FakePetWindow:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.scripts: list[str] = []
        self.events = types.SimpleNamespace(loaded=_Signal())

    def show(self) -> None:
        self.calls.append("show")

    def hide(self) -> None:
        self.calls.append("hide")

    def destroy(self) -> None:
        self.calls.append("destroy")

    def evaluate_js(self, script: str) -> None:
        self.scripts.append(script)


def fake_webview(
    monkeypatch: Any, *, screen: tuple[int, int] = (1920, 1080)
) -> types.SimpleNamespace:
    """A ``webview`` module that records what the controller asked for."""
    created: list[dict[str, Any]] = []
    window = FakePetWindow()

    def create_window(**kwargs: Any) -> FakePetWindow:
        created.append(kwargs)
        return window

    module = types.SimpleNamespace(
        create_window=create_window,
        created=created,
        screens=lambda: [types.SimpleNamespace(width=screen[0], height=screen[1])],
    )
    monkeypatch.setitem(sys.modules, "webview", module)
    return module


def controller(**kwargs: Any) -> PetController:
    return PetController(base_url="http://127.0.0.1:54321/", bridge=object(), **kwargs)


class TestGeometry:
    def test_the_figure_is_centred_on_the_screen(self) -> None:
        assert pet_origin(1920, 1080, 400, 600) == (760, 240)

    def test_a_screen_shorter_than_the_window_keeps_it_on_screen(self) -> None:
        """Centred horizontally, pinned to the top rather than half off the bottom."""
        assert pet_origin(800, 600, PET_WIDTH, PET_HEIGHT) == (190, 0)

    def test_the_clickable_patch_starts_where_the_page_will_draw_it(self) -> None:
        """The fallback matters on the first frame, before the page has reported."""
        x, y, width, height = GRIP_FALLBACK
        assert 0.0 <= x < 1.0 and 0.0 <= y < 1.0
        assert 0.0 < width <= 1.0 and 0.0 < height <= 1.0


class TestGripMath:
    def test_the_pointer_is_on_the_handle_only_inside_it(self) -> None:
        rect = (0, 0, 1000, 800)
        grip = (0.5, 0.05, 0.3, 0.05)  # x, y, w, h as fractions
        assert _inside_grip((600, 60), rect, grip) is True
        assert _inside_grip((100, 60), rect, grip) is False
        assert _inside_grip((600, 400), rect, grip) is False

    def test_a_little_padding_covers_the_edge_of_the_chip(self) -> None:
        assert _inside_grip((494, 60), (0, 0, 1000, 800), (0.5, 0.05, 0.3, 0.05)) is True

    def test_padding_is_in_device_pixels_so_scaling_does_not_shrink_the_target(
        self,
    ) -> None:
        """At 200% a 6-CSS-pixel pad is 12 device pixels; a fixed 6 would miss."""
        assert _inside_grip((488, 60), (0, 0, 1000, 800), (0.5, 0.05, 0.3, 0.05), scale=2.0) is True
        assert (
            _inside_grip((488, 60), (0, 0, 1000, 800), (0.5, 0.05, 0.3, 0.05), scale=1.0) is False
        )

    def test_the_scale_is_measured_from_the_window_not_assumed(self) -> None:
        assert _scale_of((0, 0, 840, 1280), 420) == 2.0
        assert _scale_of((0, 0, 0, 0), 420) == 1.0

    @pytest.mark.parametrize(
        "bad",
        [
            None,
            "0,0,1,1",
            [],
            [0.1, 0.1, 0.2],
            {"x": 0.1, "y": 0.1},
            {"x": 0.1, "y": 0.1, "width": True, "height": 0.2},
            {"x": 2.0, "y": 0.1, "width": 0.2, "height": 0.2},
            {"x": 0.1, "y": 0.1, "width": 0.0, "height": 0.2},
        ],
    )
    def test_a_rect_that_is_not_a_rect_is_refused(self, bad: object) -> None:
        assert _four_floats(bad) is None

    def test_both_spellings_of_the_rect_are_accepted(self) -> None:
        assert _four_floats({"x": 0.1, "y": 0.2, "width": 0.3, "height": 0.4}) == (
            0.1,
            0.2,
            0.3,
            0.4,
        )
        assert _four_floats({"x": 0.1, "y": 0.2, "w": 0.3, "h": 0.4}) == (0.1, 0.2, 0.3, 0.4)
        assert _four_floats([0.1, 0.2, 0.3, 0.4]) == (0.1, 0.2, 0.3, 0.4)


class TestHandle:
    def test_a_window_with_no_native_side_has_no_handle(self) -> None:
        assert handle_of(FakePetWindow()) == 0

    def test_the_dotnet_unboxing_convention_pywebview_uses_is_honoured(self) -> None:
        class Holder:
            def ToInt32(self) -> int:  # noqa: N802 - .NET's spelling, not ours
                return 4242

        class Native:
            Handle = Holder()

        class Formed:
            native = Native()

        assert handle_of(Formed()) == 4242

    def test_a_handle_that_refuses_to_unbox_is_zero_not_a_crash(self) -> None:
        class Native:
            Handle = object()

        class Formed:
            native = Native()

        assert handle_of(Formed()) == 0


class TestController:
    def test_show_creates_the_window_once_and_transparently(self, monkeypatch: Any) -> None:
        module = fake_webview(monkeypatch)
        pet = controller()
        assert pet.show() is True
        assert pet.created is True
        assert pet.show() is True
        assert len(module.created) == 1
        asked = module.created[0]
        assert asked["frameless"] is True
        assert asked["transparent"] is True
        assert asked["on_top"] is True
        # focus=False is what keeps the pet from stealing the keyboard mid-sentence.
        assert asked["focus"] is False
        assert asked["easy_drag"] is True
        assert asked["title"] == PET_TITLE
        assert asked["url"].endswith("/?mode=pet")

    def test_the_window_is_centred_on_the_reported_screen(self, monkeypatch: Any) -> None:
        module = fake_webview(monkeypatch, screen=(2560, 1440))
        controller().show()
        asked = module.created[0]
        assert (asked["x"], asked["y"]) == (
            (2560 - PET_WIDTH) // 2,
            (1440 - PET_HEIGHT) // 2,
        )

    def test_hide_then_show_reuses_the_same_page(self, monkeypatch: Any) -> None:
        module = fake_webview(monkeypatch)
        pet = controller()
        pet.show()
        pet.hide()
        pet.show()
        assert len(module.created) == 1
        assert pet.shown is True

    def test_toggle_reports_whether_the_pet_is_on_screen(self, monkeypatch: Any) -> None:
        fake_webview(monkeypatch)
        pet = controller()
        assert pet.toggle() is True
        assert pet.toggle() is False
        assert pet.shown is False

    def test_summon_plays_the_arrival(self, monkeypatch: Any) -> None:
        fake_webview(monkeypatch)
        pet = controller()
        assert pet.summon() is True
        scripts = pet.window.scripts  # type: ignore[union-attr]
        assert any("__jarvisPet('emerge')" in line for line in scripts)
        assert any("__jarvisPet('wake')" in line for line in scripts)

    def test_hiding_stops_the_draw_loop_from_the_shell_side(self, monkeypatch: Any) -> None:
        """The page cannot see its own window being hidden, so this is the only cue."""
        fake_webview(monkeypatch)
        pet = controller()
        pet.show()
        pet.hide()
        assert any("__jarvisPet('sleep')" in line for line in pet.window.scripts)  # type: ignore[union-attr]

    def test_audio_window_follows_visibility(self, monkeypatch: Any) -> None:
        fake_webview(monkeypatch)
        pet = controller()
        assert pet.audio_window is None
        pet.show()
        assert pet.audio_window is pet.window
        pet.hide()
        assert pet.audio_window is None

    def test_close_destroys_and_stops_the_pointer_watch(self, monkeypatch: Any) -> None:
        fake_webview(monkeypatch)
        pet = controller()
        pet.show()
        window = pet.window
        pet.close()
        assert window is not None and "destroy" in window.calls
        assert pet.created is False
        assert pet.shown is False
        pet.close()

    def test_a_visibility_hook_sees_every_change(self, monkeypatch: Any) -> None:
        fake_webview(monkeypatch)
        seen: list[bool] = []
        pet = controller(on_active_change=seen.append)
        pet.show()
        pet.hide()
        assert seen == [True, False]

    def test_a_malformed_grip_is_ignored_rather_than_defaulted(self, monkeypatch: Any) -> None:
        """Guessing a new handle position would move the only clickable patch on a
        window that is otherwise invisible to the mouse."""
        fake_webview(monkeypatch)
        pet = controller()
        pet.report_grip({"x": 0.1, "y": 0.1, "width": 0.2, "height": 0.1})
        before = pet._grip
        pet.report_grip({"x": 9, "y": 0, "width": 1, "height": 1})
        assert pet._grip == before

    def test_the_dragging_flag_is_what_keeps_a_drag_alive(self, monkeypatch: Any) -> None:
        fake_webview(monkeypatch)
        pet = controller()
        pet.show()
        pet.set_dragging(True)
        assert pet._dragging is True
        pet.set_dragging(False)
        assert pet._dragging is False

    def test_a_window_that_cannot_be_created_is_reported_not_raised(self, monkeypatch: Any) -> None:
        def explode(**kwargs: Any) -> None:
            raise RuntimeError("没有第二个窗口")

        monkeypatch.setitem(
            sys.modules, "webview", types.SimpleNamespace(create_window=explode, screens=lambda: [])
        )
        pet = controller()
        assert pet.show() is False
        assert pet.shown is False
        assert pet.hide() is True


class TestCueReplay:
    """A cue sent before the page installed its handler is a cue that never happened.

    ``show()`` can run while the bundle is still loading -- which is exactly what a
    launch that restores the pet does -- so every cue has to be said again on
    ``loaded``, including the one that keeps a page nobody can see from drawing.
    """

    def test_a_page_that_loads_while_hidden_is_told_to_stop_drawing(self, monkeypatch: Any) -> None:
        fake_webview(monkeypatch)
        pet = controller()
        pet.show()
        pet.hide()
        window = cast(FakePetWindow, pet.window)
        window.scripts.clear()
        window.events.loaded.fire()
        assert any("'sleep'" in script for script in window.scripts), window.scripts
        assert not any("'wake'" in script for script in window.scripts), window.scripts

    def test_a_page_that_loads_while_shown_is_woken(self, monkeypatch: Any) -> None:
        fake_webview(monkeypatch)
        pet = controller()
        pet.show()
        window = cast(FakePetWindow, pet.window)
        window.scripts.clear()
        window.events.loaded.fire()
        assert any("'wake'" in script for script in window.scripts), window.scripts

    def test_a_reload_asks_for_alpha_frames_again(self, monkeypatch: Any) -> None:
        """The compositor outlives the page, not the other way round."""
        fake_webview(monkeypatch)
        pet = controller()
        pet.show()
        compositor: Any = types.SimpleNamespace(
            alive=True,
            grab=None,
            captions=[],
            screen_rect=(0, 0, 404, 601),
            pressing=False,
            opaque_at=lambda x, y: False,
            set_click_through=lambda on: False,
            keep_topmost=lambda: None,
        )
        pet._compositor = compositor
        window = cast(FakePetWindow, pet.window)
        window.scripts.clear()
        window.events.loaded.fire()
        assert any("'alpha'" in script for script in window.scripts), window.scripts


class _FakeCompositor:
    """Her window, as far as the bubble is concerned: a box on a screen.

    ``screen_rect`` is ``(left, top, right, bottom)`` -- the shape the controller
    actually unpacks. Handing it ``(x, y, w, h)`` here is the mistake that once fed
    head tracking a negative width, and it would put the bubble at a nonsense x
    without failing loudly.
    """

    def __init__(self, box: tuple[int, int, int, int] = (700, 200, 1120, 840)) -> None:
        self.screen_rect = box
        self.visible = True

    def set_visible(self, shown: bool) -> None:
        self.visible = shown


class _FakeCaptionWindow:
    """The bubble's own window. Records what the controller asks it to do.

    ``set_hot_spots`` / ``button_at`` / ``pressing`` are the parts the card icons added:
    the controller decides click-through from them on every poll, and :meth:`press` is how
    a test pretends a finger landed on one -- which is the only way the routing from an
    icon to the shell gets exercised without Win32.
    """

    def __init__(self, *, on_tap: Any = None, class_name: str = "") -> None:
        self.alive = True
        self.shown = False
        self.click_through = False
        # A plausible bubble size: only its width matters to the placement maths.
        self.rect = (0, 0, 320, 60)
        self.frames: list[str] = []
        self.on_tap = on_tap
        self.class_name = class_name
        self.buttons: tuple[Any, ...] = ()
        self.pressing = False

    def set_hot_spots(self, buttons: Any) -> None:
        self.buttons = tuple(buttons)

    def button_at(self, x: int, y: int) -> str | None:
        for button in self.buttons:
            if button.x <= x < button.x + button.size and button.y <= y < button.y + button.size:
                return str(button.name)
        return None

    def press(self, x: int, y: int) -> str | None:
        """Fire the tap callback for a window-local point, the way ``_release`` would."""
        name = self.button_at(x, y)
        if name is not None and self.on_tap is not None:
            self.on_tap(name)
        return name

    def start(self, *, x: int, y: int, width: int, height: int) -> bool:
        self.rect = (x, y, width, height)
        return True

    def present(self, url: str) -> bool:
        """Adopt the picture's size, exactly like the real window does.

        ``AlphaWindow`` 的矩形跟着喂进来的帧走（``_paint_pending`` 用帧的宽高改 ``_rect``），
        所以这里也得读一次 PNG —— 假体停在固定 320 宽的话，"靠近屏幕右缘翻边"那条判断就会
        按一个屏上并不存在的宽度算，测出一条生产里不会发生的失败。
        """
        import base64
        import io

        from PIL import Image

        payload = base64.b64decode(url.split(",", 1)[1])
        with Image.open(io.BytesIO(payload)) as image:
            self.rect = (self.rect[0], self.rect[1], image.width, image.height)
        self.frames.append(url)
        return True

    def move_to(self, x: int, y: int) -> None:
        self.rect = (x, y, self.rect[2], self.rect[3])

    def set_visible(self, shown: bool) -> bool:
        self.shown = shown
        return True

    def set_click_through(self, on: bool) -> bool:
        self.click_through = on
        return True

    def keep_topmost(self) -> None:
        return None

    def close(self) -> None:
        self.alive = False


class _FakeThinkingWindow(_FakeCaptionWindow):
    """Same window shape, but counting how often visibility was touched.

    The count is the point: the blink must be ``ShowWindow`` and nothing else, so a
    controller that re-presented the picture every poll tick would still "look right"
    while paying per-frame cost on the one window meant to carry none.
    """

    def __init__(self, *, on_tap: Any = None, class_name: str = "") -> None:
        super().__init__(on_tap=on_tap, class_name=class_name)
        self.visits = 0

    def set_visible(self, shown: bool) -> bool:
        self.visits += 1
        return super().set_visible(shown)


class _QuietCompositor(_FakeCompositor):
    """A compositor for the bubbles, dead to the pointer poller.

    ``alive`` is what :meth:`PetController._sync_to_cursor` asks first; saying no sends
    it down the "no window handle yet" path, which is the one that does not need Win32.
    """

    alive = False
    hwnd = 0
    pressing = False


def _snapshot(turns: list[tuple[str, str]]) -> Any:
    return types.SimpleNamespace(history=[types.SimpleNamespace(role=r, text=t) for r, t in turns])


def _bubbled(
    box: tuple[int, int, int, int] = (700, 200, 1120, 840),
    *,
    screen: tuple[int, int] = (1920, 1080),
) -> tuple[Any, _FakeCompositor, _FakeCaptionWindow]:
    """A shown pet with a compositor and a bubble window, and no real Win32."""
    pet = controller()
    compositor = _FakeCompositor(box)
    caption = _FakeCaptionWindow()
    pet._compositor = cast(Any, compositor)
    pet._caption_window = cast(Any, caption)
    pet._shown = True
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr("jarvis.ui.pet._primary_screen", lambda: screen)
    return pet, compositor, caption


class TestSpeechBubble:
    """她说的话显示在她**旁边**的那扇小窗里，不是画在她身上。

    旧做法把字画进她那张帧的左上角，正好压着人；而把那张帧加宽到能放字的代价是
    帧率——每一帧都要重新 premultiply 一次（本机实测 420x640 13.8 ms），加宽一倍就
    等于她说话时画面卡一半。所以气泡是独立的一扇窗：静态文字画一次，整块点穿，
    停在她右边；右边放不下就翻到她左边。
    """

    def test_the_last_answer_appears_in_a_bubble_on_her_right(self) -> None:
        pet, compositor, caption = _bubbled()
        try:
            pet.on_snapshot(_snapshot([("user", "内存怎么样"), ("assistant", "用了 12.4 GB")]))

            assert caption.shown is True
            assert len(caption.frames) == 1 and caption.frames[0].startswith("data:image/png")
            assert caption.rect[0] >= compositor.screen_rect[2], "气泡必须在她右侧，不能压在身上"
            assert caption.click_through is True, "一句会飘走的话不该挡住底下的点击"
        finally:
            pet._caption_window = None

    def test_a_new_question_takes_it_away(self) -> None:
        """The next thing she says replaces it; a question in between silences her."""
        pet, _compositor, caption = _bubbled()
        try:
            pet.on_snapshot(_snapshot([("assistant", "第一答")]))
            pet.on_snapshot(_snapshot([("assistant", "第一答"), ("user", "第二个问题")]))
            assert caption.shown is False
        finally:
            pet._caption_window = None

    def test_near_the_right_edge_it_flips_to_her_left(self) -> None:
        """Reading it is the point; a bubble clipped by the screen is half missing."""
        pet, _compositor, caption = _bubbled((1450, 200, 1870, 840))
        try:
            pet.on_snapshot(_snapshot([("assistant", "用了 12.4 GB，还有 3.6 GB 空着")]))
            assert caption.rect[0] < 1450, "右边放不下就该翻到左边"
            assert caption.rect[0] + caption.rect[2] <= 1450
        finally:
            pet._caption_window = None

    def test_the_bubble_follows_her_when_she_moves(self) -> None:
        pet, compositor, caption = _bubbled()
        try:
            pet.on_snapshot(_snapshot([("assistant", "第一答")]))
            first = caption.rect[0]
            compositor.screen_rect = (300, 200, 720, 840)
            pet._sync_caption()
            assert caption.rect[0] < first, "她被拖到左边，气泡要跟过去"
        finally:
            pet._caption_window = None

    def test_repeating_the_same_sentence_does_not_buy_more_time(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Snapshots arrive on every voice transition; none of them is a new answer."""
        clock = [100.0]
        pet, _compositor, caption = _bubbled()
        monkeypatch.setattr("jarvis.ui.pet.time.monotonic", lambda: clock[0])
        try:
            pet.on_snapshot(_snapshot([("assistant", "第一句")]))
            clock[0] = 106.0
            pet.on_snapshot(_snapshot([("assistant", "第一句")]))
            clock[0] = 119.0
            assert pet._live_caption() == "第一句", "交期还是第一次那个"
            clock[0] = 121.0
            assert pet._live_caption() == ""
            pet._sync_caption()
            assert caption.shown is False, "过期之后轮询要把窗收掉"
        finally:
            pet._caption_window = None

    def test_hiding_her_takes_the_bubble_with_it(self) -> None:
        pet, _compositor, caption = _bubbled()
        try:
            pet.on_snapshot(_snapshot([("assistant", "一句话")]))
            pet.hide()
            assert caption.shown is False
        finally:
            pet._caption_window = None

    def test_a_bubble_with_nowhere_to_go_is_not_an_error(self) -> None:
        """Panel mode has no compositor: the page draws its own caption there."""
        pet = controller()
        pet.on_snapshot(_snapshot([("assistant", "照常说")]))
        assert pet._caption_window is None, "没有合成窗就不该凭空开一扇气泡窗"

    def test_a_snapshot_that_is_not_one_changes_nothing(self) -> None:
        """Duck-typed on purpose, so it has to survive whatever else arrives."""
        pet, _compositor, caption = _bubbled()
        try:
            for junk in (None, object(), _snapshot([]), _snapshot([("assistant", "")])):
                pet.on_snapshot(junk)
            assert caption.frames == [] and caption.shown is False
        finally:
            pet._caption_window = None


class TestThinkingBubble:
    """她那句「思考中」是**第二扇**气泡窗，画一次、只闪不问。

    和回答气泡分开有两理由：共用一扇窗，答案一落地就被下一次闪断吞掉，或者回答
    过期把指示一起带走。实现上守两条：PNG 只编一次（``build_bubble`` 缓存的是
    tile，PNG+base64 每次调用都重跑一遍，而轮询是 20 Hz），以及"闪"必须是
    ``ShowWindow`` —— 每半秒重画一张就等于给一扇本该零成本的窗加上 per-frame 开销。
    """

    @staticmethod
    def _windows(monkeypatch: pytest.MonkeyPatch) -> list[_FakeThinkingWindow]:
        """Swap the Win32 window class for a recorder, and hand back what got built."""
        made: list[_FakeThinkingWindow] = []

        def _build(*, on_tap: Any = None, class_name: str = "") -> _FakeThinkingWindow:
            window = _FakeThinkingWindow(on_tap=on_tap, class_name=class_name)
            made.append(window)
            return window

        monkeypatch.setattr("jarvis.ui.pet.AlphaWindow", _build)
        return made

    def test_it_cycles_prebuilt_frames_instead_of_drawing_every_tick(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """动效是真的在动，但一张也不许现画。

        轮询是 20 Hz。每 tick 都 present 就等于把"气泡为什么不占帧"那笔账重新欠上；
        而每帧都重新渲染 PNG 更糟。所以这里钉三件事：换帧的张数远小于 tick 数、出现的
        图片只有那 8 张（说明是缓存里的）、可见性只碰一次（不再靠闪）。
        """
        pet, _compositor, _caption = _bubbled()
        made = self._windows(monkeypatch)
        clock = [1000.0]
        monkeypatch.setattr("jarvis.ui.pet.time.monotonic", lambda: clock[0])
        try:
            pet.set_thinking(True)
            for step in range(20):  # one whole second of polling at 20 Hz
                clock[0] = 1000.0 + step * 0.05
                pet._sync_thinking()
            assert len(made) == 1, "二十次轮询只该开一扇窗"
            assert made[0].class_name == "XiaoYePetThinking"
            shown = made[0].frames
            assert 1 < len(shown) < 20, f"既要在动，又不能被 20 Hz 轮询牵着走：{len(shown)}"
            assert len(set(shown)) <= 8, "换的必须是预渲染那 8 张，不是现画的"
            assert made[0].visits == 1, "卡片该一直挂着，动效自己会动"
            assert made[0].click_through is True
            assert made[0].rect[0] >= 1120, "气泡停在她右侧，不能压在身上"
        finally:
            pet._thinking_window = None

    def test_switching_loader_kind_swaps_the_picture_set(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """设置里换一种动效，她那张卡要跟着换，而且不能靠重启。"""
        pet, _compositor, _caption = _bubbled()
        made = self._windows(monkeypatch)
        clock = [1000.0]
        monkeypatch.setattr("jarvis.ui.pet.time.monotonic", lambda: clock[0])
        try:
            pet.set_thinking(True)
            pet._sync_thinking()
            before = list(made[0].frames)
            pet.set_thinking_loader("ring")
            for step in range(2, 20):
                clock[0] = 1000.0 + step * 0.05
                pet._sync_thinking()
            assert made[0].frames[: len(before)] == before, "换类型之前那张该原样保留"
            assert made[0].frames[len(before) - 1 :] != before[-1:], "换完必须画出别的图"
        finally:
            pet._thinking_window = None

    def test_turning_it_off_hides_it_and_then_stops_touching_the_window(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The stuck-on bubble is the failure the operator reports as "她还在想"."""
        pet, _compositor, _caption = _bubbled()
        made = self._windows(monkeypatch)
        try:
            pet.set_thinking(True)
            pet._sync_thinking()
            pet.set_thinking(False)
            pet._sync_thinking()
            after_hide = made[0].visits
            assert made[0].shown is False
            for _ in range(40):
                pet._sync_thinking()
            assert made[0].visits == after_hide, "关掉之后每 tick 再 ShowWindow 一次是白干"
        finally:
            pet._thinking_window = None

    def test_a_hidden_pet_opens_nothing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """She is in the tray: an indicator for a figure nobody can see is a stray card."""
        pet, _compositor, _caption = _bubbled()
        made = self._windows(monkeypatch)
        pet._shown = False
        pet.set_thinking(True)
        pet._sync_thinking()
        assert made == []

    def test_panel_mode_without_a_compositor_opens_nothing(self) -> None:
        pet = controller()
        pet.set_thinking(True)
        pet._sync_thinking()
        assert pet._thinking_window is None, "没有合成窗就不该凭空开一扇指示窗"

    def test_it_stacks_above_the_answer_in_the_same_column(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Both can be up at once: two bubbles fighting for one spot read as one broken one."""
        pet, _compositor, caption = _bubbled()
        pet.on_snapshot(_snapshot([("assistant", "用了 12.4 GB，还有 3.6 GB 空着")]))
        answer_left, answer_top = caption.rect[0], caption.rect[1]
        made = self._windows(monkeypatch)
        try:
            pet.set_thinking(True)
            pet._sync_thinking()
            assert made[0].rect[0] == answer_left, "两句话要排同一列，不要一左一右"
            assert made[0].rect[1] + made[0].rect[3] <= answer_top, "思考气泡要在回答上方"
        finally:
            pet._thinking_window = None


class TestTheCardsFollowHerSkin:
    """她换了身衣服，桌上那两张卡跟着换 —— 那两张是 Python 画的。

    ``theme.ts`` 里那张配色表只有一份，PIL 这边没有副本，所以颜色是页面报上来的（桥面
    ``pet_palette``）。这里守控制器这一头：报到了、存下了、挂着的那张卡真的重画了，
    而重复报同一个颜色不该把她桌上的东西抖一遍。
    """

    AMBER = bubble_palette(0x3A2408, 0xFFB04A, 0xFFE0B0)

    @staticmethod
    def _body(url: str, width: int) -> tuple[int, int, int, int]:
        """卡片本体的颜色：取中间那一列、第一行字上方那个像素。"""
        import base64
        import io

        from PIL import Image

        image = Image.open(io.BytesIO(base64.b64decode(url.split(",", 1)[1]))).convert("RGBA")
        return cast("tuple[int, int, int, int]", image.getpixel((width // 2, 2)))

    def test_a_sentence_on_the_desk_is_painted_in_the_colours_she_was_given(self) -> None:
        pet, _compositor, caption = _bubbled()
        try:
            pet.set_palette(self.AMBER)
            pet.on_snapshot(_snapshot([("assistant", "用了 12.4 GB")]))
            url = caption.frames[-1]
            assert self._body(url, caption.rect[2]) == self.AMBER.fill
        finally:
            pet._caption_window = None

    def test_changing_the_colour_repaints_the_sentence_already_hanging_there(self) -> None:
        """她正说着话的时候换了皮肤：那张卡不能等下一句话才变色。"""
        pet, _compositor, caption = _bubbled()
        try:
            pet.on_snapshot(_snapshot([("assistant", "用了 12.4 GB")]))
            before = self._body(caption.frames[-1], caption.rect[2])
            assert before != self.AMBER.fill, "本来就是这颜色就等于什么都没测到"
            pet.set_palette(self.AMBER)
            assert self._body(caption.frames[-1], caption.rect[2]) == self.AMBER.fill
        finally:
            pet._caption_window = None

    def test_changing_the_colour_swaps_the_thinking_card_without_a_new_window(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pet, _compositor, _caption = _bubbled()
        made = TestThinkingBubble._windows(monkeypatch)
        clock = [1000.0]
        monkeypatch.setattr("jarvis.ui.pet.time.monotonic", lambda: clock[0])
        try:
            pet.set_thinking(True)
            pet._sync_thinking()
            pet.set_palette(self.AMBER)
            clock[0] = 1000.2
            pet._sync_thinking()
            assert len(made) == 1, "换颜色不该再开一扇窗"
            url = made[0].frames[-1]
            assert self._body(url, made[0].rect[2]) == self.AMBER.fill
        finally:
            pet._thinking_window = None

    def test_re_stating_the_same_colour_is_not_a_repaint(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """两扇窗都会报自己那身颜色，第二声不该把已渲染的八张图丢掉。"""
        pet, _compositor, _caption = _bubbled()
        TestThinkingBubble._windows(monkeypatch)
        pet.set_palette(self.AMBER)
        pet.set_thinking(True)
        pet._sync_thinking()
        frames = pet._thinking_frames
        assert frames is not None
        pet.set_palette(self.AMBER)
        assert pet._thinking_frames is frames, "重复推同一个颜色，不该现画第二遍"


class TestTheIconsOnHerCard:
    """那张卡是点穿的，只有图标那一小块不点穿 —— 所以"按到没有"是两件事。

    一帧图里画了什么，和窗口认为自己能接住哪一击，是两条分开的路：轮询每 20 Hz 决定一次
    （窗口自己看不见鼠标，``WS_EX_TRANSPARENT`` 时连 ``WM_MOUSEMOVE`` 都收不到），按下之后
    由窗口的 ``_release`` 决定算不算一次点击。这里两条都钉，外加"按在字上不算按在按钮上"。
    """

    @staticmethod
    def _card(
        monkeypatch: pytest.MonkeyPatch, on_action: Any = None
    ) -> tuple[Any, _FakeCaptionWindow]:
        monkeypatch.setattr("jarvis.ui.pet._primary_screen", lambda: (1920, 1080))
        pet = controller(on_action=on_action)
        caption = _FakeCaptionWindow(on_tap=pet.on_card_tap)
        pet._compositor = cast(Any, _FakeCompositor((700, 200, 1120, 840)))
        pet._caption_window = cast(Any, caption)
        pet._shown = True
        return pet, caption

    def test_the_card_takes_a_click_only_where_an_icon_is(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pet, caption = self._card(monkeypatch)
        pet.set_actions((BUBBLE_STOP, BUBBLE_ADD))
        pet.set_caption("用了 12.4 GB")

        # Read through a function: asserting ``x is False`` on a bool narrows its type for
        # the rest of the block, and the next line's ``is True`` would then be "unreachable"
        # to mypy while being the whole point of the test.
        def lit() -> bool:
            return caption.click_through

        assert lit() is True, "刚画出来的卡不该先把桌面堵住"

        stop = caption.buttons[0]
        monkeypatch.setattr(
            "jarvis.ui.pet._cursor_position",
            lambda: (caption.rect[0] + stop.x + 8, caption.rect[1] + stop.y + 8),
        )
        pet._sync_caption()
        assert lit() is False, "指针在图标上，这一击是发给她的"

        monkeypatch.setattr(
            "jarvis.ui.pet._cursor_position", lambda: (caption.rect[0] + 2, caption.rect[1] + 2)
        )
        pet._sync_caption()
        assert lit() is True, "离开图标还得把点击还给底下的窗口"

    def test_a_press_in_progress_is_not_re_decided(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """手指还按着的时候把卡变回点穿，这一下就成了别人窗口上的点击。"""
        pet, caption = self._card(monkeypatch)
        pet.set_actions((BUBBLE_STOP, BUBBLE_ADD))
        pet.set_caption("用了 12.4 GB")
        caption.click_through = False
        caption.pressing = True
        monkeypatch.setattr("jarvis.ui.pet._cursor_position", lambda: (0, 0))
        pet._sync_caption()
        assert caption.click_through is False

    def test_pressing_the_stop_icon_tells_the_shell(self, monkeypatch: pytest.MonkeyPatch) -> None:
        hits: list[str] = []
        pet, caption = self._card(monkeypatch, on_action=hits.append)
        pet.set_actions((BUBBLE_STOP, BUBBLE_ADD))
        pet.set_caption("用了 12.4 GB")
        stop = caption.buttons[0]
        assert caption.press(stop.x + 8, stop.y + 8) == BUBBLE_STOP
        assert hits == [BUBBLE_STOP]

    def test_a_press_on_the_words_is_not_an_action(self, monkeypatch: pytest.MonkeyPatch) -> None:
        hits: list[str] = []
        pet, caption = self._card(monkeypatch, on_action=hits.append)
        pet.set_actions((BUBBLE_STOP, BUBBLE_ADD))
        pet.set_caption("用了 12.4 GB，还有 3.6 GB 空着")
        assert caption.press(4, caption.rect[3] // 2) is None
        assert hits == [], "字不是按钮"

    def test_re_stating_the_icons_is_not_a_repaint(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """任务每变一次阶段都会重报一遍该挂哪些图，图没变就不该重画。"""
        pet, caption = self._card(monkeypatch)
        pet.set_actions((BUBBLE_STOP, BUBBLE_ADD))
        pet.set_caption("用了 12.4 GB")
        painted = len(caption.frames)
        pet.set_actions((BUBBLE_STOP, BUBBLE_ADD))
        assert len(caption.frames) == painted

    def test_the_icons_appear_on_the_card_when_the_work_starts(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """没有图标时那张卡上什么都没有；有活儿时它得自己长出来。"""
        pet, caption = self._card(monkeypatch)
        pet.set_caption("用了 12.4 GB")
        plain = caption.buttons
        pet.set_actions((BUBBLE_STOP,))
        assert len(caption.buttons) == 1 and caption.buttons != plain
        assert caption.frames[-1] != caption.frames[0], "图标变了必须重画那张卡"

    def test_a_press_with_nobody_listening_does_not_raise(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """壳子还没接上的时候按到图标，不该把窗口的消息线程带走。"""
        pet, caption = self._card(monkeypatch, on_action=None)
        pet.set_actions((BUBBLE_ADD,))
        pet.set_caption("用了 12.4 GB")
        icon = caption.buttons[0]
        assert caption.press(icon.x + 8, icon.y + 8) == BUBBLE_ADD


class TestTheCardIsDrivenFromTheRealWiring:
    """注册表 / 麦克风 → 控制器 → 那扇窗，走一遍真链路。

    上面那组单元测试各测一半：控制器测窗口怎么动，桥面测 ``set_thinking`` 有没有被叫。
    中间那条缝正是"代码都在、用户什么也看不见"会藏身的地方，所以这里用**真的**
    ``PetController`` 和**真的**轮询入口 ``_poll_once``，假件只放在 Win32 那一层。
    """

    @staticmethod
    def _shown_pet(
        monkeypatch: pytest.MonkeyPatch,
    ) -> tuple[PetController, list[Any], list[float]]:
        """A pet with a compositor, a fake window factory, and a clock we own.

        The clock matters: the card blinks on a half-second grid, so "is it visible right
        now" is only a meaningful question at a known instant.
        """
        made: list[Any] = []

        def _build(*, on_tap: Any = None, class_name: str = "") -> _FakeThinkingWindow:
            window = _FakeThinkingWindow(on_tap=on_tap, class_name=class_name)
            made.append(window)
            return window

        clock = [1000.0]  # an even beat: the grid says "on"
        monkeypatch.setattr("jarvis.ui.pet.AlphaWindow", _build)
        monkeypatch.setattr("jarvis.ui.pet.time.monotonic", lambda: clock[0])
        monkeypatch.setattr("jarvis.ui.pet._primary_screen", lambda: (1920, 1080))
        pet = controller()
        pet._compositor = cast(Any, _QuietCompositor())
        pet._shown = True
        return pet, made, clock

    def test_a_typed_question_lights_the_card_and_the_answer_puts_it_out(
        self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from jarvis.app.chat_service import ChatService
        from jarvis.app.turns import TurnRegistry
        from jarvis.ui.state_bridge import StateBridge
        from tests.test_ui_desktop import _bridge

        inside = threading.Event()
        release = threading.Event()

        class Slow:
            provider_name = "slow"
            model = "slow-model"

            def complete(self, messages: Any, *, options: Any = None) -> Any:
                from jarvis.llm.types import ChatResponse

                inside.set()
                assert release.wait(5.0), "没放行就答完，测的就不是等待而是别的"
                return ChatResponse(content="答完了", model="slow-model")

            def stream(self, messages: Any, *, options: Any = None) -> Any:
                return iter(())

        pet, made, clock = self._shown_pet(monkeypatch)

        def lit() -> bool:
            """Read the card through a call: mypy would otherwise "know" it is True."""
            return bool(made[0].shown)

        chat = ChatService(lambda: Slow())
        chat.start()
        bridge = _bridge(tmp_path, chat=chat, state=StateBridge(), turns=TurnRegistry())
        bridge.attach_pet(pet)

        started = bridge.chat_send("慢慢想")
        # 不"等模型进来再断言"：任务一被接受就该点灯。亮得准不准由 ``TurnRegistry.start``
        # 那条 announce 保证（确定性钉在 test_concurrent_conversations 里），这条测的是
        # 从任务表一路到那扇窗的接线。
        pet._poll_once()
        assert made and lit() is True, "模型还在想，她头上就该有那张卡"
        assert inside.wait(5.0)

        release.set()
        assert bridge._turns is not None and bridge._turns.wait(str(started["task_id"]), 5.0)
        pet._poll_once()
        assert lit() is False, "答完了还亮着，用户看到的就叫「她卡住了」"
        for _ in range(10):
            clock[0] += 0.1
            pet._poll_once()
        assert lit() is False, "收掉之后不该被下一次闪又点回来"

    def test_a_spoken_question_lights_the_same_card_without_the_task_table(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """说话那条路不经过任务表：``speech_end`` 点灯，回到 listening 收灯。"""
        from jarvis.core.events import PipelineEvent
        from jarvis.ui.lifecycle import ShellLifecycle

        pet, made, _clock = self._shown_pet(monkeypatch)
        lifecycle = ShellLifecycle(None, tray=None, pet=cast(Any, pet))

        lifecycle.on_event(PipelineEvent(kind="speech_end"))
        pet._poll_once()
        assert made and made[0].shown is True, "她听到了一句要想一想的话"

        lifecycle.on_event(PipelineEvent(kind="reply", text="答完了"))
        pet._poll_once()
        assert made[0].shown is False

    def test_a_turn_that_heard_nothing_still_puts_the_card_out(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """ASR 空串那一轮永远不会发回答 —— 只盯回答事件就会把卡片永远点着。"""
        from jarvis.core.events import PipelineEvent
        from jarvis.ui.lifecycle import ShellLifecycle

        pet, made, _clock = self._shown_pet(monkeypatch)
        lifecycle = ShellLifecycle(None, tray=None, pet=cast(Any, pet))

        lifecycle.on_event(PipelineEvent(kind="speech_end"))
        pet._poll_once()
        assert made and made[0].shown is True

        lifecycle.on_event(PipelineEvent(kind="state", text="listening"))
        pet._poll_once()
        assert made[0].shown is False, "回到聆听就是把这一轮翻篇了，不管它答没答"

    def test_the_two_doors_do_not_switch_each_other_off(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """键盘在等的时候，一句说完的话不该把那张卡收走。"""
        pet, made, _clock = self._shown_pet(monkeypatch)

        pet.set_thinking(True, source="turn")
        pet.set_thinking(True, source="voice")
        pet.set_thinking(False, source="voice")
        pet._poll_once()
        assert made and made[0].shown is True, "还有一扇门在等，灯就该亮着"

        pet.set_thinking(False, source="turn")
        pet._poll_once()
        assert made[0].shown is False


class TestParkingTheRenderer:
    """Moving the browser window off screen must not hand it the keyboard.

    The packaged build proved the failure: the pet's own layered window ended up the
    foreground window after ``ShowWindow(SW_SHOW)``, so keystrokes anywhere on the
    machine went into a window that ignores them. Both windows are shown without
    activation now, and the parked one still has to be *shown* -- a hidden WebView2 is
    an occluded one, and Chromium stops painting frames for it.
    """

    def test_the_park_call_carries_no_activate(self) -> None:
        assert PARK_FLAGS & SWP_NOACTIVATE

    def test_the_park_call_still_shows_the_window(self) -> None:
        assert PARK_FLAGS & SWP_SHOWWINDOW, "hidden instead of parked would freeze the figure"


class TestKeyboardGoesBack:
    """Putting a window on screen must not take the keyboard away from somewhere else.

    Measured on the packaged build: after the pet appeared, ``GetForegroundWindow()``
    was the renderer window parked at -32000 -- an invisible foreground window, which
    means every keystroke on the machine went nowhere until the operator clicked
    another window. The pet is summoned by the wake word now, so this fires at exactly
    the moment someone is about to type.
    """

    def test_nothing_is_restored_to_no_window_or_to_one_of_ours(self) -> None:
        assert _restore_foreground(0, (7, 9)) is False
        assert _restore_foreground(7, (7, 9)) is False, "the renderer window is at -32000"
        assert _restore_foreground(9, (7, 9)) is False

    def test_the_window_that_had_the_keyboard_gets_it_back(self, monkeypatch: Any) -> None:
        calls: list[int] = []

        class FakeUser32:
            def IsWindow(self, handle: Any) -> int:  # noqa: N802 - Win32 的拼写
                return 1

            def SetForegroundWindow(self, handle: Any) -> int:  # noqa: N802
                calls.append(int(handle.value or 0))  # c_void_p 不能直接 int()
                return 1

        monkeypatch.setattr(ctypes, "windll", types.SimpleNamespace(user32=FakeUser32()))
        assert _restore_foreground(4242, (7, 9)) is True
        assert calls == [4242]

    def test_a_window_that_vanished_in_the_meantime_is_left_alone(self, monkeypatch: Any) -> None:
        class FakeUser32:
            def IsWindow(self, handle: Any) -> int:  # noqa: N802 - Win32 的拼写
                return 0

            def SetForegroundWindow(self, handle: Any) -> int:  # noqa: N802
                raise AssertionError("不该被调用")

        monkeypatch.setattr(ctypes, "windll", types.SimpleNamespace(user32=FakeUser32()))
        assert _restore_foreground(4242, ()) is False

    def test_showing_her_captures_the_foreground_before_it_shows_a_window(
        self, monkeypatch: Any
    ) -> None:
        """Ordering is the whole fix: after ``show()`` it is already too late to ask."""
        fake_webview(monkeypatch)
        seen: list[int] = []

        def record(hwnd: int, ours: tuple[int, ...]) -> bool:
            seen.append(hwnd)
            return False

        monkeypatch.setattr("jarvis.ui.pet._foreground_window", lambda: 4242)
        monkeypatch.setattr("jarvis.ui.pet._restore_foreground", record)
        controller().show()
        assert seen == [4242]


class TestSkinReachesHer:
    """她一页、仪表盘一页 —— 皮肤必须同时到两页。

    用户原话：「页面有颜色改变的话宠物颜色也得变化吧」。宠物是另一个页面（``?mode=pet``），
    有自己的一份图和材质；仪表盘自己换色之后再告诉壳子一声，由壳子把 id 推给这一页。
    """

    def test_the_cue_carries_the_skin_id_as_a_quoted_string(self) -> None:
        pet = controller()
        window = FakePetWindow()
        pet._window = window

        assert pet.apply_skin("amber") is True

        assert any("__jarvisSetSkin" in script for script in window.scripts), window.scripts
        assert '"amber"' in " ".join(window.scripts), "id 要带引号进脚本，不能裸拼"

    def test_an_id_that_is_not_shaped_like_one_cannot_break_out(self) -> None:
        """走到这里之前桥面已按形状挡过一层，但引号仍然由 ``json.dumps`` 给。

        判据不是"脚本里没有 alert"（它当然有，那是 payload 的一部分），而是**未转义的引号
        只有最外层那一对** —— 里面的引号全是 ``\\"``，所以 payload 只能待在字符串里。
        """
        pet = controller()
        window = FakePetWindow()
        pet._window = window

        pet.apply_skin('x"); alert(1); //')

        script = " ".join(window.scripts)
        assert script.count('"') - script.count('\\"') == 2, script

    def test_no_page_no_cue(self) -> None:
        """宠物没开的时候不该凭空去 evaluate_js（那会抛，而且没有任何东西可改）。"""
        pet = controller()
        assert pet.apply_skin("green") is False
