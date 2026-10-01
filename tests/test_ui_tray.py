"""The tray icon: what it claims, how it is built, and what happens when it can't be.

Nothing here starts a real notification area -- ``pystray`` is injected as a fake
module in ``sys.modules``, which is also the only way to test the failure that
matters: a machine where the icon cannot be created must not be a machine where the
X hides the app somewhere invisible.
"""

from __future__ import annotations

import sys
import types
from typing import Any, ClassVar

import pytest

from jarvis.ui.tray import (
    STATUS_HEARING,
    STATUS_OFF,
    STATUS_WAITING,
    STATUS_WORKING,
    TITLES,
    TrayIcon,
    _menu_items,
    draw_icon,
)


class FakeMenuItem:
    def __init__(
        self,
        text: str,
        action: Any,
        checked: Any = None,
        radio: bool = False,
        default: bool = False,
        visible: bool = True,
        enabled: bool = True,
    ) -> None:
        self.text = text
        self.action = action
        self.checked = checked
        self.default = default


class FakeMenu:
    SEPARATOR = "<separator>"

    def __init__(self, *items: Any) -> None:
        self.items = list(items)


class FakeIcon:
    """Stands in for ``pystray.Icon`` and remembers what the shell asked for."""

    created: ClassVar[list[FakeIcon]] = []

    def __init__(self, name: str, icon: Any = None, title: str = "", menu: Any = None) -> None:
        self.name = name
        self.icon = icon
        self.title = title
        self.menu = menu
        self.visible = False
        self.stopped = False
        self.notifications: list[tuple[str, str]] = []
        FakeIcon.created.append(self)

    def run_detached(self, setup: Any = None) -> None:
        self.visible = True

    def stop(self) -> None:
        self.stopped = True

    def notify(self, message: str, title: str | None = None) -> None:
        self.notifications.append((message, title or ""))


def fake_pystray(monkeypatch: Any) -> types.ModuleType:
    module = types.ModuleType("pystray")
    module.Icon = FakeIcon  # type: ignore[attr-defined]
    module.Menu = FakeMenu  # type: ignore[attr-defined]
    module.MenuItem = FakeMenuItem  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "pystray", module)
    FakeIcon.created = []
    return module


class Builder:
    """A namespace for :func:`_menu_items`, which is pure for the same reason."""

    MenuItem = FakeMenuItem
    Menu = FakeMenu


def labels(items: tuple[Any, ...]) -> list[str]:
    """Menu texts with separators collapsed to ``|`` -- the shape a human reads."""
    return [getattr(item, "text", "|") for item in items]


class TestDrawing:
    @pytest.fixture(autouse=True)
    def _require_pillow(self) -> None:
        pytest.importorskip("PIL", reason="托盘位图需要 Pillow")

    def test_every_state_draws_a_distinct_bitmap(self) -> None:
        frames = {status: draw_icon(status).tobytes() for status in TITLES}
        assert len(set(frames.values())) == len(frames), "the states must be tellable apart"

    def test_bitmap_is_the_size_pystray_scales_from(self) -> None:
        image = draw_icon(STATUS_WAITING, pixels=48)
        assert image.size == (48, 48)
        assert image.mode == "RGBA"

    def test_unknown_state_is_drawn_as_mic_closed(self) -> None:
        assert draw_icon("daydreaming").tobytes() == draw_icon(STATUS_OFF).tobytes()


class TestMenuShape:
    def test_left_click_brings_the_window_back(self) -> None:
        calls: list[str] = []
        items = _menu_items(
            Builder,
            on_activate=lambda: calls.append("activate"),
            on_quit=lambda: calls.append("quit"),
            on_toggle_pet=lambda: calls.append("pet"),
            pet_shown=lambda: False,
        )
        defaults = [item for item in items if getattr(item, "default", False)]
        assert [item.text for item in defaults] == ["显示主界面"]
        defaults[0].action()
        assert calls == ["activate"]

    def test_quit_is_last_and_the_pet_entry_tracks_the_window(self) -> None:
        items = _menu_items(
            Builder,
            on_activate=lambda: None,
            on_quit=lambda: None,
            on_toggle_pet=lambda: None,
            pet_shown=lambda: True,
        )
        assert labels(items)[-1] == "退出小夜"
        pet = next(item for item in items if getattr(item, "text", "") == "桌面宠物")
        assert pet.checked(None) is True

    def test_no_pet_hook_means_no_pet_entry(self) -> None:
        """A shell without a pet window must not offer a menu item that does nothing."""
        items = _menu_items(
            Builder,
            on_activate=lambda: None,
            on_quit=lambda: None,
            on_toggle_pet=None,
            pet_shown=lambda: False,
        )
        assert labels(items) == ["显示主界面", "|", "退出小夜"]


class TestTrayIcon:
    def test_start_puts_up_an_icon_saying_the_mic_is_closed(self, monkeypatch: Any) -> None:
        fake_pystray(monkeypatch)
        tray = TrayIcon(on_activate=lambda: None, on_quit=lambda: None)
        assert tray.start() is True
        assert tray.running is True
        assert FakeIcon.created[0].title == TITLES[STATUS_OFF]

    def test_status_change_retitles(self, monkeypatch: Any) -> None:
        pytest.importorskip("PIL")
        fake_pystray(monkeypatch)
        tray = TrayIcon(on_activate=lambda: None, on_quit=lambda: None)
        tray.start()
        tray.set_status(STATUS_WAITING)
        assert FakeIcon.created[0].title == TITLES[STATUS_WAITING]

    def test_repeating_the_same_status_does_not_redraw(self, monkeypatch: Any) -> None:
        pytest.importorskip("PIL")
        fake_pystray(monkeypatch)
        tray = TrayIcon(on_activate=lambda: None, on_quit=lambda: None)
        tray.start()
        tray.set_status(STATUS_HEARING)
        first = FakeIcon.created[0].icon
        tray.set_status(STATUS_HEARING)
        assert FakeIcon.created[0].icon is first

    def test_unknown_status_shows_the_listening_claim(self, monkeypatch: Any) -> None:
        """A state the shell invented must not leave the icon saying nothing."""
        fake_pystray(monkeypatch)
        tray = TrayIcon(on_activate=lambda: None, on_quit=lambda: None)
        tray.start()
        tray.set_status("daydreaming")
        assert FakeIcon.created[0].title == TITLES[STATUS_WAITING]

    def test_missing_pystray_is_a_reported_failure_not_a_crash(self, monkeypatch: Any) -> None:
        monkeypatch.setitem(sys.modules, "pystray", None)
        tray = TrayIcon(on_activate=lambda: None, on_quit=lambda: None)
        assert tray.start() is False
        assert tray.running is False
        assert "托盘" in tray.detail

    def test_icon_that_refuses_to_run_is_the_same_failure(self, monkeypatch: Any) -> None:
        module = fake_pystray(monkeypatch)

        class Exploding:
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                raise RuntimeError("没有通知区域")

        module.Icon = Exploding  # type: ignore[attr-defined]
        tray = TrayIcon(on_activate=lambda: None, on_quit=lambda: None)
        assert tray.start() is False
        assert "没有通知区域" in tray.detail

    def test_notify_reaches_the_shell(self, monkeypatch: Any) -> None:
        fake_pystray(monkeypatch)
        tray = TrayIcon(on_activate=lambda: None, on_quit=lambda: None)
        tray.start()
        assert tray.notify("收进托盘了") is True
        assert FakeIcon.created[0].notifications[0][0] == "收进托盘了"

    def test_notify_without_an_icon_says_so(self) -> None:
        tray = TrayIcon(on_activate=lambda: None, on_quit=lambda: None)
        assert tray.notify("无处可说") is False

    def test_stop_hides_then_stops_and_survives_a_second_call(self, monkeypatch: Any) -> None:
        fake_pystray(monkeypatch)
        tray = TrayIcon(on_activate=lambda: None, on_quit=lambda: None)
        tray.start()
        icon = FakeIcon.created[0]
        tray.stop()
        assert (icon.stopped, icon.visible, tray.running) == (True, False, False)
        tray.stop()

    def test_working_state_is_the_one_the_operator_sees_while_it_thinks(
        self, monkeypatch: Any
    ) -> None:
        fake_pystray(monkeypatch)
        tray = TrayIcon(on_activate=lambda: None, on_quit=lambda: None)
        tray.start()
        tray.set_status(STATUS_WORKING)
        assert FakeIcon.created[0].title == TITLES[STATUS_WORKING]
