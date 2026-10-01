"""The desktop pet: a figure standing on the screen rather than a window on the desk.

What this window is
-------------------
A second pywebview window over the same bundle, opened at ``?mode=pet``: no panels,
no telemetry, no title bar -- just the cyber figure, the wormhole it arrives
through, and a caption. It is frameless, transparent, always-on-top, and it never
takes focus, so it cannot steal the keyboard from whatever the operator is doing.

Why it is created lazily
------------------------
A second WebView2 means a second renderer process, a second WebGL context, and a
second animation loop. The measured idle cost of the *first* window's tree is around
a tenth of a core, and the whole point of the assistant is that it sits in the
background all day. So the window does not exist until the operator asks for the
pet, and after that it is hidden rather than destroyed -- hiding costs nothing, and
re-creating it would put a multi-second load between "say the wake word" and
"something appears on screen", which is the moment this feature is judged on.

Why it is a panel, and why the panel is designed
------------------------------------------------
``transparent=True`` is passed and does not arrive on this host: the pixels that end
up on the desktop belong to the WebView2 child window, and a WinForms top-level form
has no per-pixel alpha to give them. Four attempts were measured and each is written
down in :meth:`PetController._paint_backdrop`, because "make it a hole in the
desktop" is the obvious next idea and it is the one that has already been ruled out.
So the figure stands in a field -- a portal-shaped gradient with a rim, in the HUD's
own palette -- rather than in an accidental grey rectangle.

Why clicks pass through it
--------------------------
An object drawn over the desktop that swallows clicks is a toy for five minutes and
an obstacle forever. ``WS_EX_TRANSPARENT`` on the top-level window makes the hit test
continue to the windows underneath, so the desktop, the taskbar and the application
behind the figure all keep working. The one exception is the grip -- a small chip the
operator can press to move the pet -- and the cursor is watched so that only that
patch is ever clickable. Watching the pointer is 8 calls a second of
``GetCursorPos``; the alternative (a global mouse hook) would need a message loop of
its own and would see every click the operator makes, which is a privacy cost this
feature does not earn.
"""

from __future__ import annotations

import ctypes
import logging
import threading
import time
from collections.abc import Callable
from typing import Any

from jarvis.ui.compositor import AlphaWindow

logger = logging.getLogger("jarvis.ui.pet")

PET_WIDTH = 420
PET_HEIGHT = 640
"""Logical size of the pet window: a standing figure needs a tall box, and a wide
one would cover more desktop than the figure itself."""

PET_TITLE = "小夜 · 桌面宠物"

GWL_EXSTYLE = -20
WS_EX_TRANSPARENT = 0x00000020
SWP_NOSIZE = 0x0001
SWP_SHOWWINDOW = 0x0040
BACKDROP_INK = (4, 7, 13)
"""The colour the pet's form paints behind the page: the HUD's own deep background,
so the panel reads as part of the product. See :meth:`PetController._paint_backdrop`
for why this window is not transparent."""

CURSOR_POLL_SECONDS = 0.12
"""How often the pointer is sampled to decide whether the window is clickable."""

GRIP_FALLBACK = (0.58, 0.06, 0.34, 0.07)
"""Where the drag handle is until the page says otherwise: normalised
``(x, y, width, height)`` of the viewport, top-right of the figure's head."""

EMERGE_SCRIPT = "window.__jarvisPet && window.__jarvisPet('emerge');"
"""Play the wormhole arrival. The page owns what that looks like; this module only
says that it happened now."""

WAKE_SCRIPT = "window.__jarvisPet && window.__jarvisPet('wake');"
SLEEP_SCRIPT = "window.__jarvisPet && window.__jarvisPet('sleep');"
ALPHA_SCRIPT = "window.__jarvisPet && window.__jarvisPet('alpha');"
"""Tell the page a real-alpha window is listening for its frames.

That is the switch for the whole look: the page stops painting its portal panel (a
panel composited into an alpha window becomes a rectangle on the desktop again) and
starts shipping PNGs. Nothing else about the figure changes, which is the point --
the same Three.js scene, the same mouth, the same skins.
"""

POINTER_SCRIPT = "window.__jarvisPetPointer && window.__jarvisPetPointer({x:.3f}, {y:.3f});"
"""Where the OS pointer is, as -1..1 of the pet's own box.

The page cannot see a cursor it is not under, and in alpha mode it is not under
anything -- it is parked off screen doing nothing but rendering. Head tracking is
the one thing that has to cross the bridge in this direction.
"""

OFF_SCREEN = -32000
"""Where the renderer window goes once its pixels have a better home.

Hidden rather than off-screen would be natural, and it is wrong: a hidden WebView2
is an occluded one, and Chromium stops producing frames for it. Off screen it is
still "visible" to the compositor, so the figure keeps breathing.
"""
"""Start and stop the drawing loop.

A hidden WebView2 window still believes it is visible -- ``document.hidden`` stays
false -- so the pet would keep shading a 3D scene at 20 Hz for an audience of
nobody, on a window that is not on screen. The shell knows when it hid the window,
so it says so."""


def pet_origin(screen_width: int, screen_height: int, width: int, height: int) -> tuple[int, int]:
    """Centre the window on the screen, as asked for: 屏幕中间.

    Clamped so a small or negative origin cannot put the figure off the edge of a
    laptop panel, where the grip would be unreachable and the pet invisible.
    """
    x = max(0, (screen_width - width) // 2)
    y = max(0, (screen_height - height) // 2)
    return x, y


def _ex_style(hwnd: int) -> int:
    return int(ctypes.windll.user32.GetWindowLongW(hwnd, GWL_EXSTYLE))


def _set_ex_style(hwnd: int, style: int) -> None:
    ctypes.windll.user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style)


def _cursor_position() -> tuple[int, int] | None:
    """The pointer in physical screen pixels, or ``None`` if the OS would not say."""

    class Point(ctypes.Structure):
        _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

    point = Point()
    if not ctypes.windll.user32.GetCursorPos(ctypes.byref(point)):
        return None
    return int(point.x), int(point.y)


def _window_rect(hwnd: int) -> tuple[int, int, int, int] | None:
    """``(left, top, right, bottom)`` in physical screen pixels."""

    class Rect(ctypes.Structure):
        _fields_ = [
            ("left", ctypes.c_long),
            ("top", ctypes.c_long),
            ("right", ctypes.c_long),
            ("bottom", ctypes.c_long),
        ]

    rect = Rect()
    if not ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return None
    return rect.left, rect.top, rect.right, rect.bottom


def handle_of(window: Any) -> int:
    """The WinForms handle of a pywebview window, or 0.

    ``window.native`` is the ``BrowserForm`` and ``Handle`` is a .NET ``IntPtr``;
    ``ToInt32()`` is the spelling the rest of pywebview uses for its own
    ``SetWindowLong`` calls, so it is used here rather than a second convention.
    """
    native = getattr(window, "native", None)
    handle = getattr(native, "Handle", None)
    to_int32 = getattr(handle, "ToInt32", None)
    if callable(to_int32):
        try:
            return int(to_int32())
        except Exception:  # pragma: no cover - a handle that will not unbox
            return 0
    return 0


class PetController:
    """One transparent window, its click-through, and the grip that beats it.

    The shell hands in the URL and the JS bridge; everything else is decided here.
    """

    def __init__(
        self,
        *,
        base_url: str,
        bridge: Any,
        on_active_change: Callable[[bool], None] | None = None,
        width: int = PET_WIDTH,
        height: int = PET_HEIGHT,
    ) -> None:
        self._base_url = base_url
        self._bridge = bridge
        self._on_active_change = on_active_change
        self._width = width
        self._height = height
        self._window: Any | None = None
        self._shown = False
        self._grip = GRIP_FALLBACK
        self._dragging = False
        self._click_through = True
        self._stop = threading.Event()
        self._watcher: threading.Thread | None = None
        self._hwnd = 0
        self._compositor: AlphaWindow | None = None
        self._pointer_sent = (0.0, 0.0)
        self._grab_reported: tuple[float, float, float, float] | None = None
        self._frames_seen = 0

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    @property
    def window(self) -> Any | None:
        return self._window

    @property
    def audio_window(self) -> Any | None:
        """The window to feed speech PCM to while the pet is the one on screen."""
        return self._window if self._shown else None

    @property
    def shown(self) -> bool:
        return self._shown

    @property
    def created(self) -> bool:
        return self._window is not None

    # ------------------------------------------------------------------
    # Showing and hiding
    # ------------------------------------------------------------------

    def show(self, *, emerge: bool = False) -> bool:
        """Create the window if this is the first time, then put it on screen."""
        if self._window is None and not self._create():
            return False
        window = self._window
        if window is None:  # pragma: no cover - _create reports the failure
            return False
        try:
            window.show()
        except Exception:  # pragma: no cover - a GUI that will not show
            logger.exception("the pet window would not show")
            return False
        self._shown = True
        self._hwnd = handle_of(window)
        self._apply_click_through(self._click_through)
        self._start_watcher()
        self._start_compositor()
        self._command(WAKE_SCRIPT)
        if emerge:
            self.emerge()
        self._notify(True)
        logger.info("pet window shown at %sx%s", self._width, self._height)
        return True

    def hide(self) -> bool:
        """Take it off screen. The page keeps running, so the next show is instant."""
        window = self._window
        if window is None:
            self._shown = False
            self._notify(False)
            return True
        try:
            window.hide()
        except Exception:  # pragma: no cover - a GUI that will not hide
            logger.exception("the pet window would not hide")
            return False
        self._command(SLEEP_SCRIPT)
        compositor = self._compositor
        if compositor is not None:
            compositor.set_visible(False)
        self._shown = False
        self._notify(False)
        return True

    def toggle(self) -> bool:
        """Returns whether the pet is on screen afterwards, not whether it worked."""
        if self._shown:
            self.hide()
        else:
            self.show()
        return self._shown

    def summon(self) -> bool:
        """The wake word arrived: put the figure on screen and play the arrival."""
        return self.show(emerge=True)

    def emerge(self) -> None:
        self._command(EMERGE_SCRIPT)

    def _on_page_loaded(self) -> None:
        """Re-play the cues the page was not ready for when they were sent."""
        compositor = self._compositor
        if compositor is not None and compositor.alive:
            self._command(ALPHA_SCRIPT)
        if self._shown:
            self._command(WAKE_SCRIPT)

    def _command(self, script: str) -> None:
        """Send one cue to the page. A missed animation is not a fault worth raising for."""
        window = self._window
        if window is None:
            return
        try:
            window.evaluate_js(script)
        except Exception:  # pragma: no cover - the page may still be loading
            logger.debug("a pet cue did not reach the page", exc_info=True)

    def close(self) -> None:
        """Destroy the window for good. Called on the way out of the app."""
        self._stop.set()
        watcher = self._watcher
        if watcher is not None:
            watcher.join(timeout=1.0)
            self._watcher = None
        compositor = self._compositor
        self._compositor = None
        if compositor is not None:
            compositor.close()
        window = self._window
        self._window = None
        self._shown = False
        self._hwnd = 0
        if window is None:
            return
        try:
            window.destroy()
        except Exception:  # pragma: no cover - it is going away either way
            logger.debug("the pet window was already gone", exc_info=True)

    def _create(self) -> bool:
        import webview

        screen = _primary_screen()
        x, y = pet_origin(screen[0], screen[1], self._width, self._height)
        try:
            window: Any = webview.create_window(
                title=PET_TITLE,
                url=f"{self._base_url.rstrip('/')}/?mode=pet",
                js_api=self._bridge,
                width=self._width,
                height=self._height,
                x=x,
                y=y,
                frameless=True,
                transparent=True,
                on_top=True,
                focus=False,
                resizable=False,
                # pywebview's own drag: it is only reachable while the cursor is on
                # the grip, because everywhere else the window does not see clicks.
                easy_drag=True,
            )
        except Exception:  # pragma: no cover - a shell that will not make a window
            logger.exception("could not create the pet window")
            return False
        self._window = window
        # The cues only land once the page's own script has installed its
        # command handler, and ``show`` can run before that. Re-sending on
        # every load is what makes "the shell asked for alpha frames" true
        # even when the shell asked first.
        window.events.loaded += self._on_page_loaded
        if not self._paint_backdrop(window):
            logger.info("the pet panel could not be coloured; using the shell default")
        return True

    # ------------------------------------------------------------------
    # Click-through
    # ------------------------------------------------------------------

    def report_grip(self, rect: object) -> None:
        """Where the page's drag handle is, as ``(x, y, w, h)`` fractions of itself."""
        values = _four_floats(rect)
        if values is None:
            logger.warning("ignored a malformed pet grip rect: %r", rect)
            return
        self._grip = values
        self._grab_reported = values
        window = self._compositor
        if window is not None:
            # In alpha mode the handle is not in the window that receives the click,
            # so the rectangle the page drew is the only thing the hit test has.
            window.set_grab(values)

    def set_dragging(self, dragging: bool) -> None:
        """Freeze click-through decisions while the operator is moving the pet.

        Without this the pointer slides off the grip mid-drag, the watcher makes the
        window transparent, the page loses its implicit pointer capture, and the
        figure stops following the mouse half way across the screen.
        """
        self._dragging = bool(dragging)

    def _apply_click_through(self, on: bool) -> None:
        hwnd = self._hwnd
        if not hwnd:
            return
        try:
            style = _ex_style(hwnd)
            want = style | WS_EX_TRANSPARENT if on else style & ~WS_EX_TRANSPARENT
            if want != style:
                _set_ex_style(hwnd, want)
            self._click_through = on
        except Exception:  # pragma: no cover - a window that is already gone
            logger.debug("click-through could not be changed", exc_info=True)

    def _start_compositor(self) -> bool:
        """Put an alpha window on the desktop and let the page draw into it.

        Returns whether the figure is now the desktop's own pixels rather than a
        panel. When it fails -- no Win32, a class that will not register -- the pet
        stays exactly as it was, panel and all, because losing the figure entirely
        would be a worse answer to "can it be transparent" than a rectangle.
        """
        if self._compositor is not None and self._compositor.alive:
            self._compositor.set_visible(True)
            return True
        rect = _window_rect(self._hwnd or handle_of(self._window))
        if rect is None:
            return False
        left, top, right, bottom = rect
        window = AlphaWindow(on_tap=self.hide)
        if not window.start(x=left, y=top, width=right - left, height=bottom - top):
            return False
        self._compositor = window
        self._park_renderer_offscreen()
        self._command(ALPHA_SCRIPT)
        if self._grab_reported is not None:
            window.set_grab(self._grab_reported)
        logger.info("pet composited at %sx%s (no panel)", right - left, bottom - top)
        return True

    def _park_renderer_offscreen(self) -> None:
        """Move the WebView2 window away, leaving it visible so it keeps rendering."""
        hwnd = self._hwnd or handle_of(self._window)
        if not hwnd:
            return
        self._hwnd = hwnd
        try:
            user32 = ctypes.windll.user32
            # 0x0001 | 0x0040: keep the size, no activation. Top-most is cleared here
            # on purpose -- a window at -32000 that insists on being above everything
            # is a window that can be dragged onto a second monitor by accident.
            user32.SetWindowPos(
                ctypes.c_void_p(hwnd),
                0,
                OFF_SCREEN,
                OFF_SCREEN,
                0,
                0,
                SWP_NOSIZE | SWP_SHOWWINDOW,
            )
        except Exception:  # pragma: no cover - a shell that will not move a window
            logger.debug("the pet renderer would not move off screen", exc_info=True)

    def present_frame(self, payload: object) -> bool:
        """One PNG frame from the page. ``False`` when nothing is compositing it."""
        window = self._compositor
        if window is None or not self._shown or not isinstance(payload, str) or not payload:
            return False
        if not self._frames_seen:
            logger.info(
                "first pet frame arrived from the page (%s bytes of data URL)", len(payload)
            )
        self._frames_seen += 1
        return window.present(payload)

    def _push_pointer(self, rect: tuple[int, int, int, int]) -> None:
        """Tell the page where the cursor is, so she can look at it.

        Only sent when it moved by more than a pixel or two of her own width: the
        bridge call is cheap but not free, and an unchanged pointer is unchanged.
        """
        cursor = _cursor_position()
        if cursor is None:
            return
        left, top, right, bottom = rect
        width = max(1, right - left)
        height = max(1, bottom - top)
        x = max(-1.0, min(1.0, ((cursor[0] - left) / width) * 2.0 - 1.0))
        y = max(-1.0, min(1.0, ((cursor[1] - top) / height) * 2.0 - 1.0))
        if abs(x - self._pointer_sent[0]) < 0.02 and abs(y - self._pointer_sent[1]) < 0.02:
            return
        self._pointer_sent = (x, y)
        self._command(POINTER_SCRIPT.format(x=x, y=y))

    def _start_watcher(self) -> None:
        if self._watcher is not None:
            return
        self._stop.clear()
        self._watcher = threading.Thread(
            target=self._watch_cursor, name="jarvis-pet-cursor", daemon=True
        )
        self._watcher.start()

    def _watch_cursor(self) -> None:
        """Let the grip be clickable and nothing else be."""
        while not self._stop.is_set():
            time.sleep(CURSOR_POLL_SECONDS)
            if self._dragging or not self._shown:
                continue
            self._sync_to_cursor()

    def _sync_to_cursor(self) -> bool:
        """One decision. Returns whether it could be made at all."""
        compositor = self._compositor
        if compositor is not None and compositor.alive:
            # The renderer window is off screen and takes no input; the compositor
            # owns clicks, hit-testing and the drag. What the pointer is still worth
            # is the only thing that makes a standing figure look alive.
            self._push_pointer(compositor.rect)
            return True
        hwnd = self._hwnd or handle_of(self._window)
        if not hwnd:
            return False
        self._hwnd = hwnd
        cursor = _cursor_position()
        rect = _window_rect(hwnd)
        if cursor is None or rect is None:
            return False
        on_grip = _inside_grip(cursor, rect, self._grip, scale=_scale_of(rect, self._width))
        want_through = not on_grip
        if want_through != self._click_through:
            self._apply_click_through(want_through)
        return True

    def _notify(self, shown: bool) -> None:
        handler = self._on_active_change
        if handler is not None:
            try:
                handler(shown)
            except Exception:  # pragma: no cover - the shell's bookkeeping, not its job
                logger.debug("pet visibility hook failed", exc_info=True)

    def _paint_backdrop(self, window: Any) -> bool:
        """Give the form the same deep background the page paints its portal with.

        ``transparent=True`` is asked for and does not arrive. Measured on this
        machine, in this order: the form paints #EFF0F0 (WinForms' own control
        colour) through the page's transparent pixels; setting the form to black
        produces #202020 on screen; and a colour key that matches that value exactly
        -- read back off the desktop, not guessed -- removes nothing, because the
        pixels belong to the WebView2 *child* window, and ``WS_EX_LAYERED`` is a
        top-level style. The veil is the browser compositor, not the form.

        So the pet is a panel, and the honest move is to make it a designed one: the
        page paints a portal-shaped backdrop, and the form behind it is given the
        same colour so no second rectangle appears at the edges.
        """
        native = getattr(window, "native", None)
        if native is None:
            return False
        try:
            from System.Drawing import Color  # pythonnet, loaded by pywebview's WinForms side

            native.BackColor = Color.FromArgb(*BACKDROP_INK)
        except Exception:  # pragma: no cover - a CLR type that is not reachable
            logger.debug("the pet form could not be recoloured", exc_info=True)
            return False
        return True


def _scale_of(rect: tuple[int, int, int, int], logical_width: int) -> float:
    """Device pixels per CSS pixel, read off the window itself.

    The grip is reported in CSS pixels and the pointer lives in device pixels; on a
    150%-scaled panel guessing that ratio would put the clickable patch in the wrong
    place, so it is measured from the window's own width.
    """
    width = rect[2] - rect[0]
    if logical_width <= 0 or width <= 0:
        return 1.0
    return width / logical_width


def _inside_grip(
    cursor: tuple[int, int],
    rect: tuple[int, int, int, int],
    grip: tuple[float, float, float, float],
    *,
    scale: float = 1.0,
) -> bool:
    """Is the pointer over the handle? All fractions, so the maths is one line."""
    left, top, right, bottom = rect
    width = max(1, right - left)
    height = max(1, bottom - top)
    x, y, w, h = grip
    gx0 = left + x * width
    gx1 = gx0 + w * width
    gy0 = top + y * height
    gy1 = gy0 + h * height
    pad = 6 * scale
    return gx0 - pad <= cursor[0] <= gx1 + pad and gy0 - pad <= cursor[1] <= gy1 + pad


def _number(value: object) -> float | None:
    """A float out of whatever JSON arrived, with bools refused."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _four_floats(rect: object) -> tuple[float, float, float, float] | None:
    """A normalised grip rectangle, or ``None`` if this is not one."""
    if isinstance(rect, dict):
        raw: tuple[object, object, object, object] = (
            rect.get("x"),
            rect.get("y"),
            rect.get("width", rect.get("w")),
            rect.get("height", rect.get("h")),
        )
    elif isinstance(rect, (list, tuple)) and len(rect) == 4:
        raw = (rect[0], rect[1], rect[2], rect[3])
    else:
        return None
    numbers = [_number(item) for item in raw]
    if any(item is None for item in numbers):
        return None
    x, y, width, height = (float(item or 0.0) for item in numbers)
    if not (0.0 <= x <= 1.0 and 0.0 <= y <= 1.0 and 0.0 < width <= 1.0 and 0.0 < height <= 1.0):
        return None
    return x, y, width, height


def _primary_screen() -> tuple[int, int]:
    """The screen the figure should be centred on: the one with the taskbar."""
    try:
        import webview

        screens = webview.screens()
        # ``Screen`` carries no primary flag, and the first entry is the one
        # ``create_window`` places a windowless window on, so centring on it puts the
        # figure where the operator is looking.
        if screens:
            return int(screens[0].width), int(screens[0].height)
    except Exception:  # pragma: no cover - no screens without a desktop
        logger.debug("could not read the screen size for the pet window", exc_info=True)
    return 1920, 1080


__all__ = [
    "GRIP_FALLBACK",
    "PET_HEIGHT",
    "PET_TITLE",
    "PET_WIDTH",
    "PetController",
    "handle_of",
    "pet_origin",
]
