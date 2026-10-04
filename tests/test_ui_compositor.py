"""The alpha compositor: the arithmetic that decides what the desktop sees.

``UpdateLayeredWindow`` itself cannot be exercised meaningfully in a test, and it is
not where the bugs are. The bugs are in the three pieces of arithmetic around it --
whether a pixel belongs to the grab handle, whether the alpha was premultiplied
(GDI does not do it for you, and skipping it puts a dark fringe on every glowing
line), and whether the pet controller keeps working when no compositor could start.
"""

from __future__ import annotations

import base64
import binascii
import io
from typing import Any, ClassVar, cast

import pytest
from PIL import Image

from jarvis.ui.compositor import (
    BUBBLE_ADD,
    BUBBLE_MAX_WIDTH,
    BUBBLE_STOP,
    DEFAULT_BUBBLE,
    THINKING_FRAMES,
    AlphaWindow,
    BubbleButton,
    bubble_palette,
    build_bubble,
    build_thinking_frames,
    decode_png,
    draw_handle,
    in_rect,
    premultiply,
    wrap_text,
)
from jarvis.ui.pet import PetController

RECT = (0.58, 0.06, 0.34, 0.07)
"""The shipped handle: top-right of a 420x640 window, as fractions of it."""

LONG_ANSWER = "好的，现在给你说说今天的安排：上午十点有一个会议，下午三点再提醒你看一下服务器状态。"
"""A three-line answer, used wherever the card has to reach its ceiling."""


class TestHitTest:
    def test_the_patch_the_page_drew_is_the_patch_that_clicks(self) -> None:
        assert in_rect(RECT, 300, 50, 420, 640)
        assert not in_rect(RECT, 300, 300, 420, 640)  # mid-figure

    def test_the_far_edge_forgives_a_rounded_pixel(self) -> None:
        """Layout reports fractions; the window is in whole device pixels.

        Without the slack the handle would be a pixel narrower than it looks, and a
        click that lands where the pill is drawn and does nothing is the kind of bug
        people describe as "sometimes it doesn't grab".
        """
        right = (RECT[0] + RECT[2]) * 420
        assert in_rect(RECT, int(right) + 1, 50, 420, 640)
        assert not in_rect(RECT, int(right) + 4, 50, 420, 640)


class TestPremultiply:
    def test_opaque_pixels_only_reorder_channels(self) -> None:
        rgba = bytes([10, 20, 30, 255] * 4)
        assert premultiply(rgba, (2, 2)) == bytes([30, 20, 10, 255] * 4)

    def test_half_alpha_halves_the_colour(self) -> None:
        """``AC_SRC_ALPHA`` reads the buffer as *already* multiplied.

        Feeding it straight RGBA is the classic mistake: every soft edge arrives with
        its un-multiplied colour, which on an additive glow reads as a dark outline.
        """
        plane = premultiply(bytes([200, 100, 50, 128]), (1, 1))
        assert plane == bytes([25, 50, 100, 128])

    def test_fully_transparent_pixels_are_black(self) -> None:
        assert premultiply(bytes([255, 255, 255, 0]), (1, 1)) == bytes([0, 0, 0, 0])


class TestDecode:
    def test_a_data_url_becomes_a_premultiplied_buffer(self) -> None:
        from PIL import Image

        surface = Image.new("RGBA", (3, 2), (10, 120, 240, 200))
        buffer = io.BytesIO()
        surface.save(buffer, format="PNG")
        url = "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode()

        data, width, height = decode_png(url)
        assert (width, height) == (3, 2)
        assert len(data) == 3 * 2 * 4
        assert data[:4] == bytes([188, 94, 8, 200])

    def test_a_frame_that_is_not_a_png_raises_where_it_can_be_caught(self) -> None:
        """``present`` swallows this: it runs 20 times a second on the bridge thread,
        and a half-written data URL is not worth an exception in the shell."""
        with pytest.raises((binascii.Error, OSError)):
            decode_png("data:image/png;base64,notanimage")

    def test_presenting_without_a_window_answers_false(self) -> None:
        """The page may keep sending after the pet is hidden; that is not an error."""
        assert AlphaWindow().present("data:image/png;base64,AAAA") is False


class _StubCompositor:
    """Stands in for ``AlphaWindow`` so the controller's decisions can be watched.

    ``rect`` and ``screen_rect`` are deliberately *different* boxes here. Both fakes
    and the real window used to agree that ``rect`` was the desktop rectangle, while
    :class:`jarvis.ui.compositor.AlphaWindow` returned ``(x, y, width, height)`` --
    and the pointer maths that subtracts ``right - left`` from it got a negative
    width. A fake that copies the caller's assumption hides the bug; one that keeps
    both spellings states the promise.
    """

    instances: ClassVar[list[_StubCompositor]] = []

    def __init__(self, *, on_tap: Any = None, **_kwargs: Any) -> None:
        self.on_tap = on_tap
        self.started = False
        self.grabs: list[Any] = []
        self.frames: list[str] = []
        self.visible = False
        self.hwnd = 4321
        self.pressing = False
        self.opaque = False
        self.probed: list[tuple[int, int]] = []
        self.through: list[bool] = []
        self.topmost = 0
        self.captions: list[str] = []
        _StubCompositor.instances.append(self)

    @property
    def alive(self) -> bool:
        return self.started

    @property
    def rect(self) -> tuple[int, int, int, int]:
        return (100, 40, 420, 640)

    @property
    def screen_rect(self) -> tuple[int, int, int, int]:
        return (100, 40, 520, 680)

    def opaque_at(self, x: int, y: int) -> bool:
        self.probed.append((x, y))
        return self.opaque

    def set_click_through(self, on: bool) -> bool:
        if self.through and self.through[-1] == on:
            return False
        self.through.append(on)
        return True

    @property
    def click_through(self) -> bool:
        return not self.through or self.through[-1]

    def keep_topmost(self) -> None:
        self.topmost += 1

    def set_caption(self, text: str) -> None:
        self.captions.append(text)

    def start(self, **_kwargs: Any) -> bool:
        self.started = True
        return True

    def set_grab(self, rect: Any) -> None:
        self.grabs.append(rect)

    def set_visible(self, visible: bool) -> None:
        self.visible = visible

    def present(self, payload: str) -> bool:
        self.frames.append(payload)
        return True

    def close(self) -> None:
        self.started = False


@pytest.fixture
def stub(monkeypatch: Any) -> Any:
    _StubCompositor.instances = []
    monkeypatch.setattr("jarvis.ui.pet.AlphaWindow", _StubCompositor)
    return _StubCompositor


class TestControllerWiring:
    def test_a_frame_only_reaches_the_compositor_while_the_pet_is_shown(
        self, stub: Any, monkeypatch: Any
    ) -> None:
        controller = PetController(base_url="http://127.0.0.1:1/", bridge=object())
        assert controller.present_frame("data:image/png;base64,AA") is False

        controller._compositor = stub()
        assert controller.present_frame("data:image/png;base64,AA") is False
        controller._shown = True
        assert controller.present_frame("data:image/png;base64,AA") is True
        assert stub.instances[0].frames == ["data:image/png;base64,AA"]

    def test_a_frame_that_is_not_text_is_refused(self, stub: Any) -> None:
        """The bridge hands over whatever JS sent; the compositor wants bytes."""
        controller = PetController(base_url="http://x/", bridge=object())
        controller._compositor = stub()
        controller._shown = True
        assert controller.present_frame(None) is False
        assert controller.present_frame({"huge": True}) is False

    def test_the_handle_moves_to_the_compositor_when_the_page_reports_it(self, stub: Any) -> None:
        controller = PetController(base_url="http://x/", bridge=object())
        controller._compositor = stub()
        controller.report_grip({"x": 0.5, "y": 0.1, "width": 0.3, "height": 0.08})
        assert stub.instances[0].grabs == [(0.5, 0.1, 0.3, 0.08)]

    def test_hiding_takes_the_alpha_window_with_the_figure(self, stub: Any) -> None:
        controller = PetController(base_url="http://x/", bridge=object())
        window = stub()
        controller._compositor = window
        controller._shown = True
        controller.hide()
        assert window.visible is False
        assert controller.shown is False

    def test_a_malformed_grip_reaches_nobody(self, stub: Any) -> None:
        controller = PetController(base_url="http://x/", bridge=object())
        controller._compositor = stub()
        controller.report_grip({"x": "not a number"})
        assert stub.instances[0].grabs == []


class TestStartFailure:
    def test_no_measurable_window_means_no_compositor(self, stub: Any) -> None:
        """The fallback is the whole point of the return value.

        With no screen rectangle to copy, the alpha window would be placed by guess,
        so the controller declines and the pet stays exactly as it was -- panel and
        all. If "could not be transparent" took the figure with it, the operator
        would have lost a working feature to gain a prettier one.
        """
        controller = PetController(base_url="http://x/", bridge=object())
        assert controller._start_compositor() is False
        assert stub.instances == []


class TestHandleLabel:
    def test_a_box_too_small_to_hold_a_word_is_left_alone(self) -> None:
        """A 20-pixel pill would be a smudge over the figure, not a label."""
        from PIL import Image

        image = Image.new("RGBA", (404, 601), (0, 0, 0, 0))
        draw_handle(image, (0.5, 0.5, 0.02, 0.01))
        assert image.getpixel((200, 300)) == (0, 0, 0, 0)

    def test_the_pill_is_painted_where_the_page_said_the_handle_is(self) -> None:
        from PIL import Image

        image = Image.new("RGBA", (404, 601), (0, 0, 0, 0))
        draw_handle(image, (0.58, 0.06, 0.34, 0.07))
        # The centre of that rectangle: (0.75 * 404, 0.095 * 601) = (303, 57).
        # ``cast`` because Pillow's getpixel is annotated as returning a union that
        # includes float, and the only thing here is the alpha channel.
        centre = cast("tuple[int, int, int, int]", image.getpixel((303, 57)))
        outside = cast("tuple[int, int, int, int]", image.getpixel((20, 57)))
        assert centre[3] > 0
        assert outside[3] == 0

    def test_the_frame_carries_the_pill_before_it_is_premultiplied(self) -> None:
        """decode_png with a grab rectangle must not lose the figure or the label."""
        from PIL import Image

        surface = Image.new("RGBA", (300, 400), (0, 0, 0, 0))
        for y in range(180, 220):
            for x in range(120, 180):
                surface.putpixel((x, y), (255, 255, 255, 255))
        buffer = io.BytesIO()
        surface.save(buffer, format="PNG")
        url = "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode()

        plain, *_ = decode_png(url)
        labelled, width, height = decode_png(url, (0.5, 0.02, 0.45, 0.06))
        assert (width, height) == (300, 400)
        assert len(labelled) == len(plain) == 300 * 400 * 4
        # The figure survives: a white pixel premultiplies to BGRA white.
        offset = (200 * 300 + 150) * 4
        assert labelled[offset : offset + 4] == bytes([255, 255, 255, 255])
        # And the label is pixels that were not there before.
        assert labelled != plain


class _ScriptWindow:
    """A page that only remembers what the shell told it."""

    def __init__(self) -> None:
        self.scripts: list[str] = []

    def evaluate_js(self, script: str) -> None:
        self.scripts.append(script)


def alpha_pet(stub: Any, monkeypatch: Any, *, cursor: tuple[int, int]) -> tuple[PetController, Any]:
    """A pet whose pixels live in the compositor, with the pointer parked at ``cursor``."""
    controller = PetController(base_url="http://x/", bridge=object())
    window = stub()
    window.started = True
    controller._compositor = window
    controller._shown = True
    controller._window = _ScriptWindow()
    monkeypatch.setattr("jarvis.ui.pet._cursor_position", lambda: cursor)
    monkeypatch.setattr("jarvis.ui.pet._window_rect", lambda _hwnd: (100, 40, 520, 680))
    return controller, window


class TestPointerOwnsClickThrough:
    """A transparent window cannot be the one that decides to stop being transparent.

    That was the drag bug: the compositor cleared ``WS_EX_TRANSPARENT`` from its own
    ``WM_MOUSEMOVE`` handler, and a window the hit test skips never receives one. The
    decision belongs to the shell's pointer poller, which is the only party that can
    see where the cursor is *before* it presses.
    """

    def test_the_poller_lets_her_be_clicked_where_she_is_drawn(
        self, stub: Any, monkeypatch: Any
    ) -> None:
        controller, window = alpha_pet(stub, monkeypatch, cursor=(260, 300))
        window.opaque = True
        assert controller._sync_to_cursor() is True
        assert window.through == [False]
        # Screen pixels in, window-local pixels out: (260, 300) - (100, 40).
        assert window.probed == [(160, 260)]

    def test_the_desktop_keeps_the_click_where_nothing_is_painted(
        self, stub: Any, monkeypatch: Any
    ) -> None:
        controller, window = alpha_pet(stub, monkeypatch, cursor=(260, 300))
        window.opaque = False
        controller._sync_to_cursor()
        assert window.through == [True]

    def test_a_press_in_progress_is_not_re_decided(self, stub: Any, monkeypatch: Any) -> None:
        """Mid-drag the compositor holds the capture; a second opinion would drop it."""
        controller, window = alpha_pet(stub, monkeypatch, cursor=(260, 300))
        window.pressing = True
        controller._sync_to_cursor()
        assert window.through == []
        assert window.probed == []

    def test_the_operator_dragging_her_freezes_the_decision_too(
        self, stub: Any, monkeypatch: Any
    ) -> None:
        controller, window = alpha_pet(stub, monkeypatch, cursor=(260, 300))
        controller.set_dragging(True)
        controller._sync_to_cursor()
        assert window.probed == []

    def test_head_tracking_is_measured_against_the_desktop_box(
        self, stub: Any, monkeypatch: Any
    ) -> None:
        """Half across and a quarter down, in that box's own units.

        ``AlphaWindow.rect`` is ``(x, y, width, height)`` and the pointer maths wants
        ``(left, top, right, bottom)``. Handing it the first made the width ``420-100``
        and the height ``640-40``, so the same cursor position came back as
        ``(0.313, -0.467)`` instead -- which is why she kept staring at one corner of the
        screen wherever the cursor went. The fake compositor had been returning the second
        spelling, which is why the tests never saw it.
        """
        controller, _window = alpha_pet(stub, monkeypatch, cursor=(310, 200))
        controller._sync_to_cursor()
        scripts = cast("_ScriptWindow", controller._window).scripts
        assert any("(0.000, -0.500)" in script for script in scripts), scripts

    def test_the_topmost_band_is_re_asserted_from_the_same_poll(
        self, stub: Any, monkeypatch: Any
    ) -> None:
        controller, window = alpha_pet(stub, monkeypatch, cursor=(260, 300))
        controller._sync_to_cursor()
        assert window.topmost == 1

    def test_a_failing_decision_does_not_end_the_thread(self, stub: Any, monkeypatch: Any) -> None:
        """A poller that dies quietly looks exactly like a pet that froze.

        Nothing on screen says "the pointer decisions stopped", so the loop has to keep
        going and say so once, loudly enough to find in the log.
        """
        controller, _window = alpha_pet(stub, monkeypatch, cursor=(260, 300))

        def explode() -> bool:
            raise RuntimeError("合成器不见了")

        monkeypatch.setattr(controller, "_sync_to_cursor", explode)
        for _ in range(3):
            controller._poll_once()
        assert controller._poll_failures == 3


class TestOnePointerSamplePerRound:
    """A round used to read the pointer twice, for the same answer.

    ``_sync_to_cursor`` called ``GetCursorPos`` for its own click-through decision and
    ``_push_pointer`` called it again to work out the head-tracking angle: two
    syscalls doing the work of one, twenty times a second, on the thread that also
    answers the page. The sample is handed down now, and this is the test that notices
    if somebody reads it again.
    """

    def test_a_decision_reads_the_pointer_once(self, stub: Any, monkeypatch: Any) -> None:
        controller, _window = alpha_pet(stub, monkeypatch, cursor=(260, 300))
        reads: list[int] = []

        def counting() -> tuple[int, int]:
            reads.append(1)
            return (260, 300)

        monkeypatch.setattr("jarvis.ui.pet._cursor_position", counting)
        assert controller._sync_to_cursor() is True
        assert len(reads) == 1

    def test_a_full_poll_reads_the_pointer_once(self, stub: Any, monkeypatch: Any) -> None:
        """``_poll_once`` is what the watcher thread actually runs, caption and all."""
        controller, _window = alpha_pet(stub, monkeypatch, cursor=(260, 300))
        reads: list[int] = []

        def counting() -> tuple[int, int]:
            reads.append(1)
            return (260, 300)

        monkeypatch.setattr("jarvis.ui.pet._cursor_position", counting)
        controller._poll_once()
        assert len(reads) == 1

    def test_a_press_in_progress_still_reads_it_once(self, stub: Any, monkeypatch: Any) -> None:
        """The early return mid-drag happens after the sample, not before it."""
        controller, window = alpha_pet(stub, monkeypatch, cursor=(260, 300))
        window.pressing = True
        reads: list[int] = []

        def counting() -> tuple[int, int]:
            reads.append(1)
            return (260, 300)

        monkeypatch.setattr("jarvis.ui.pet._cursor_position", counting)
        controller._sync_to_cursor()
        assert len(reads) == 1


def surface_window(blob: tuple[int, int, int, int] | None = None) -> AlphaWindow:
    """A compositor with one frame already painted, no Win32 involved.

    ``blob`` is ``(x, y, w, h)`` of opaque pixels in a 40x30 premultiplied BGRA frame.
    """
    window = AlphaWindow()
    width, height = 40, 30
    buffer = bytearray(width * height * 4)
    if blob is not None:
        x0, y0, w, h = blob
        for y in range(y0, min(y0 + h, height)):
            for x in range(x0, min(x0 + w, width)):
                offset = (y * width + x) * 4
                buffer[offset : offset + 4] = bytes([200, 220, 255, 255])
    window._surface = (bytes(buffer), width, height)
    return window


class TestBodyHitTest:
    """Her body is the drag target, decided by the pixels rather than by a rectangle.

    A wireframe is mostly nothing: a strict one-pixel test would be true only exactly on
    a line, so the scan looks around the cursor. The empty part of the box has to stay
    click-through or the figure is a grey brick again -- which is why a window with no
    frame yet answers ``False``.
    """

    def test_a_window_that_has_never_painted_does_not_take_the_click(self) -> None:
        assert AlphaWindow().opaque_at(10, 10) is False

    def test_a_pixel_of_her_is_grabbable(self) -> None:
        assert surface_window((10, 10, 6, 6)).opaque_at(12, 12) is True

    def test_the_scan_reaches_across_a_glowing_line(self) -> None:
        """Five pixels away is still "on her"; fifty is not."""
        window = surface_window((10, 10, 6, 6))
        assert window.opaque_at(19, 19) is True  # the corner, just inside the radius
        assert window.opaque_at(30, 25) is False

    def test_the_gap_between_her_arm_and_her_body_stays_click_through(self) -> None:
        assert surface_window((2, 2, 4, 20)).opaque_at(25, 12) is False

    def test_a_point_outside_the_frame_is_not_her(self) -> None:
        window = surface_window((0, 0, 40, 30))
        assert window.opaque_at(-3, 5) is False
        assert window.opaque_at(39, 40) is False


def _handle(value: Any) -> int:
    """A ctypes ``HWND``/``c_void_p`` or a bare int, as a signed 64-bit number.

    ``HWND_TOPMOST`` is ``-1`` wearing a pointer's clothes: passed as
    ``wintypes.HWND(-1)`` it arrives here as ``0xFFFFFFFFFFFFFFFF``, and a test that
    compared against ``-1`` literally would fail on the production code being correct.
    """
    raw = getattr(value, "value", value)
    number = int(raw or 0)
    return number - (1 << 64) if number >= 1 << 63 else number


class _RecordingWin32:
    """The three calls this class makes, with every argument written down."""

    def __init__(self) -> None:
        self.styles: list[tuple[int, int]] = []
        self.positions: list[tuple[int, int, tuple[int, int, int, int], int]] = []
        self.shown: list[int] = []

    def SetWindowLongW(self, hwnd: Any, index: int, value: int) -> int:  # noqa: N802
        self.styles.append((_handle(hwnd), int(value)))
        return 0

    def SetWindowPos(  # noqa: N802 - Win32 spells it this way
        self, hwnd: Any, after: Any, x: int, y: int, w: int, h: int, flags: int
    ) -> bool:
        self.positions.append((_handle(hwnd), _handle(after), (x, y, w, h), flags))
        return True

    def ShowWindow(self, hwnd: Any, command: int) -> int:  # noqa: N802
        self.shown.append(command)
        return 1


def composited(monkeypatch: Any, clock: list[float]) -> tuple[AlphaWindow, _RecordingWin32]:
    win32 = _RecordingWin32()
    monkeypatch.setattr("jarvis.ui.compositor._USER32", win32)
    monkeypatch.setattr("jarvis.ui.compositor.time.monotonic", lambda: clock[0])
    window = AlphaWindow()
    window._hwnd = 4321
    return window, win32


class TestTopmost:
    """Measured on this machine: the style bit is not how a window becomes topmost.

    ``SetWindowLongW(GWL_EXSTYLE, ex | WS_EX_TOPMOST)`` writes ``0x8080068`` and reads
    back ``0x8080060`` -- Windows drops the bit -- and the pet stayed below an
    ordinary window the operator clicked. Only ``SetWindowPos`` with ``HWND_TOPMOST``
    inserts her into the topmost band. These tests pin the call, because the style
    read-back looks fine either way.
    """

    def test_raise_topmost_asks_the_only_call_that_works(self, monkeypatch: Any) -> None:
        clock = [100.0]
        window, win32 = composited(monkeypatch, clock)
        window.raise_topmost()
        assert win32.positions == [(4321, -1, (0, 0, 0, 0), 1 | 2 | 0x10)]

    def test_the_style_write_does_not_claim_to_be_topmost(self, monkeypatch: Any) -> None:
        clock = [100.0]
        window, win32 = composited(monkeypatch, clock)
        window.set_click_through(False)
        assert win32.styles, "styles are written"
        _hwnd, value = win32.styles[0]
        assert value & 0x00000008 == 0, "WS_EX_TOPMOST is not a SetWindowLongW business"
        assert value & 0x00080000, "WS_EX_LAYERED is, and does stick"
        assert win32.positions, "and topmost goes through SetWindowPos"

    def test_click_through_still_flips_the_transparent_bit(self, monkeypatch: Any) -> None:
        clock = [100.0]
        window, win32 = composited(monkeypatch, clock)
        assert window.set_click_through(False) is True
        assert window.set_click_through(False) is False, "no change, no restatement"
        assert win32.styles[0][1] & 0x20 == 0
        assert window.set_click_through(True) is True
        assert win32.styles[1][1] & 0x20, "WS_EX_TRANSPARENT comes back"

    def test_re_asking_is_rate_limited(self, monkeypatch: Any) -> None:
        clock = [100.0]
        window, win32 = composited(monkeypatch, clock)
        window.keep_topmost()
        window.keep_topmost()
        assert len(win32.positions) == 1
        clock[0] = 100.0 + 3.0
        window.keep_topmost()
        assert len(win32.positions) == 2

    def test_coming_back_from_the_tray_puts_her_on_top_again(self, monkeypatch: Any) -> None:
        """Whatever was opened while she was away is now in front of an ordinary window."""
        clock = [100.0]
        window, win32 = composited(monkeypatch, clock)
        window.set_visible(True)
        assert win32.shown == [4], "SW_SHOWNOACTIVATE -- see the keyboard note below"
        assert win32.positions and win32.positions[-1][1] == -1

    def test_showing_her_never_activates_the_window(self, monkeypatch: Any) -> None:
        """An overlay that can be activated is an overlay that eats the keyboard.

        The packaged build did exactly this: ``ShowWindow(SW_SHOW)`` made the pet's own
        layered window the foreground window, and typing anywhere on the machine went
        into a window that ignores keys. ``WS_EX_NOACTIVATE`` cannot save a window that
        is *created* visible, so both halves are pinned here.
        """
        clock = [100.0]
        window, win32 = composited(monkeypatch, clock)
        window.set_visible(True)
        window.set_visible(False)
        assert win32.shown == [4, 0], "no SW_SHOW(5) anywhere"


def _png(surface: Any) -> str:
    buffer = io.BytesIO()
    surface.save(buffer, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode()


class TestCaption:
    """The words beside her have to be pixels, because the page's DOM stays off screen."""

    def paint(self) -> Any:
        from PIL import ImageDraw

        return ImageDraw.Draw(Image.new("RGBA", (8, 8)))

    def test_chinese_breaks_anywhere_but_a_number_does_not(self) -> None:
        from jarvis.ui.compositor import font_at

        font = font_at(16)
        if font is None:  # pragma: no cover - a Windows box without these three fonts
            pytest.skip("no Chinese font installed to measure against")
        lines = wrap_text(self.paint(), "当前CPU占用是45.0%，已经很接近满载了", font, 90, 4)
        assert lines, "it wraps"
        assert not any(line.endswith("45.") for line in lines), lines
        joined = "".join(lines).replace(" ", "")
        assert "45.0%" in joined, joined

    def test_a_long_answer_ends_with_an_ellipsis_not_a_half_line(self) -> None:
        from jarvis.ui.compositor import font_at

        font = font_at(14)
        if font is None:  # pragma: no cover
            pytest.skip("no Chinese font installed to measure against")
        lines = wrap_text(self.paint(), "字" * 300, font, 120, 4)
        assert len(lines) == 4
        assert lines[-1].endswith("…")

    def test_the_bubble_is_pixels_by_the_time_the_desktop_sees_them(self) -> None:
        """A bubble is its own picture now, not paint on her frame.

        Rendering it in-frame was what put words over her body, and widening her frame
        to make room would have halved her frame rate while she talked (the frame is
        re-premultiplied every time). So the bubble is a standalone PNG that gets its
        own window -- and the pixels have to actually be in it.
        """
        import base64
        import io

        built = build_bubble("我在，你说。")
        assert built is not None, "有中文字体却没有气泡，等于没有这句回答"
        url, width, height = built.url, built.width, built.height
        image = Image.open(io.BytesIO(base64.b64decode(url.split(",", 1)[1]))).convert("RGBA")
        assert (image.size[0], image.size[1]) == (width, height)
        assert any(
            cast("tuple[int, int, int, int]", image.getpixel((x, y)))[3] > 0
            for y in range(0, height, 4)
            for x in range(0, width, 4)
        ), "a caption that reaches nowhere is the failure this replaces"

    def test_the_tail_points_left_because_the_bubble_stands_on_her_right(self) -> None:
        """Shape, not decoration: the arrow has to leave from the side facing her.

        A bubble whose tail points away from the figure reads as a second window that
        happens to be nearby, which is exactly what this is not supposed to look like.
        """
        import base64
        import io

        built = build_bubble("我在，你说。")
        assert built is not None
        url, width, height = built.url, built.width, built.height
        image = Image.open(io.BytesIO(base64.b64decode(url.split(",", 1)[1]))).convert("RGBA")
        pixels = image.getchannel("A").load()
        assert pixels is not None

        def opaque_fraction(column: int) -> float:
            opaque = 0
            for y in range(height):
                if int(cast(Any, pixels[column, y])) > 0:
                    opaque += 1
            return opaque / height

        assert opaque_fraction(4) > 0, "箭头在她那一侧：左边几列必须有像素"
        assert opaque_fraction(4) < 0.6, "左边那几列是箭头，不是一整面墙"
        assert opaque_fraction(width // 2) > 0.6, "中间是气泡本体，应当基本铺满"

    def test_nothing_to_say_is_not_a_bubble(self) -> None:
        assert build_bubble("") is None
        assert build_bubble("   ") is None


AMBER = bubble_palette(0x3A2408, 0xFFB04A, 0xFFE0B0)
"""琥珀 · 暖阳 as ``theme.ts`` reports it -- the skin furthest from the default inks."""


def _pixels(url: str) -> Any:
    return Image.open(io.BytesIO(base64.b64decode(url.split(",", 1)[1]))).convert("RGBA")


def _body(url: str, width: int) -> tuple[int, int, int, int]:
    """The card's own background: one pixel inside the body, above the first line."""
    return cast("tuple[int, int, int, int]", _pixels(url).getpixel((width // 2, 2)))


class TestTheCardFollowsHerSkin:
    """她身上换了颜色，桌上那两张卡不能还是原来那身。

    卡片是这套界面里**唯一由 Python 画**的一块，所以换肤漏掉的就是它：一个琥珀色的人配
    一张青色卡，和 3D 层那次"只染了一半的半身像"是同一种失败。页面报的是它自己那三个墨色
    （配色表只有 ``theme.ts`` 那一份，抄进 Python 就是一张会过期的名单）。
    """

    def test_the_reported_inks_move_the_answer_bubbles_pixels(self) -> None:
        default = build_bubble("我在，你说。")
        amber = build_bubble("我在，你说。", palette=AMBER)
        assert default is not None and amber is not None
        assert _body(default.url, default.width) != _body(
            amber.url, amber.width
        ), "两套皮肤画出同一张卡，等于没跟"
        assert _body(amber.url, amber.width) == AMBER.fill

    def test_the_card_stays_readable_on_a_bright_skin(self) -> None:
        """底色压得更黑、字提得更亮 —— 直接拿皮肤的 fill 当卡底是读不了的。"""
        assert AMBER.fill[3] >= 200, "卡片要能盖住桌面，不能是半透的"
        ink, floor = sum(AMBER.ink[:3]), sum(AMBER.fill[:3])
        assert ink > floor * 4, f"字和底拉开不够：{ink} vs {floor}"
        assert all(channel <= 255 for channel in AMBER.ink[:3]), "提亮不许冲出 255"

    def test_the_thinking_card_is_painted_in_the_same_inks(self) -> None:
        frames = build_thinking_frames("dots", AMBER)
        assert len(frames) == THINKING_FRAMES
        assert _body(frames[0].url, frames[0].width) == AMBER.fill
        plain = build_thinking_frames("dots", DEFAULT_BUBBLE)
        assert _body(frames[0].url, frames[0].width) != _body(plain[0].url, plain[0].width)

    def test_swapping_back_to_a_worn_skin_reuses_the_pictures(self) -> None:
        """换肤要重画，来回换不该每次都重画 —— 八张小 PNG 是全部成本。"""
        first = build_thinking_frames("dots", AMBER)
        assert build_thinking_frames("dots", AMBER) is first
        other = build_thinking_frames("dots", DEFAULT_BUBBLE)
        assert other is not first
        assert build_thinking_frames("dots", AMBER) is first, "另一种皮肤不该把缓存清了"


class TestTheCardIsSmall:
    """操作者说"太大了"说了两次，所以把尺寸钉在这儿，而不是钉在注释里。

    钉的是**绝对像素**，不是"别超过那个常量"：把上限调回去的话，跟着常量走的断言会跟着
    一起放宽，那就等于什么都没钉。数字是本机实测（中文字体在位）——
    缩小前：长答 235x78、短句 95x38；缩小后：182x68、85x32。
    """

    def test_a_long_answer_is_capped_by_width_and_lines(self) -> None:
        built = build_bubble(LONG_ANSWER)
        assert built is not None
        assert built.width <= 190, f"卡片顶到上限就该停：{built.width}"
        assert built.height <= 70, f"三行字不该比这更高：{built.height}"

    def test_a_short_answer_hugs_its_own_text(self) -> None:
        built = build_bubble("现在几点了")
        assert built is not None
        assert built.width <= 100, f"一句话不该占满一张卡：{built.width}"

    def test_the_thinking_card_is_the_same_scale(self) -> None:
        """那张「思考中」和回答卡是一套东西，不许一个跟着缩一个不动。"""
        frames = build_thinking_frames("dots", DEFAULT_BUBBLE)
        assert frames
        assert (
            frames[0].width <= 90 and frames[0].height <= 40
        ), f"思考卡也在这把尺子上：{frames[0].width}x{frames[0].height}"


class TestTheIconsOnTheCard:
    """卡上那两枚图钉的是**位置**，不只是样子。

    窗口是点穿的，只有图标那一小块不点穿 —— 所以按钮矩形必须是画出来的那块。位置说错
    一格，用户按「停止」就会按下「再问一句」，那比没有按钮更糟。
    """

    def test_the_icons_come_back_with_rects_inside_the_card(self) -> None:
        built = build_bubble("我在，你说。", actions=(BUBBLE_STOP, BUBBLE_ADD))
        assert built is not None
        assert [button.name for button in built.buttons] == [BUBBLE_STOP, BUBBLE_ADD]
        for button in built.buttons:
            assert button.x >= 0 and button.x + button.size <= built.width
            assert button.y >= 0 and button.y + button.size <= built.height

    def test_the_icons_are_not_stacked_on_top_of_each_other(self) -> None:
        built = build_bubble("我在，你说。", actions=(BUBBLE_STOP, BUBBLE_ADD))
        assert built is not None
        first, second = built.buttons
        assert first.x + first.size <= second.x, "两枚挤在一起，按哪个都不确定"

    def test_the_icons_come_out_of_the_texts_width_not_the_cards(self) -> None:
        """上限是和操作者约好的那条线：加了图标也不许把卡撑过去。"""
        plain = build_bubble(LONG_ANSWER)
        with_icons = build_bubble(LONG_ANSWER, actions=(BUBBLE_STOP, BUBBLE_ADD))
        assert plain is not None and with_icons is not None
        assert with_icons.width <= max(plain.width, BUBBLE_MAX_WIDTH + 8)

    def test_a_card_without_icons_has_nothing_to_press(self) -> None:
        built = build_bubble("我在，你说。")
        assert built is not None
        assert built.buttons == ()

    def test_the_thinking_card_carries_the_same_icons_on_every_frame(self) -> None:
        """动效在换图，按钮不许跟着漂：八张里每一张的位置必须一模一样。"""
        frames = build_thinking_frames("ring", DEFAULT_BUBBLE, (BUBBLE_STOP, BUBBLE_ADD))
        assert frames
        assert all(frame.buttons == frames[0].buttons for frame in frames)
        assert all(
            (frame.width, frame.height) == (frames[0].width, frames[0].height) for frame in frames
        )

    def test_a_release_onto_an_icon_is_a_press_of_that_icon(self) -> None:
        """窗口这一头真正会走的那条路：按下 → 抬起 → 报出按的是哪个。

        上面那些测试用假体直接调了回调，所以这里补上 ``_release`` 本身 —— 图标矩形和
        回调之间就差这一格，而它错了的话用户看到的是"按停止结果她问了下一个问题"。
        """
        hits: list[str] = []
        window = surface_window()
        window._on_tap = hits.append
        window.set_hot_spots((BubbleButton(name=BUBBLE_STOP, x=20, y=4, size=16),))
        window._press = (25, 9)
        window._moved = False
        window._release(None)
        assert hits == [BUBBLE_STOP]

    def test_a_release_on_the_words_presses_nothing(self) -> None:
        hits: list[str] = []
        window = surface_window()
        window._on_tap = hits.append
        window.set_hot_spots((BubbleButton(name=BUBBLE_STOP, x=20, y=4, size=16),))
        window._press = (2, 2)
        window._moved = False
        window._release(None)
        assert hits == []

    def test_a_press_that_slid_is_a_drag_not_a_button(self) -> None:
        """拖过几个像素就算拖动：不然她说话时你想挪一下鼠标，会顺手把这一轮掐了。"""
        hits: list[str] = []
        window = surface_window()
        window._on_tap = hits.append
        window.set_hot_spots((BubbleButton(name=BUBBLE_STOP, x=20, y=4, size=16),))
        window._press = (25, 9)
        window._moved = True
        window._release(None)
        assert hits == []
