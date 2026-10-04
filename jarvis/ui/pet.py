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
background all day. So the window does not exist until the operator asks for the pet
(:meth:`PetController.toggle`), and after that it is hidden rather than destroyed --
hiding costs nothing, and re-creating it costs 1.64 seconds measured on this machine.

That 1.64 s is also why there is no pre-build. Keeping the window alive behind the tray
was tried and measured: a second WebView2 environment costs **1.1 GB of RSS** and does
not go idle just because nobody can see it. Buying back one and a half seconds once per
launch with a gigabyte the operator never asked to spend is the wrong trade, so the wake
word pays 1.64 s the first time and nothing after that.

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
behind the figure all keep working.

The exception is *her*: wherever the composited frame has pixels, the pointer is on
the pet, and everywhere the frame is transparent the desktop keeps the click. That is
decided by this module's poller, sampling ``GetCursorPos`` 20 times a second and
asking the compositor whether those pixels are opaque -- not by the compositor's own
mouse messages, which a transparent window cannot receive (the reason dragging was
dead for a whole release: the code that cleared the transparency was waiting on a
message that the transparency itself prevented). Watching the pointer is the cheaper
and the only correct direction; a global mouse hook would need a message loop of its
own and would see every click the operator makes, which is a privacy cost this feature
does not earn.

The same poller re-asserts the topmost band every couple of seconds, because
``WS_EX_TOPMOST`` cannot be set with ``SetWindowLongW`` -- measured, see
:mod:`jarvis.ui.compositor`.
"""

from __future__ import annotations

import ctypes
import json
import logging
import threading
import time
from collections.abc import Callable, Sequence
from typing import Any

from jarvis.ui.compositor import (
    DEFAULT_BUBBLE,
    GRAB_TARGET,
    THINKING_FPS,
    AlphaWindow,
    Bubble,
    BubblePalette,
    build_bubble,
    build_thinking_frames,
)

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
SWP_NOACTIVATE = 0x0010
PARK_FLAGS = SWP_NOSIZE | SWP_SHOWWINDOW | SWP_NOACTIVATE
"""How the renderer window is moved off screen: shown (so it keeps painting), never
activated (so it never holds the keyboard)."""
BACKDROP_INK = (4, 7, 13)
"""The colour the pet's form paints behind the page: the HUD's own deep background,
so the panel reads as part of the product. See :meth:`PetController._paint_backdrop`
for why this window is not transparent."""

CURSOR_POLL_SECONDS = 0.05
"""How often the pointer is sampled to decide whether the window is clickable.

Twenty times a second, up from eight. The old rate came from counting
``GetCursorPos`` calls as if they were expensive (they are a system call each, and
the work in this loop is the pixel lookup, not the sample), and at 8 Hz a drag
decided by this thread visibly lagged the cursor.
"""

GRIP_FALLBACK = (0.58, 0.06, 0.34, 0.07)

CAPTION_HELD_SECONDS = 20.0
"""How long an answer stays beside her before it goes away on its own.

A figure that keeps repeating what she said an hour ago is not a companion, it is a
bug that talks. A new question clears it immediately (see :meth:`PetController.set_caption`)."""

CAPTION_GAP = 14
"""Space between her box and the bubble, in pixels. Her tail needs somewhere to point."""

CAPTION_TOP_RATIO = 0.12
"""Where the bubble's top edge sits in her height: beside her head, not her feet."""

THINKING_LOADER_DEFAULT = "dots"
"""The animation the 「思考中」 card cycles, until the operator picks another.

It is a setting and not a constant because the same wait is shown in two places -- this
card and the chat bubble -- and the one thing they must not disagree about is what
"she is thinking" looks like.
"""
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


def _foreground_window() -> int:
    """The window that currently owns the keyboard, or 0."""
    try:
        return int(ctypes.windll.user32.GetForegroundWindow() or 0)
    except Exception:  # pragma: no cover - a shell that will not answer
        return 0


def _restore_foreground(hwnd: int, ours: tuple[int, ...]) -> bool:
    """Give the keyboard back to whoever had it before we put a window on screen.

    Returning ``True`` when it did something. Windows lets *this* process hand the
    foreground away -- it holds it at this moment -- so this is the one place where a
    cross-process ``SetForegroundWindow`` is allowed rather than silently refused.
    Never restore onto one of our own windows: the renderer window is parked at
    -32000, and a foreground window nobody can see is exactly the bug this fixes.
    """
    if not hwnd or hwnd in ours:
        return False
    try:
        user32 = ctypes.windll.user32
        if not user32.IsWindow(ctypes.c_void_p(hwnd)):
            return False
        user32.SetForegroundWindow(ctypes.c_void_p(hwnd))
    except Exception:  # pragma: no cover - a shell that will not move focus
        logger.debug("the keyboard could not be handed back", exc_info=True)
        return False
    return True


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
        on_window_change: Callable[[Any | None], None] | None = None,
        on_action: Callable[[str], object] | None = None,
        width: int = PET_WIDTH,
        height: int = PET_HEIGHT,
    ) -> None:
        self._base_url = base_url
        self._bridge = bridge
        self._on_active_change = on_active_change
        self._on_window_change = on_window_change
        self._on_action = on_action
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
        self._caption_window: AlphaWindow | None = None
        self._caption = ""
        self._caption_until = 0.0
        self._bubble_palette: BubblePalette = DEFAULT_BUBBLE
        self._bubble_actions: tuple[str, ...] = ()
        self._thinking = False
        self._thinking_sources: set[str] = set()
        self._thinking_window: AlphaWindow | None = None
        self._thinking_kind = THINKING_LOADER_DEFAULT
        self._thinking_frames: list[Bubble] | None = None
        self._thinking_index = 0
        self._thinking_next_at = 0.0
        self._thinking_refused = False
        self._thinking_painted = False
        self._thinking_visible = False
        self._pointer_sent = (0.0, 0.0)
        self._grab_reported: tuple[float, float, float, float] | None = None
        self._frames_seen = 0
        self._poll_failures = 0

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

    @property
    def frames_seen(self) -> int:
        """How many frames the page has shipped. A number, not a log line to grep.

        It is the only external evidence of what the figure costs: the frame rate is
        the CPU, and "is she animating at all" is otherwise indistinguishable from
        "she is animating too slowly to see".
        """
        return self._frames_seen

    # ------------------------------------------------------------------
    # Window bookkeeping
    # ------------------------------------------------------------------

    def observe_window(self, callback: Callable[[Any | None], None] | None) -> None:
        """Register the one thing the shell must know: which window is on screen.

        The shell pushes voice state into a list of windows, and she has to be in it
        or her listening/thinking pose never moves. That used to be handled at the
        single call site that opened her on start-up -- which meant every *other*
        way of opening her (the tray item, the button on the bar) produced a window
        nobody pushed to. Registering here instead makes it a property of showing
        her rather than a step one code path remembered to take.

        ``None`` is delivered on hide so the shell can drop a window it should stop
        feeding.
        """
        self._on_window_change = callback

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
        # Whoever was typing before she appeared. Shown windows activate themselves --
        # pywebview's ``show()`` does it, and the renderer window we then park at
        # -32000 ends up holding the keyboard, which is a desktop pet that breaks
        # typing until the operator clicks something else. Restored below.
        previous_foreground = _foreground_window()
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
        if _restore_foreground(previous_foreground, (self._hwnd, handle_of(window))):
            logger.debug("the keyboard went back to the window that had it")
        self._command(WAKE_SCRIPT)
        if emerge:
            self.emerge()
        # A bubble that was live when she was hidden comes back with her: the caption
        # is about what she said, and hiding her is not a statement about that.
        self._apply_caption()
        self._notify(True)
        self._notify_window(window)
        logger.info("pet window shown at %sx%s", self._width, self._height)
        return True

    def hide(self) -> bool:
        """Take it off screen. The page keeps running, so the next show is instant."""
        window = self._window
        if window is None:
            self._shown = False
            self._hide_caption()
            self._notify(False)
            self._notify_window(None)
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
        self._hide_caption()
        self._shown = False
        self._notify(False)
        self._notify_window(None)
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
        # Asleep unless somebody is looking: a hidden page still believes it is
        # visible, and would shade a 3D scene for an audience of nobody.
        self._command(WAKE_SCRIPT if self._shown else SLEEP_SCRIPT)

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
        caption = self._caption_window
        self._caption_window = None
        if caption is not None:
            caption.close()
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
        window = AlphaWindow(on_tap=self.on_figure_tap)
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
                PARK_FLAGS,
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

    def apply_skin(self, skin_id: str) -> bool:
        """Re-ink the running page in another palette. ``False`` when nothing is up.

        The pet is a **separate page**: the dashboard applying a skin to itself leaves
        this window's figure in the old colours, which reads as a bug rather than a
        theme. The id is quoted with ``json.dumps`` and the page validates it again, so
        a value that somehow got this far cannot turn into script.
        """
        if self._window is None:
            return False
        self._command(f"window.__jarvisSetSkin && window.__jarvisSetSkin({json.dumps(skin_id)})")
        return True

    def set_caption(self, text: str) -> None:
        """What she just said, in a bubble standing on her right.

        Its own window rather than pixels on her frame: the frame is re-premultiplied
        for every frame the page ships (measured 13.8 ms at 420x640), so a frame wide
        enough to hold words would roughly halve her frame rate for as long as the
        words are up -- and it would put them *over* her, which is what this replaced.
        Text is static, so this window is painted once per answer, carries no
        per-frame cost, and stays click-through: a passing bubble does not eat the
        clicks that belong to whatever is under it.

        Re-stating the same sentence does not buy more time: snapshots arrive on every
        voice transition, and letting each one push the deadline out is how "the last
        answer" ends up living on the desktop forever.
        """
        wanted = text or ""
        if wanted == self._caption:
            return
        self._caption = wanted
        self._caption_until = time.monotonic() + CAPTION_HELD_SECONDS if wanted else 0.0
        self._apply_caption()

    def set_palette(self, palette: BubblePalette) -> None:
        """Repaint her desktop cards in the skin's colours. Cheap, safe to re-state.

        The card is the one piece of her that Python paints, so it is the one piece a
        CSS-only skin change would leave behind: an amber figure with a cyan caption is
        the same half-retinted failure the 3D layer already hit once. The page reports
        its own inks (``pet_palette`` on the bridge) because the six skins live in
        ``theme.ts`` and a second copy of them here would be a list that expires.

        Re-stating happens by design, twice over: the page reports on mount and again on
        every skin change, and the bridge re-pushes whatever it holds when a figure is
        attached. Ignoring a colour already in use is what keeps that from re-shaking a
        card that is already painted in it.

        Every window call stays on the caller's thread, as with ``set_caption``: this
        repaints a live bubble if one is up, and the animation frames are dropped so the
        next tick rebuilds them in the new colours.
        """
        if palette == self._bubble_palette:
            return
        self._bubble_palette = palette
        self._thinking_frames = None
        self._thinking_refused = False
        self._thinking_painted = False
        if self._live_caption():
            self._apply_caption()

    def set_actions(self, actions: Sequence[str]) -> None:
        """Which icons to hang on her cards. Any thread; repaints what is already up.

        The shell drives this from the task table: 停止 appears while something is
        running or waiting, and the ＋ is there whenever a card is, because "and then
        also ask this" does not stop being useful when she is idle.

        Repainting is not free -- eight PNGs for the animation -- so the frames are only
        dropped when the icon set actually changed. A shell that re-stated this on every
        turn update would otherwise re-render the card twenty times a second.
        """
        wanted = tuple(actions)
        if wanted == self._bubble_actions:
            return
        self._bubble_actions = wanted
        self._thinking_frames = None
        self._thinking_painted = False
        if self._live_caption():
            self._apply_caption()

    def on_card_tap(self, target: str) -> None:
        """An icon on one of her cards was pressed. Runs on the window's pump thread."""
        handler = self._on_action
        if handler is None or target == GRAB_TARGET:
            return
        try:
            handler(target)
        except Exception:  # pragma: no cover - a card must not eat the shell's errors
            logger.warning("气泡上的「%s」没有人接", target, exc_info=True)

    def on_figure_tap(self, target: str) -> None:
        """A tap on her own pixels. Only the handle strip answers, and it answers with 收.

        Her body deliberately does nothing: making the whole figure clickable meant
        every attempt to see whether she had become draggable hid her instead.
        """
        if target == GRAB_TARGET:
            self.hide()

    def _sync_card_clicks(self, window: AlphaWindow) -> None:
        """Let a card take a click only where its icons are.

        The same bargain as the figure, and for the same measured reason: a window that
        swallows every click inside its box is a hole in the desktop, and a
        ``WS_EX_TRANSPARENT`` window never receives the ``WM_MOUSEMOVE`` that would have
        told it the pointer arrived -- so the poller decides, from the icon rects the
        picture was built with.
        """
        rect = window.rect
        if not rect[2] or not rect[3] or window.pressing:
            return
        cursor = _cursor_position()
        if cursor is None:
            return
        over = window.button_at(cursor[0] - rect[0], cursor[1] - rect[1]) is not None
        if window.set_click_through(not over) and over:
            logger.debug("the pointer found her card at %s,%s", cursor[0], cursor[1])

    def set_thinking(self, active: bool, *, source: str = "turn") -> None:
        """Say that one particular thing is waiting on the model. Any thread, cheap.

        Sources are named because there are two of them: the task table (a typed
        question) and the microphone (a spoken one). One shared boolean would let the
        voice path put the card away while a typed turn is still running, which is the
        same mistake as serving two messages out of one window.

        It only records the intent. Every window call for this bubble happens on the
        poller (:meth:`_sync_thinking`): the turn's worker thread and the pointer thread
        flipping the same HWND is how a bubble ends up stuck on the desktop after the
        answer has long since arrived.
        """
        if active:
            self._thinking_sources.add(source)
        else:
            self._thinking_sources.discard(source)
        self._thinking = bool(self._thinking_sources)

    def _live_caption(self) -> str:
        """The caption to show right now, empty once it has expired."""
        if not self._caption:
            return ""
        if time.monotonic() >= self._caption_until:
            self._caption = ""
            return ""
        return self._caption

    def _apply_caption(self) -> None:
        """Make the bubble window match the caption: show it, move it, or hide it."""
        text = self._live_caption()
        window = self._caption_window
        if not text:
            if window is not None and window.alive:
                window.set_visible(False)
            return
        compositor = self._compositor
        if compositor is None or not self._shown:
            return  # panel mode: the page draws its own caption there
        built = build_bubble(text, palette=self._bubble_palette, actions=self._bubble_actions)
        if built is None:
            return
        window = self._ensure_caption_window()
        if window is None:
            return
        # The icons are set with the picture and never separately: they are where this
        # particular wrap put them, and a stale rect is a button that fires something else.
        window.set_hot_spots(built.buttons)
        # 每次都设, 而不是只在创建时设一次: 这是这个窗口的定义(一句会飘走的话,
        # 不该挡住底下的点击), 而 set_click_through 在不变化时不碰样式.
        window.set_click_through(True)
        window.present(built.url)
        x, y = self._caption_origin(built.width, built.height)
        window.move_to(x, y)
        window.set_visible(True)

    def _hide_caption(self) -> None:
        """Take the bubble off screen without forgetting what she said."""
        window = self._caption_window
        if window is not None and window.alive:
            window.set_visible(False)

    def _ensure_caption_window(self) -> AlphaWindow | None:
        """Build the bubble's window once, and never more than one."""
        window = self._caption_window
        if window is not None and window.alive:
            return window
        window = AlphaWindow(class_name="XiaoYePetBubble", on_tap=self.on_card_tap)
        if not window.start(x=OFF_SCREEN, y=OFF_SCREEN, width=1, height=1):
            logger.warning("气泡窗起不来，这句话只留在对话面板里")
            return None
        # 创建时不带键盘焦点(AlphaWindow 自己的规矩), 点穿在 _apply_caption 里逐次确保.
        self._caption_window = window
        return window

    def _caption_origin(self, width: int, height: int) -> tuple[int, int]:
        """Her right side, or her left when the right one is off the screen."""
        compositor = self._compositor
        if compositor is None:
            return OFF_SCREEN, OFF_SCREEN
        left, top, right, bottom = compositor.screen_rect
        screen_width, screen_height = _primary_screen()
        x = right + CAPTION_GAP
        if x + width > screen_width:
            # Near the right edge the bubble moves to her other side: reading it is
            # the point, and a bubble clipped by the screen is a bubble half missing.
            x = left - CAPTION_GAP - width
        x = max(0, min(x, max(0, screen_width - width)))
        y = top + int((bottom - top) * CAPTION_TOP_RATIO)
        y = max(0, min(y, max(0, screen_height - height)))
        return int(x), int(y)

    def _sync_caption(self) -> None:
        """Called from the poller: expire it, and keep it beside her while she moves."""
        text = self._live_caption()
        window = self._caption_window
        if window is None or not window.alive:
            return
        if not text:
            if window.rect[2] and window.rect[3]:
                window.set_visible(False)
            return
        if not self._shown:
            return
        x, y = self._caption_origin(window.rect[2], window.rect[3])
        if (x, y) != (window.rect[0], window.rect[1]):
            window.move_to(x, y)
        window.keep_topmost()
        self._sync_card_clicks(window)

    def _ensure_thinking_window(self) -> AlphaWindow | None:
        """The 「思考中」 card's own window, built at most once."""
        window = self._thinking_window
        if window is not None and window.alive:
            return window
        window = AlphaWindow(class_name="XiaoYePetThinking", on_tap=self.on_card_tap)
        if not window.start(x=OFF_SCREEN, y=OFF_SCREEN, width=1, height=1):
            logger.warning("思考气泡窗起不来，「思考中」只留在对话面板里")
            return None
        # A fresh window holds no picture: present() has to run again before the blink
        # has anything to show.
        self._thinking_painted = False
        self._thinking_visible = False
        self._thinking_window = window
        return window

    def set_thinking_loader(self, kind: str) -> None:
        """Switch which animation the card cycles. Cheap, and safe to call every apply."""
        if kind == self._thinking_kind:
            return
        self._thinking_kind = kind
        # The next tick rebuilds the frame list for the new kind; the refusal flag goes
        # with it, because "no font" is an answer about the machine, not about this kind.
        self._thinking_frames = None
        self._thinking_refused = False
        self._thinking_painted = False

    def _sync_thinking(self) -> None:
        """Cycle the 「思考中」 card. Runs on the poller, and only there.

        The pictures are encoded once per loader kind and swapped thereafter, because
        this loop turns over twenty times a second and every present costs a decode plus
        one ``UpdateLayeredWindow``. Encoding eight small cards once is the whole cost of
        the animation; the alternative -- redrawing per tick -- is the per-frame work that
        moving captions out of her frame was meant to remove.
        """
        window = self._thinking_window
        if not self._thinking or not self._shown:
            if window is not None and window.alive and self._thinking_visible:
                window.set_visible(False)
                self._thinking_visible = False
            return
        if self._compositor is None:
            return  # panel mode: the page draws its own indicator
        frames = self._thinking_frames
        if frames is None:
            if self._thinking_refused:
                return
            frames = build_thinking_frames(
                self._thinking_kind, self._bubble_palette, self._bubble_actions
            )
            if not frames:
                self._thinking_refused = True
                logger.warning("「思考中」卡片画不出来（字体或 PIL 没到位），这次不试了")
                return
            self._thinking_frames = frames
        window = self._ensure_thinking_window()
        if window is None:
            return
        now = time.monotonic()
        # Every tick, not every frame: the icons are where the pointer has to land, and a
        # decision made only when the picture changes would leave the card deaf for
        # seven ticks out of eight.
        self._sync_card_clicks(window)
        if self._thinking_painted and now < self._thinking_next_at:
            return
        if self._thinking_painted:
            self._thinking_index = (self._thinking_index + 1) % len(frames)
        card = frames[self._thinking_index]
        window.set_hot_spots(card.buttons)
        window.present(card.url)
        self._thinking_painted = True
        self._thinking_next_at = now + 1.0 / THINKING_FPS
        x, y = self._caption_origin(card.width, card.height)
        caption = self._caption_window
        if caption is not None and caption.alive and caption.rect[3] and self._live_caption():
            # Above the sentence she is still showing, in the same column: two bubbles
            # fighting for one spot is one bubble that is never quite readable.
            x, y = caption.rect[0], max(0, caption.rect[1] - card.height - CAPTION_GAP)
        if (x, y) != (window.rect[0], window.rect[1]):
            window.move_to(x, y)
        if not self._thinking_visible:
            window.set_visible(True)
            self._thinking_visible = True

    def on_snapshot(self, snapshot: object) -> None:
        """Follow the transcript: her last answer stays beside her, a new question clears it.

        Duck-typed rather than importing :class:`jarvis.ui.state_bridge.UiState`: this
        is the same bargain the tray icon makes with pipeline events -- read the two
        fields, ignore the rest, never raise.
        """
        raw: object = getattr(snapshot, "history", None)
        turns = raw if isinstance(raw, (list, tuple)) else ()
        if not turns:
            return
        last = turns[-1]
        role = str(getattr(last, "role", "") or "")
        text = str(getattr(last, "text", "") or "")
        if role == "assistant" and text:
            self.set_caption(text)
        elif role == "user":
            self.set_caption("")

    def _push_pointer(
        self, rect: tuple[int, int, int, int], cursor: tuple[int, int] | None
    ) -> None:
        """Tell the page where the cursor is, so she can look at it.

        ``cursor`` is handed in by the caller, which samples it once per round.
        Reading it here as well meant two ``GetCursorPos`` calls for one answer --
        twenty rounds a second, so forty syscalls doing the work of twenty.

        Only sent when it moved by more than a pixel or two of her own width: the
        bridge call is cheap but not free, and an unchanged pointer is unchanged.
        """
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
        """Let her be clickable and nothing else be."""
        while not self._stop.is_set():
            time.sleep(CURSOR_POLL_SECONDS)
            if self._dragging or not self._shown:
                continue
            self._poll_once()

    def _poll_once(self) -> None:
        """One decision, and one decision never kills the thread.

        A raise here used to end the pet quietly: no more click-through decisions, no
        more topmost re-asserts, and nothing in the log after the traceback that scrolled
        past on a machine with no console. The window still looked fine -- it just stopped
        answering to the pointer, which is the failure the operator reports as "she froze"
        when she did not.
        """
        try:
            self._sync_caption()
        except Exception:  # pragma: no cover - a bubble must not stop the pointer
            logger.warning("气泡同步出错；这一句先不管", exc_info=True)
        try:
            self._sync_thinking()
        except Exception:  # pragma: no cover - an indicator must not stop the pointer
            logger.warning("思考气泡同步出错；这一轮先不管", exc_info=True)
        try:
            self._sync_to_cursor()
        except Exception as exc:  # pragma: no cover - needs a compositor that misbehaves
            self._poll_failures += 1
            if self._poll_failures == 1 or self._poll_failures % 200 == 0:
                logger.warning(
                    "指针轮询出错（%s），穿透与置顶决策已停 %d 次；继续尝试",
                    exc,
                    self._poll_failures,
                    exc_info=(self._poll_failures == 1),
                )

    def _sync_to_cursor(self) -> bool:
        """One decision. Returns whether it could be made at all."""
        compositor = self._compositor
        if compositor is not None and compositor.alive:
            # Sampled once and handed down: the pointer push wants the same answer this
            # decision is about, and asking the OS twice per round is pure cost.
            cursor = _cursor_position()
            # The renderer window is off screen and takes no input; the compositor owns
            # the pixels, so it owns the answer to "is the pointer on her?".
            self._push_pointer(compositor.screen_rect, cursor)
            if self._dragging or compositor.pressing:
                # Mid-drag: the compositor has the capture. Re-deciding here would
                # make her transparent the moment the pointer slid off the pill and
                # drop the rest of the drag on the floor.
                return True
            rect = _window_rect(compositor.hwnd)
            if cursor is None or rect is None:
                return False
            on_her = compositor.opaque_at(cursor[0] - rect[0], cursor[1] - rect[1])
            if compositor.set_click_through(not on_her) and on_her:
                logger.debug("the pointer found her at %s,%s", cursor[0], cursor[1])
            compositor.keep_topmost()
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

    def _notify_window(self, window: Any | None) -> None:
        """Tell the shell which window to push state into, or that there is none.

        Same bargain as :meth:`_notify`: this is the shell's bookkeeping, so a hook
        that raises must not take the pet down with it.
        """
        handler = self._on_window_change
        if handler is None:
            return
        try:
            handler(window)
        except Exception:  # pragma: no cover - the shell's bookkeeping, not its job
            logger.debug("pet window hook failed", exc_info=True)

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
