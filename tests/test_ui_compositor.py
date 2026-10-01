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

from jarvis.ui.compositor import AlphaWindow, decode_png, draw_handle, in_rect, premultiply
from jarvis.ui.pet import PetController

RECT = (0.58, 0.06, 0.34, 0.07)
"""The shipped handle: top-right of a 420x640 window, as fractions of it."""


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
    """Stands in for ``AlphaWindow`` so the controller's decisions can be watched."""

    instances: ClassVar[list[_StubCompositor]] = []

    def __init__(self, *, on_tap: Any = None, **_kwargs: Any) -> None:
        self.on_tap = on_tap
        self.started = False
        self.grabs: list[Any] = []
        self.frames: list[str] = []
        self.visible = False
        _StubCompositor.instances.append(self)

    @property
    def alive(self) -> bool:
        return self.started

    @property
    def rect(self) -> tuple[int, int, int, int]:
        return (100, 40, 520, 680)

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
