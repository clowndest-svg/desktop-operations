"""The pet window's own arithmetic: where it goes, what is clickable, what it costs.

The ctypes calls are the one part that cannot run meaningfully in a test, so the
fake window below deliberately has no ``native`` handle: everything under test is the
decision (is the pointer over the handle? is this rect usable?), and the decision is
made from numbers the page reported -- not from a live window.
"""

from __future__ import annotations

import sys
import types
from typing import Any

import pytest

from jarvis.ui.pet import (
    GRIP_FALLBACK,
    PET_HEIGHT,
    PET_WIDTH,
    PetController,
    _four_floats,
    _inside_grip,
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
