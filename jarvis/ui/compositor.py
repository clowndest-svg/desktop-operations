"""A window that can really be transparent, for the desktop figure.

Why this file exists
--------------------
The pet is drawn by Three.js inside a WebView2 window, and that window cannot be
made transparent. This was measured four ways in §11j of the operations guide and
re-checked here before writing a line of it: colour-key transparency
(``WS_EX_LAYERED`` + ``LWA_COLORKEY``) applies to the top-level form, but the pixels
in question belong to the **child** WebView2 window, which composites through
DirectComposition and is not affected by its parent's key. What the operator saw was
a dark rectangle standing on the desktop -- correct pixels, wrong promise.

A window with per-pixel alpha has to be one whose pixels the process owns. So this is
a plain Win32 layered window, created and pumped on its own thread, that receives the
figure's already-rendered frames from the page (a PNG with real alpha, measured at
8 ms to encode and 9.5 ms to cross the bridge at 420x640) and hands them to
``UpdateLayeredWindow``. Everything outside the figure is a hole in the desktop:
clicks land on whatever is behind it, and there is no backdrop to remove.

Why the window also owns the pointer
------------------------------------
The renderer window has moved off screen to keep rendering, so it never receives a
mouse event. The one thing on the pet that takes a click is whatever the figure's
own pixels cover, and the page can only say where it drew the handle strip.

So the decision is made by the shell's pointer poller (:mod:`jarvis.ui.pet`), not by
this window. It has to be that way round: while ``WS_EX_TRANSPARENT`` is set the
desktop hit test *skips* this window, so no ``WM_MOUSEMOVE`` can ever arrive here to
clear it. A window cannot use its own mouse events to decide whether it gets mouse
events. Measured, and it is why "按住拖动" sat there doing nothing.

Two more facts this file had to learn by measurement, both about
``SetWindowLongW``:

* ``WS_EX_LAYERED`` added to an existing window by ``SetWindowLongW`` works every
  time (creating the window with it fails with error 1400).
* ``WS_EX_TOPMOST`` added the same way **does not** -- the style is read back without
  that bit, and the window stays below an ordinary one. Only
  ``SetWindowPos(hwnd, HWND_TOPMOST, ...)`` inserts it into the topmost band, which
  is why :meth:`AlphaWindow.raise_topmost` is a separate call and
  :meth:`AlphaWindow.keep_topmost` re-asserts it from the poller.

Threading
---------
``present`` is called from the JS-bridge thread and does the expensive part (decode,
premultiply) there, then posts a message; the pump thread only ever does the GDI
call. That split is deliberate: ``UpdateLayeredWindow`` on a thread that is not
pumping its own messages is a recipe for a window that stops updating, and decoding a
PNG inside a window procedure holds up every mouse event behind it.
"""

from __future__ import annotations

import base64
import ctypes
import io
import logging
import math
import threading
import time
from collections.abc import Callable, Sequence
from ctypes import wintypes
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger("jarvis.ui.compositor")

# 0x80000000. The first version had 0 here, which is not "no style" but
# WS_OVERLAPPED -- and the pet came back with a title bar and a border, which
# is a very expensive way to learn the difference.
WS_POPUP = 0x80000000
WS_VISIBLE = 0x10000000
WS_EX_TOPMOST = 0x00000008
WS_EX_TOOLWINDOW = 0x00000040
WS_EX_LAYERED = 0x00080000
WS_EX_TRANSPARENT = 0x00000020
WS_EX_NOACTIVATE = 0x08000000

WM_DESTROY = 0x0002
WM_LBUTTONDOWN = 0x0201
WM_LBUTTONUP = 0x0202
WM_MOUSEMOVE = 0x0200
WM_QUIT = 0x0012
PM_REMOVE = 0x0001

FRAME_POLL_SECONDS = 0.012
"""How often the pump looks for a frame: 80 Hz against a figure that paints at 20."""

CS_HREDRAW = 0x0002
CS_VREDRAW = 0x0001

BI_RGB = 0
DIB_RGB_COLORS = 0
ULW_ALPHA = 0x00000002
AC_SRC_OVER = 0x00
AC_SRC_ALPHA = 0x01

SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOACTIVATE = 0x0010
HWND_TOPMOST = -1
SW_HIDE = 0
SW_SHOWNOACTIVATE = 4
"""Show her without activating her. ``SW_SHOW`` (5) would take the keyboard."""

OFF_SCREEN_PLACEHOLDER = -32000
"""Where the window is born: the styles get added before anyone can see it.

It has to be *created* visible (a window created hidden can never be entered into the
topmost band), and an uncomposited top-level window is an opaque rectangle -- so it is
created visible somewhere nobody can see, and moved into place in the same call that
makes it topmost.
"""

GRAB_SLOP = 6
"""Pixels of movement that still count as a click rather than a drag.

A person pressing and releasing on a 60-pixel-wide pill moves a couple of pixels;
without this the tap would become a one-pixel drag and the pet would not hide.
"""

GRAB_TARGET = "grab"
"""What a tap on her handle strip is called, in the same vocabulary as the card icons."""

OPAQUE_MIN_ALPHA = 24
"""How solid a pixel has to be for the pointer to be *on her*.

She is a wireframe: a strict one-pixel test would only be true exactly on a line,
and a figure you can only grab where she happens to be a fifth of a pixel wide is
not grabbable. The poller scans a neighbourhood around the cursor, so this stays a
threshold rather than a morphological operation.

Read off the **premultiplied** buffer: ``premultiply`` multiplies the colour
channels by alpha and leaves alpha itself alone, so the fourth byte is still the
coverage the page drew.
"""

GRAB_SCAN_RADIUS = 9
"""How far around the cursor to look for her pixels, in device pixels."""

GRAB_SCAN_STEP = 3
"""The lattice the scan walks. Three pixels is finer than a person can notice and
a third of the samples of one-pixel stepping, on a poll that runs twenty times a
second."""

TOPMOST_REASSERT_SECONDS = 2.0
"""How often the shell re-asserts the topmost band while she is on screen.

She is one topmost window among several, and Windows puts the most recent one last
in line -- a screen recorder, an overlay, a notification. Re-asserting is one cheap
``SetWindowPos`` and is idempotent when nothing moved; the alternative is polling
the z-order to find out she fell behind, which costs more and still loses to a
window that re-asserts faster than we look.

Honest limit: this wins against ordinary windows, not against a full-screen
exclusive game or something that pins itself above the topmost band."""

BUBBLE_MAX_WIDTH = 176
"""How wide her speech bubble may get, in pixels of the desktop.

A ceiling, not a target: the tile is sized to the text up to this, then wraps.

It came down from 300 along with the font (see :data:`BUBBLE_FONT_SIZE`): at 21 px the
bubble stood 87 px tall for two lines of answer, which reads as a second window next to
a 400 px figure. The reference the operator pointed at -- the task cards beside Qoder's
own pet -- are ~13 px text on a ~56 px card, and this is the scale it matches now.
232 → 176 is the second step down the same ruler: he asked again, and "还是太大" is an
answer about the desk, not about the code."""

BUBBLE_FONT_SIZE = 13
"""Answer text, in desktop pixels.

Small on purpose, and *not* scaled by the bubble's width any more: the bubble is drawn
in physical desktop pixels, so a font that scales with the tile scales with nothing at
all -- it was 21 px because the tile was 300 px wide, not because 21 px was right for a
desk. 13 px is the size the operator's own reference cards use."""

BUBBLE_PADDING = 7
"""Breathing room inside the bubble, all four sides.

The old build reused the font size for the padding, the corner radius *and* the tail, so
shrinking the text would have shrunk all three at once. They are separate numbers now:
the tail wants to be small even when the text is generous."""

BUBBLE_LINES = 3
"""Long enough for a sentence, short enough to read at a glance across a desk."""


class _BitmapInfoHeader(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD),
        ("biWidth", wintypes.LONG),
        ("biHeight", wintypes.LONG),
        ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD),
        ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD),
        ("biXPelsPerMeter", wintypes.LONG),
        ("biYPelsPerMeter", wintypes.LONG),
        ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]


class _BlendFunction(ctypes.Structure):
    _fields_ = [
        ("BlendOp", ctypes.c_ubyte),
        ("BlendFlags", ctypes.c_ubyte),
        ("SourceConstantAlpha", ctypes.c_ubyte),
        ("AlphaFormat", ctypes.c_ubyte),
    ]


class _WndClass(ctypes.Structure):
    _fields_ = [
        ("style", wintypes.UINT),
        ("lpfnWndProc", ctypes.c_void_p),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HINSTANCE),
        ("hIcon", wintypes.HICON),
        ("hCursor", wintypes.HANDLE),
        ("hbrBackground", wintypes.HANDLE),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
        ("hIconSm", wintypes.HICON),
    ]


# ``Any`` rather than ``WinDLL | None``: every call site below is a Win32 entry point,
# and a union with ``None`` would need a guard on each of them to satisfy mypy. The one
# real check lives in :meth:`AlphaWindow.start`, which is where a non-Windows run fails.
#
# ``use_last_error=True`` because ``ctypes.get_last_error()`` only reports anything for
# functions created that way: without it the reader always sees 0, and a real failure --
# "class already registered (1410)" -- gets logged as "(0)" and read as "no reason".
def _load_dll(name: str) -> Any:
    """Load one Win32 library with a *working* last-error, or ``None`` off Windows."""
    try:
        return ctypes.WinDLL(name, use_last_error=True)
    except (OSError, AttributeError):  # pragma: no cover - non-Windows
        return None


_USER32: Any = _load_dll("user32")
_KERNEL32: Any = _load_dll("kernel32")
_GDI32: Any = getattr(ctypes.windll, "gdi32", None)


def _prototype(dll: Any, name: str, restype: Any, argtypes: list[Any]) -> Any:
    """Declare one entry point.

    Not ceremony: ctypes assumes ``int`` for an undeclared return value, and a
    handle on x64 is a 64-bit pointer. Truncate ``CreateCompatibleDC`` and you get
    a plausible-looking ``HDC`` that belongs to nothing -- a failure that shows up
    as a black pet, on one machine, at night, with no error anywhere.
    """
    function = getattr(dll, name)
    function.restype = restype
    function.argtypes = argtypes
    return function


if _USER32 is not None:
    _prototype(_USER32, "GetDC", wintypes.HDC, [wintypes.HWND])
    _prototype(_USER32, "ReleaseDC", ctypes.c_int, [wintypes.HWND, wintypes.HDC])
    _prototype(
        _USER32,
        "CreateWindowExW",
        wintypes.HWND,
        [
            wintypes.DWORD,
            wintypes.LPCWSTR,
            wintypes.LPCWSTR,
            wintypes.DWORD,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            wintypes.HWND,
            wintypes.HMENU,
            wintypes.HINSTANCE,
            wintypes.LPVOID,
        ],
    )
    _prototype(
        _USER32,
        "DefWindowProcW",
        ctypes.c_long,
        [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM],
    )
    # ``LPVOID`` for the second argument because IDC_ARROW is MAKEINTRESOURCE(32512)
    # -- an integer wearing a pointer's clothes, which ``LPCWSTR`` rightly refuses.
    _prototype(_USER32, "LoadCursorW", wintypes.HANDLE, [wintypes.HINSTANCE, wintypes.LPVOID])
    _prototype(
        _USER32,
        "UpdateLayeredWindow",
        wintypes.BOOL,
        [
            wintypes.HWND,
            wintypes.HDC,
            ctypes.POINTER(wintypes.POINT),
            ctypes.POINTER(wintypes.SIZE),
            wintypes.HDC,
            ctypes.POINTER(wintypes.POINT),
            wintypes.DWORD,
            ctypes.POINTER(_BlendFunction),
            wintypes.DWORD,
        ],
    )
    # ``SetWindowPos`` takes two handles and returns a BOOL. Without the prototype
    # ctypes reads the return as a 32-bit int and passes ``HWND(-1)`` as one too, and
    # a truncated insert-after handle is a window that lands somewhere arbitrary.
    _prototype(
        _USER32,
        "SetWindowPos",
        wintypes.BOOL,
        [
            wintypes.HWND,
            wintypes.HWND,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_uint,
        ],
    )
    _prototype(
        _USER32, "SetWindowLongW", ctypes.c_long, [wintypes.HWND, ctypes.c_int, ctypes.c_long]
    )
    _prototype(_USER32, "ShowWindow", wintypes.BOOL, [wintypes.HWND, ctypes.c_int])
if _KERNEL32 is not None:
    _prototype(_KERNEL32, "GetCurrentThreadId", wintypes.DWORD, [])
    _prototype(_KERNEL32, "GetModuleHandleW", wintypes.HINSTANCE, [wintypes.LPCWSTR])
if _GDI32 is not None:
    _prototype(_GDI32, "CreateCompatibleDC", wintypes.HDC, [wintypes.HDC])
    _prototype(
        _GDI32,
        "CreateDIBSection",
        wintypes.HBITMAP,
        [
            wintypes.HDC,
            ctypes.POINTER(_BitmapInfoHeader),
            wintypes.UINT,
            ctypes.POINTER(ctypes.c_void_p),
            wintypes.HANDLE,
            wintypes.DWORD,
        ],
    )
    _prototype(_GDI32, "SelectObject", wintypes.HGDIOBJ, [wintypes.HDC, wintypes.HGDIOBJ])
    _prototype(_GDI32, "DeleteObject", wintypes.BOOL, [wintypes.HGDIOBJ])
    _prototype(_GDI32, "DeleteDC", wintypes.BOOL, [wintypes.HDC])


def in_rect(
    rect: tuple[float, float, float, float], x: int, y: int, width: int, height: int
) -> bool:
    """Is the pointer inside a normalised ``(x, y, w, h)`` patch of this window?

    The page reports the handle as fractions of its own viewport because that is the
    only frame of reference layout gives it; this converts to pixels and forgives a
    rounding difference of a pixel at the far edge.
    """
    left, top = rect[0] * width, rect[1] * height
    return (
        left - 1 <= x <= left + rect[2] * width + 1 and top - 1 <= y <= top + rect[3] * height + 1
    )


HANDLE_TEXT = "小夜 · 按住拖动"
"""The one clickable patch, labelled.

Drawn here rather than in the page because this window composites the WebGL canvas
only -- an HTML pill would never reach the desktop -- and because the shell already
owns the rectangle: the label and the thing it labels cannot then disagree.
"""

FONT_CANDIDATES = (
    r"C:\Windows\Fonts\msyh.ttc",
    r"C:\Windows\Fonts\msyhbd.ttc",
    r"C:\Windows\Fonts\simhei.ttf",
)
"""Chinese-capable system fonts, tried in order. Missing all three is not a failure:
the pet simply has no label, which is the same as before this existed."""


_PILL_CACHE: dict[tuple[object, ...], Any] = {}
"""The pill, drawn once per size and pasted onto every frame after that.

Text shaping at 20 frames a second is CPU spent re-laying-out the same four
words; ``alpha_composite`` is a single C-level blend.
"""


def _build_pill(box: tuple[int, int, int, int]) -> Any:
    """Render the label once, at the rectangle the page reported."""
    from PIL import Image, ImageDraw

    width = box[2] - box[0]
    height = box[3] - box[1]
    tile = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    paint = ImageDraw.Draw(tile)
    radius = height // 2
    paint.rounded_rectangle(
        (0, 0, width - 1, height - 1),
        radius=radius,
        fill=(6, 16, 28, 200),
        outline=(77, 216, 255, 190),
        width=2,
    )
    font = font_at(max(12, radius))
    if font is None:  # a pill with no word in it still says "grab here"
        paint.ellipse((radius, radius, radius * 2, radius * 2), fill=(143, 232, 255, 230))
    else:
        paint.text(
            (width / 2, height / 2),
            HANDLE_TEXT,
            font=font,
            fill=(143, 232, 255, 255),
            anchor="mm",
        )
    return tile


_FONT_CACHE: dict[int, Any] = {}
"""Loaded at a pixel size and kept: ``ImageFont.truetype`` opens the file every call,
and the caption is redrawn whenever she says something new."""


def font_at(px: int) -> Any:
    """A Chinese-capable font at ``px``, or ``None`` when the machine has none."""
    from PIL import ImageFont

    size = max(10, int(px))
    cached = _FONT_CACHE.get(size)
    if cached is not None:
        return cached if cached is not False else None
    font: Any = False
    for path in FONT_CANDIDATES:
        try:
            font = ImageFont.truetype(path, size)
            break
        except OSError:
            continue
    _FONT_CACHE[size] = font
    return font if font is not False else None


def _breakable_after(text: str, index: int) -> bool:
    """Can a line end right after ``text[index]``?

    Chinese can break anywhere, and must. An ASCII run cannot: breaking ``45.0%`` into
    ``45.`` + ``0%`` is the same class of mistake as a text-to-speech pass that reads it
    as "450", and this project has already been burned by exactly that number.
    """
    character = text[index]
    if not character.isascii():
        return True
    if character in " ,;:/-":
        return True
    following = text[index + 1] if index + 1 < len(text) else ""
    return not following or not following.isascii() or following in " ,;:/-"


def wrap_text(paint: Any, text: str, font: Any, max_width: int, max_lines: int) -> list[str]:
    """Break one answer into at most ``max_lines`` lines of at most ``max_width`` pixels.

    Greedy, and character-based because Chinese has no spaces to break on: the line
    runs as far as the width allows, then steps back to the last legal boundary; if the
    whole run is one unbreakable token it breaks where the width says and the word is
    split rather than the bubble overflowing.
    """
    wanted = " ".join(str(text).split())[:240]
    if not wanted:
        return []
    lines: list[str] = []
    length = len(wanted)
    start = 0
    while start < length and len(lines) < max_lines:
        end, boundary = start, -1
        while end < length and paint.textlength(wanted[start : end + 1], font=font) <= max_width:
            if _breakable_after(wanted, end):
                boundary = end + 1
            end += 1
        if end >= length:
            lines.append(wanted[start:].rstrip())
            break
        cut = boundary if boundary > start else end
        lines.append(wanted[start:cut].rstrip())
        start = cut
    if len(lines) == max_lines and start < length:
        lines[-1] = lines[-1].rstrip() + "…"
    return [line for line in lines if line]


_BUBBLE_CACHE: dict[tuple[str, int, int, int, BubblePalette, tuple[str, ...]], Any] = {}
"""The last bubble, drawn once. Cleared on every new caption: one is live at a time,
and letting forty answers pile up in memory to save 4 ms once is not a trade."""

_THINKING_CACHE: dict[tuple[str, BubblePalette, tuple[str, ...]], list[Bubble]] = {}
"""The 「思考中」 card, eight encoded pictures per loader kind, palette and icon set.

Kept per kind and palette rather than cleared: the operator can switch kinds in the
settings panel and switch back, or change skins twice, and re-rendering eight small
PNGs on every swap would be the only work this window does.
"""

CAPTION_INK = (232, 240, 250, 255)
"""近白, 偏冷一点: 深色底上要的是对比, 不是再来一层蓝。"""

CAPTION_FILL = (14, 18, 26, 231)
"""Neutral near-black, not tinted glass.

The reference card beside Qoder's pet is this colour, and the reason is legibility:
text stands up on a neutral dark, and a blue-black fill next to a blue figure reads as
part of the figure rather than as something written on top of the desktop."""

CAPTION_RIM = (120, 160, 200, 70)
"""A border you have to look for. It was 2 px of bright cyan, which on a card this
small turns the bubble into a button -- the reference has no visible border at all."""


@dataclass(frozen=True)
class BubblePalette:
    """The three inks a bubble is drawn with, in RGBA.

    A value rather than three module constants because the figure has six skins and the
    card is the one piece of her that is painted by Python: a cyan card next to an amber
    hologram is the "half-retinted bust" failure that already bit the 3D layer once.
    """

    fill: tuple[int, int, int, int]
    ink: tuple[int, int, int, int]
    rim: tuple[int, int, int, int]


DEFAULT_BUBBLE = BubblePalette(CAPTION_FILL, CAPTION_INK, CAPTION_RIM)
"""The palette used before the page has said what it is wearing (and in tests)."""


def _rgb(value: int) -> tuple[int, int, int]:
    return (value >> 16 & 255, value >> 8 & 255, value & 255)


BUBBLE_STOP = "stop"
BUBBLE_ADD = "add"
"""The two things a card beside her can be asked to do: end this round, ask another.

Named rather than drawn as text: the picture has no room for two Chinese labels at this
size, and a card that grows to fit words is the "太大了" the operator already reported."""

ICON_BOX = 16
ICON_GAP = 5
"""The icon chips and the air around them, in desktop pixels."""


@dataclass(frozen=True, slots=True)
class BubbleButton:
    """Where one icon ended up inside the finished picture, in window-local pixels."""

    name: str
    x: int
    y: int
    size: int


@dataclass(frozen=True, slots=True)
class Bubble:
    """One painted card: the picture, its size, and the parts of it that answer to a click.

    The buttons travel with the picture because they are computed from the same wrap: a
    second call to ask "where did the stop icon go?" would be a second layout that can
    disagree with the first -- and clicking one thing while another is drawn is worse than
    no buttons at all.
    """

    url: str
    width: int
    height: int
    buttons: tuple[BubbleButton, ...] = ()


def _encode(tile: Any) -> str:
    buffer = io.BytesIO()
    tile.save(buffer, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")


def _icon_column(actions: Sequence[str]) -> int:
    """How much horizontal room a row of icons needs, including the air beside it."""
    if not actions:
        return 0
    return len(actions) * ICON_BOX + (len(actions) - 1) * ICON_GAP + 2 * ICON_GAP


def _draw_icons(
    paint: Any, palette: BubblePalette, actions: Sequence[str], right: int, centre: int
) -> tuple[BubbleButton, ...]:
    """Lay the icons in a row ending at ``right``, centred on ``centre``.

    Shapes, not glyphs: this is the one part of her drawn without the page's fonts, and a
    「▸」 missing from the machine's CJK face would leave a blank where a button should be.
    Two rectangles make a plus and one makes a stop, and both read at 16 px.
    """
    buttons: list[BubbleButton] = []
    chip = (*[min(255, channel + 30) for channel in palette.fill[:3]], palette.fill[3])
    top = centre - ICON_BOX // 2
    for index, name in enumerate(actions):
        left = right - (len(actions) - index) * ICON_BOX - (len(actions) - 1 - index) * ICON_GAP
        paint.rounded_rectangle(
            (left, top, left + ICON_BOX - 1, top + ICON_BOX - 1),
            radius=4,
            fill=chip,
            outline=palette.rim,
            width=1,
        )
        middle = left + ICON_BOX // 2
        middle_y = top + ICON_BOX // 2
        arm = ICON_BOX // 2 - 3
        if name == BUBBLE_STOP:
            paint.rectangle((middle - 3, middle_y - 3, middle + 3, middle_y + 3), fill=palette.ink)
        else:
            bar = (middle - 1, middle_y - arm, middle + 1, middle_y + arm)
            paint.rectangle(bar, fill=palette.ink)
            cross = (middle - arm, middle_y - 1, middle + arm, middle_y + 1)
            paint.rectangle(cross, fill=palette.ink)
        buttons.append(BubbleButton(name=name, x=left, y=top, size=ICON_BOX))
    return tuple(buttons)


def bubble_palette(fill: int, line: int, glow: int) -> BubblePalette:
    """Derive the card's inks from the figure's three inks.

    The card is the skin's own fill pushed further towards black -- a saturated panel
    behind text is unreadable -- and the text is the skin's glow, which is the light
    tone that skin already uses for what it wants you to read.
    """
    deep = _rgb(fill)
    light = _rgb(glow)
    edge = _rgb(line)
    return BubblePalette(
        fill=(deep[0] // 2 + 4, deep[1] // 2 + 4, deep[2] // 2 + 8, 236),
        ink=(min(255, light[0] + 26), min(255, light[1] + 26), min(255, light[2] + 26), 255),
        rim=(edge[0], edge[1], edge[2], 70),
    )


def build_bubble(
    text: str,
    *,
    max_width: int = BUBBLE_MAX_WIDTH,
    palette: BubblePalette = DEFAULT_BUBBLE,
    actions: Sequence[str] = (),
) -> Bubble | None:
    """Draw one answer as a standalone bubble.

    Standalone rather than pasted onto her frame, because of what pasting costs: the
    frame is re-premultiplied on *every* frame the page ships (measured 13.8 ms for
    420x640 on this machine), so widening it to make room for words would roughly
    halve her frame rate while she is talking -- exactly when the words are up. A
    bubble is static pixels, so it gets its own window, painted once per answer and
    click-through except over the icons it was asked to carry.

    The tail points left, toward her: this bubble stands on her right.

    ``palette`` is her skin's card inks -- see :func:`bubble_palette` -- because a cyan
    caption beside an amber figure reads as two pieces of art that disagree.

    ``actions`` are the icons to hang on the right edge. They come out of the *text's*
    width rather than the card's: the ceiling agreed with the operator stays the ceiling,
    and a long answer wraps one character earlier instead of growing a second window.

    ``None`` means "nothing to draw" -- empty text, or no font to shape it with.
    """
    if not text.strip():
        return None
    tile_width = max(120, int(max_width))
    size = BUBBLE_FONT_SIZE
    pad = BUBBLE_PADDING
    icons = tuple(actions)
    key = (text, tile_width, size, pad, palette, icons)
    cached = _BUBBLE_CACHE.get(key)
    if cached is not None:
        tile, buttons = cached
    else:
        from PIL import Image, ImageDraw

        font = font_at(size)
        if font is None:
            return None
        measure = ImageDraw.Draw(Image.new("RGBA", (8, 8)))
        column = _icon_column(icons)
        lines = wrap_text(measure, text, font, tile_width - 2 * pad - column, BUBBLE_LINES)
        if not lines:
            return None
        # 卡片贴着字走: 短句就是一张小卡, 长句才长到上限. 写死上限会让「现在几点了」
        # 变成一张右边空一半的横幅 -- 参考卡也不是定宽的.
        widest = max(measure.textlength(line, font=font) for line in lines)
        body_width = max(60 + column, min(tile_width, int(widest) + 2 * pad + column))
        line_height = int(size * 1.45)
        body_height = line_height * len(lines) + 2 * pad
        tail = max(6, pad - 2)
        tile = Image.new("RGBA", (body_width + tail, body_height), (0, 0, 0, 0))
        paint = ImageDraw.Draw(tile)
        # The body is inset by the tail, which lives in the left margin.
        paint.rounded_rectangle(
            (tail, 0, body_width + tail - 1, body_height - 1),
            radius=min(12, pad + 2),
            fill=palette.fill,
            outline=palette.rim,
            width=1,
        )
        # The tail, pointing left at her. Fill only: outlining it would draw a seam
        # straight down the edge of the bubble, which reads as a second box.
        tip_top = int(body_height * 0.30)
        paint.polygon(
            (
                (tail + 2, tip_top),
                (tail + 2, tip_top + size),
                (2, tip_top + size // 2),
            ),
            fill=palette.fill,
        )
        for index, line in enumerate(lines):
            paint.text(
                (tail + pad, pad + index * line_height + line_height // 2),
                line,
                font=font,
                fill=palette.ink,
                anchor="lm",
            )
        buttons = _draw_icons(paint, palette, icons, tail + body_width - pad, body_height // 2)
        _BUBBLE_CACHE.clear()
        _BUBBLE_CACHE[key] = (tile, tuple(buttons))
    return Bubble(url=_encode(tile), width=tile.size[0], height=tile.size[1], buttons=buttons)


THINKING_TEXT = "思考中"
"""What the pet's thinking card says. The chat bubble uses the same three characters."""

THINKING_FRAMES = 8
"""How many pictures one loader cycles through. Eight is enough for a spin to read as
continuous at eight hertz, and it is the whole cost: the frames are encoded once."""

THINKING_FPS = 8
"""How often the poller swaps to the next frame while she thinks."""

_LOADER_BOX = 15
"""The square the loader is drawn in, in desktop pixels."""


def _loader_ink(palette: BubblePalette, alpha: int) -> tuple[int, int, int, int]:
    """The loader's colour: the card's own ink, at a requested strength."""
    red, green, blue = palette.ink[:3]
    return (red, green, blue, max(28, min(255, alpha)))


def _draw_loader(
    paint: Any, palette: BubblePalette, kind: str, frame: int, x: int, y: int, size: int
) -> None:
    """One frame of one loader, inside the square ``(x, y, size, size)``.

    Kept deliberately coarse -- these are a few dozen pixels next to a figure, and a
    shape that has to be *read* at that size is a shape with fewer parts.
    """
    box = size / 2.0
    if kind == "ring":
        span = 110
        start = frame * (360 // THINKING_FRAMES)
        paint.arc(
            (x + 1, y + 1, x + size - 1, y + size - 1),
            start=start,
            end=start + span,
            fill=_loader_ink(palette, 235),
            width=2,
        )
        return
    if kind == "bars":
        width = max(2, size // 5)
        for index in range(4):
            phase = (frame + index * 2) % THINKING_FRAMES
            height = int(size * (0.32 + 0.62 * abs(math.sin(phase / THINKING_FRAMES * math.pi))))
            left = x + index * (width + 2)
            paint.rectangle(
                (left, y + size - height, left + width, y + size),
                fill=_loader_ink(palette, 200 if index % 2 else 240),
            )
        return
    if kind == "matrix":
        cell = max(3, size // 4)
        for column in range(3):
            for row in range(2):
                head = (frame + column * 3 + row) % THINKING_FRAMES
                paint.rectangle(
                    (x + column * (cell + 1), y + row * (cell + 1)),
                    (x + column * (cell + 1) + cell - 1, y + row * (cell + 1) + cell - 1),
                    fill=_loader_ink(palette, 70 + head * 22),
                )
        return
    radius = size / 6.0
    for index in range(3):
        lit = (frame // 2 + index) % 3 == 0
        centre = x + radius + index * (size - 2 * radius) / 2.0
        paint.ellipse(
            (centre - radius, y + box - radius, centre + radius, y + box + radius),
            fill=_loader_ink(palette, 245 if lit else 90),
        )


def build_thinking_frames(
    kind: str,
    palette: BubblePalette = DEFAULT_BUBBLE,
    actions: Sequence[str] = (),
) -> list[Bubble]:
    """The pet's 「思考中」 card, as ``THINKING_FRAMES`` pre-encoded pictures.

    Pre-rendered rather than drawn per tick because the poller runs twenty times a
    second and ``build_bubble`` re-encodes PNG + base64 on every call: cycling eight
    cached cards costs a decode and one ``UpdateLayeredWindow``, while redrawing costs
    both of those plus the drawing. The blink is a swap of pictures, not new art.

    Cached per ``(kind, palette, actions)`` for the same reason: switching skins or
    showing and hiding the icons repaints the card, and going back to a combination you
    already wore should not repaint it again.

    Returns an empty list when there is no font to shape the text with -- the caller
    says so out loud once instead of opening an empty window.
    """
    icons = tuple(actions)
    cached = _THINKING_CACHE.get((kind, palette, icons))
    if cached is not None:
        return cached
    from PIL import Image, ImageDraw

    font = font_at(BUBBLE_FONT_SIZE)
    if font is None:
        return []
    pad = BUBBLE_PADDING
    tail = max(6, pad - 2)
    gap = 7
    measure = ImageDraw.Draw(Image.new("RGBA", (8, 8)))
    text_width = int(measure.textlength(THINKING_TEXT, font=font))
    body_width = _LOADER_BOX + gap + text_width + 2 * pad + _icon_column(icons)
    body_height = max(int(BUBBLE_FONT_SIZE * 1.6) + 2 * pad, ICON_BOX + 2 * ICON_GAP)
    frames: list[Bubble] = []
    for frame in range(THINKING_FRAMES):
        tile = Image.new("RGBA", (body_width + tail, body_height), (0, 0, 0, 0))
        paint = ImageDraw.Draw(tile)
        paint.rounded_rectangle(
            (tail, 0, body_width + tail - 1, body_height - 1),
            radius=min(12, pad + 2),
            fill=palette.fill,
            outline=palette.rim,
            width=1,
        )
        neck_top = int(body_height * 0.30)
        paint.polygon(
            (
                (tail + 2, neck_top),
                (tail + 2, neck_top + BUBBLE_FONT_SIZE),
                (2, int(body_height * 0.55)),
            ),
            fill=palette.fill,
        )
        middle = body_height // 2
        _draw_loader(
            paint,
            palette,
            kind,
            frame,
            tail + pad,
            middle - _LOADER_BOX // 2,
            _LOADER_BOX,
        )
        paint.text(
            (tail + pad + _LOADER_BOX + gap, middle),
            THINKING_TEXT,
            font=font,
            fill=palette.ink,
            anchor="lm",
        )
        # Drawn last and identically on every frame: the icons are the part the pointer
        # aims at, so a frame that swapped them with the loader would move the target.
        buttons = _draw_icons(paint, palette, icons, tail + body_width - pad, middle)
        frames.append(
            Bubble(url=_encode(tile), width=tile.size[0], height=tile.size[1], buttons=buttons)
        )
    _THINKING_CACHE[(kind, palette, icons)] = frames
    return frames


def draw_handle(image: Any, grab: tuple[float, float, float, float]) -> None:
    """Paste the cached grab pill onto the decoded frame, in place."""
    width, height = image.size
    left = int(grab[0] * width)
    top = int(grab[1] * height)
    box = (left, top, left + int(grab[2] * width), top + int(grab[3] * height))
    if box[2] - box[0] < 40 or box[3] - box[1] < 16:
        return
    key = (box, HANDLE_TEXT)
    pill = _PILL_CACHE.get(key)
    if pill is None:
        pill = _build_pill(box)
        _PILL_CACHE[key] = pill
    image.alpha_composite(pill, (box[0], box[1]))


def premultiply(rgba: Any, size: tuple[int, int]) -> bytes:
    """RGBA pixels → top-down BGRA with **premultiplied** alpha.

    ``UpdateLayeredWindow`` with ``AC_SRC_ALPHA`` requires the colour channels to
    already be multiplied by alpha; feeding it straight RGBA gives a dark fringe
    around every soft edge, which on a glowing wireframe is most of the image.
    """
    import numpy as np

    plane = np.frombuffer(rgba, dtype=np.uint8).reshape(size[1], size[0], 4).astype(np.uint16)
    alpha = plane[..., 3:4]
    rgb = (plane[..., :3] * alpha + 127) // 255
    packed = np.dstack([rgb[..., 2], rgb[..., 1], rgb[..., 0], plane[..., 3]])
    return packed.astype(np.uint8).tobytes()


def decode_png(
    data_url: str,
    grab: tuple[float, float, float, float] | None = None,
) -> tuple[bytes, int, int]:
    """``data:image/png;base64,...`` → ``(premultiplied bytes, width, height)``.

    ``grab`` is the handle rectangle, drawn while the pixels are still a plain RGBA
    image -- after ``premultiply`` the buffer is opaque bytes that belong to GDI, and
    compositing onto a premultiplied frame gets the blending wrong.
    """
    import numpy as np
    from PIL import Image

    payload = data_url.split(",", 1)[1] if data_url.startswith("data:") else data_url
    image = Image.open(io.BytesIO(base64.b64decode(payload))).convert("RGBA")
    if grab is not None:
        draw_handle(image, grab)
    return premultiply(np.asarray(image), image.size), image.size[0], image.size[1]


class AlphaWindow:
    """The layered window, its pump thread, and the frames it shows."""

    def __init__(
        self,
        *,
        on_tap: Callable[[str], object] | None = None,
        class_name: str = "XiaoYePetCompositor",
    ) -> None:
        """A layered alpha window.

        ``class_name`` is per instance because a window class carries its window
        procedure, and one process can only register a given class **once** -- a second
        window of the same class silently inherits the first one's proc, so its frames
        would be painted into the wrong window (measured: the second registration
        returns 0 with 1410, and the failure was invisible until the bubble simply
        never appeared). The bubble therefore registers its own.

        ``on_tap`` is called with the name of whatever was tapped, from the pump thread.
        """
        self._class_name = class_name
        self._hwnd = 0
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._frame: tuple[bytes, int, int] | None = None
        self._surface: tuple[bytes, int, int] | None = None
        # The last frame that reached the screen, kept for the pointer hit test. It is
        # already in memory -- this only holds the reference one paint longer -- and its
        # fourth channel is still the coverage the page drew, which is exactly what
        # "is the cursor on her?" needs.
        self._proc: Any = None  # the callback must outlive the window
        self._mem_dc = 0
        self._dib = 0
        self._bits = 0
        self._cached_size = (0, 0)
        self._rect = (0, 0, 0, 0)
        self._press: tuple[int, int] | None = None
        self._moved = False
        self._click_through = True
        self._grab: tuple[float, float, float, float] | None = None
        self._hot: tuple[BubbleButton, ...] = ()
        self._topmost_at = 0.0
        # ``object`` because the shell's hide() answers with a bool and this call
        # site discards it: a tap is a tap.
        self._on_tap = on_tap
        self._painted = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self, *, x: int, y: int, width: int, height: int) -> bool:
        """Create the window on its own thread. ``False`` when this is not Windows."""
        if _USER32 is None:  # pragma: no cover - the desktop shell is Windows-only
            return False
        self._rect = (x, y, width, height)
        self._thread = threading.Thread(target=self._run, name="jarvis-pet-compositor", daemon=True)
        self._thread.start()
        if not self._ready.wait(3.0):
            logger.warning("the compositor thread never reported in")
            return False
        if not self._hwnd:
            # Reported in, then declined: the thread ran and could not make a
            # window. Returning true here would hand the caller an invisible pet
            # and a log line that says it is on screen.
            logger.warning("the pet compositor has no window; keeping the panel")
            return False
        logger.info("pet compositor up at %sx%s", width, height)
        return True

    def close(self) -> None:
        thread = self._thread
        if thread is None or not thread.is_alive():
            return
        self._stop.set()
        if self._hwnd:
            _USER32.PostMessageW(wintypes.HWND(self._hwnd), WM_DESTROY, 0, 0)
        thread.join(2.0)
        self._thread = None

    @property
    def alive(self) -> bool:
        return bool(self._hwnd) and not self._stop.is_set()

    # ------------------------------------------------------------------
    # Frames
    # ------------------------------------------------------------------

    def present(self, data_url: str) -> bool:
        """Show one frame. Called from the JS-bridge thread; never blocks on the UI."""
        if not self.alive:
            return False
        try:
            frame = decode_png(data_url, self._grab)
        except Exception as exc:  # a half-written data URL is not worth a crash
            logger.debug("dropped an undecodable pet frame: %s", exc)
            return False
        with self._lock:
            self._frame = frame
        return True

    # ------------------------------------------------------------------
    # Geometry and pointer
    # ------------------------------------------------------------------

    @property
    def hwnd(self) -> int:
        return self._hwnd

    @property
    def rect(self) -> tuple[int, int, int, int]:
        """Where the figure is on the desktop, as ``(x, y, width, height)``."""
        return self._rect

    @property
    def screen_rect(self) -> tuple[int, int, int, int]:
        """The same box as ``(left, top, right, bottom)``, which is what the shell's
        pointer maths and :func:`jarvis.ui.pet._window_rect` both expect.

        Handing ``(x, y, w, h)`` to a caller that subtracts ``right - left`` from it
        is how head tracking was fed a negative width: she kept looking at one corner
        of the screen no matter where the cursor went.
        """
        x, y, width, height = self._rect
        return x, y, x + width, y + height

    @property
    def pressing(self) -> bool:
        """Whether a press is in progress, so the poller must not touch click-through."""
        return self._press is not None

    @property
    def click_through(self) -> bool:
        """Whether the desktop keeps working under her right now."""
        return self._click_through

    def opaque_at(self, x: int, y: int) -> bool:
        """Is the pointer on *her*, in coordinates local to this window?

        A one-pixel test on a wireframe is a test for a line, so a small lattice is
        scanned around the point instead: ``GRAB_SCAN_RADIUS`` at
        ``GRAB_SCAN_STEP`` spacing, which is 64 lookups on a poll running 20 times a
        second. The empty part of the box stays click-through, which is the whole
        promise of this window.
        """
        surface = self._surface
        if surface is None:
            # Nothing has been painted yet, so there is no evidence she is standing
            # here -- and a window that takes every click in a 420x640 box while
            # showing nothing is a hole in the operator's desktop, not a pet.
            return False
        buffer, width, height = surface
        if not (0 <= x < width and 0 <= y < height):
            return False
        radius, step = GRAB_SCAN_RADIUS, GRAB_SCAN_STEP
        for probe_y in range(y - radius, y + radius + 1, step):
            if not 0 <= probe_y < height:
                continue
            row = probe_y * width
            for probe_x in range(x - radius, x + radius + 1, step):
                if not 0 <= probe_x < width:
                    continue
                if buffer[(row + probe_x) * 4 + 3] >= OPAQUE_MIN_ALPHA:
                    return True
        return False

    def set_visible(self, visible: bool) -> None:
        if not self._hwnd:
            return
        # SW_SHOWNOACTIVATE (4), never SW_SHOW (5): the second one activates the window,
        # and an overlay that can be activated is an overlay that can eat the operator's
        # keyboard. Measured on the packaged build -- with SW_SHOW the pet's own window
        # was the foreground window, and typing anywhere on the machine went nowhere.
        _USER32.ShowWindow(wintypes.HWND(self._hwnd), SW_SHOWNOACTIVATE if visible else SW_HIDE)
        if visible:
            # ``SW_SHOW`` does not make a window topmost, and an operator who hid her
            # into the tray and called her back expects her *above* whatever they have
            # opened in the meantime.
            self.raise_topmost()

    def set_grab(self, rect: tuple[float, float, float, float] | None) -> None:
        """Where the handle strip is, as fractions of the window (from the page)."""
        self._grab = rect

    def set_hot_spots(self, buttons: Sequence[BubbleButton]) -> None:
        """Which parts of the current picture answer to a click, in window-local pixels.

        Set alongside every picture, because the layout moves: the same icon sits lower
        on a three-line card than on a one-line one, and a stale rect is a button that
        fires something else.
        """
        self._hot = tuple(buttons)

    def button_at(self, x: int, y: int) -> str | None:
        """The name of the icon under a window-local point, or ``None``."""
        for button in self._hot:
            if button.x <= x < button.x + button.size and button.y <= y < button.y + button.size:
                return button.name
        return None

    def move_to(self, x: int, y: int) -> None:
        """Put this window's top-left at ``(x, y)``, leaving size and z-order alone.

        Written here rather than by a bare ``SetWindowPos`` because ``_paint_pending``
        takes x/y from ``_rect`` when it adopts a new frame: a move that only touched
        the window would be undone by the next paint.
        """
        self._rect = (x, y, self._rect[2], self._rect[3])
        self.raise_topmost(SWP_NOSIZE | SWP_NOACTIVATE, x=x, y=y)

    # ------------------------------------------------------------------
    # The thread
    # ------------------------------------------------------------------

    def _run(self) -> None:  # pragma: no cover - exercised on a real desktop
        try:
            self._build_and_pump()
        except Exception:  # a dead pump thread must not look like a slow one
            logger.exception("the pet compositor died")
            self._ready.set()

    def _build_and_pump(self) -> None:
        # kernel32, not user32: the first version asked the wrong DLL and the
        # thread died on its first line, which is exactly the failure mode the
        # try/except below exists to make loud instead of silent.
        instance = _KERNEL32.GetModuleHandleW(None)
        procedure = ctypes.WINFUNCTYPE(
            ctypes.c_long, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM
        )
        self._proc = procedure(self._window_proc)
        name = self._class_name
        klass = _WndClass(
            CS_HREDRAW | CS_VREDRAW,
            ctypes.cast(self._proc, ctypes.c_void_p),
            0,
            0,
            instance,
            0,
            _USER32.LoadCursorW(0, 32512),  # IDC_ARROW
            0,
            None,
            name,
            0,
        )
        if not _USER32.RegisterClassW(ctypes.byref(klass)):
            error = ctypes.get_last_error()
            if error != 1410:  # ERROR_CLASS_ALREADY_EXISTS: a second pet window
                logger.warning("could not register the compositor window class (%s)", error)
                self._ready.set()
                return
        x, y, width, height = self._rect
        # Two measured constraints fight over this one call, and the resolution is the
        # combination that satisfies both:
        #
        # * ``WS_EX_LAYERED`` and ``WS_EX_TOOLWINDOW`` cannot be set at creation at all
        #   (error 1400, ``ERROR_INVALID_WINDOW_HANDLE``) -- they have to be added to a
        #   window that already exists. ``WS_EX_NOACTIVATE`` *can* be set at creation.
        # * A window that is **not** created visible can never be moved into the topmost
        #   band afterwards: ``SetWindowPos(HWND_TOPMOST)`` returns True and the style
        #   reads back without the bit, every time. Measured four ways on this machine.
        #   And a window created visible *activates itself*, which is how the pet ended
        #   up eating the keyboard.
        #
        # So: create visible with ``WS_EX_NOACTIVATE`` (cannot take the keyboard, can be
        # made topmost) but **off screen**, add the rest of the styles, then move her into
        # place in the same call that puts her in the topmost band. No flash, no stolen
        # focus, and she stays above ordinary windows.
        self._hwnd = int(
            _USER32.CreateWindowExW(
                WS_EX_NOACTIVATE,
                name,
                "小夜",
                WS_POPUP | WS_VISIBLE,
                OFF_SCREEN_PLACEHOLDER,
                OFF_SCREEN_PLACEHOLDER,
                width,
                height,
                0,
                0,
                instance,
                None,
            )
            or 0
        )
        self._ready.set()
        if not self._hwnd:
            logger.warning(
                "CreateWindowEx gave the pet no window (%s)",
                _KERNEL32.GetLastError(),
            )
            return
        self._apply_styles()
        # Into place, into the topmost band, without activating: one call, because the
        # window was created off screen precisely so that no empty rectangle can flash
        # between here and the first frame.
        self.raise_topmost(SWP_NOSIZE | SWP_NOACTIVATE, x=x, y=y)
        message = wintypes.MSG()
        while not self._stop.is_set():
            while _USER32.PeekMessageW(ctypes.byref(message), 0, 0, 0, PM_REMOVE):
                if message.message == WM_QUIT:
                    self._stop.set()
                    break
                _USER32.TranslateMessage(ctypes.byref(message))
                _USER32.DispatchMessageW(ctypes.byref(message))
            # The frame arrives through a shared slot, not a posted message.
            # ``PostThreadMessageW`` reported success while the queue never showed
            # it to us -- a thread message has no window to dispatch to -- and
            # chasing that cost more than this whole file. Polling at 80 Hz is
            # nothing next to the 20 Hz the figure actually paints at.
            self._paint_pending(self._hwnd)
            self._stop.wait(FRAME_POLL_SECONDS)
        self._release_bitmap()
        _USER32.DestroyWindow(wintypes.HWND(self._hwnd))
        self._hwnd = 0

    def _window_proc(
        self, hwnd: int, message: int, word: int, long_: int
    ) -> int:  # pragma: no cover
        if message == WM_MOUSEMOVE:
            self._on_move(long_ & 0xFFFF, long_ >> 16)
            return 0
        if message == WM_LBUTTONDOWN:
            self._press = (long_ & 0xFFFF, long_ >> 16)
            self._moved = False
            _USER32.SetCapture(wintypes.HWND(hwnd))
            return 0
        if message == WM_LBUTTONUP:
            self._release(wintypes.HWND(hwnd))
            return 0
        if message == WM_DESTROY:
            _USER32.PostQuitMessage(0)
            return 0
        return int(_USER32.DefWindowProcW(wintypes.HWND(hwnd), message, word, long_))

    # ------------------------------------------------------------------
    # Pointer behaviour
    # ------------------------------------------------------------------

    def _on_move(self, x: int, y: int) -> None:
        """Follow the press. Whether this window *can* be pressed is decided outside.

        The click-through decision used to live here, and that was the bug: it reads
        ``WM_MOUSEMOVE``, which a ``WS_EX_TRANSPARENT`` window never receives. The
        poller in :mod:`jarvis.ui.pet` owns it now -- see this module's header.
        """
        if self._press is None:
            return
        if abs(x - self._press[0]) > GRAB_SLOP or abs(y - self._press[1]) > GRAB_SLOP:
            self._moved = True
        if not self._moved:
            return
        # The press grabbed the pill, so the pill stays under the cursor.
        self._rect = (
            self._rect[0] + x - self._press[0],
            self._rect[1] + y - self._press[1],
            self._rect[2],
            self._rect[3],
        )
        self.raise_topmost(SWP_NOSIZE | SWP_NOACTIVATE, x=self._rect[0], y=self._rect[1])

    def _over_grab(self, x: int, y: int) -> bool:
        """Was the press *on the handle*, as opposed to anywhere on her?

        Only the handle answers a tap with "收起". Making her whole body answer that
        way would mean every attempt to check whether she had become draggable hid
        her instead.
        """
        grab = self._grab
        if grab is None:
            return False
        return in_rect(grab, x, y, self._rect[2], self._rect[3])

    def _release(self, hwnd: Any) -> None:
        _USER32.ReleaseCapture()
        pressed, dragged = self._press, self._moved
        self._press = None
        self._moved = False
        if pressed is None or self._on_tap is None or dragged:
            return
        # Icons first, then her handle: a card that carries both is rare, and the icon is
        # the smaller target, so it gets the claim on the pixel it was drawn into.
        target = self.button_at(*pressed)
        if target is None and self._over_grab(*pressed):
            target = GRAB_TARGET
        if target is not None:
            self._on_tap(target)

    def set_click_through(self, on: bool) -> bool:
        """Let the desktop keep working, or let her take the pointer. ``True`` = pass through.

        Returns whether the answer actually changed, so the caller can log the flip
        instead of the hundred polls a second that agree with it.
        """
        if not self._hwnd or on == self._click_through:
            return False
        self._click_through = on
        self._apply_styles()
        return True

    def raise_topmost(
        self,
        flags: int = SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE,
        x: int = 0,
        y: int = 0,
    ) -> None:
        """Put her in the topmost band. The style bit is not enough -- measured.

        ``SetWindowLongW(GWL_EXSTYLE, ... | WS_EX_TOPMOST)`` writes 0x8080068 and
        reads back 0x8080060: Windows drops the bit, and the window stays below an
        ordinary one. ``SetWindowPos`` with ``HWND_TOPMOST`` is the call that works,
        and it is also the one that moves her when dragging.
        """
        if not self._hwnd:
            return
        _USER32.SetWindowPos(
            wintypes.HWND(self._hwnd), wintypes.HWND(HWND_TOPMOST), x, y, 0, 0, flags
        )
        self._topmost_at = time.monotonic()

    def keep_topmost(self) -> None:
        """Re-assert the band on the poller's clock: one cheap idempotent call.

        Something else in the topmost group can still be put after her, and waiting
        for that to happen before fixing it means the operator sees her under a
        window they never asked to be above her."""
        if time.monotonic() - self._topmost_at < TOPMOST_REASSERT_SECONDS:
            return
        self.raise_topmost()

    def _apply_styles(self) -> None:
        ex = WS_EX_LAYERED | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE
        if self._click_through:
            ex |= WS_EX_TRANSPARENT
        _USER32.SetWindowLongW(wintypes.HWND(self._hwnd), -20, ex)
        self.raise_topmost()

    # ------------------------------------------------------------------
    # The GDI side
    # ------------------------------------------------------------------

    def _paint_pending(self, hwnd: int) -> None:  # pragma: no cover - needs a real window
        with self._lock:
            frame = self._frame
            self._frame = None
        if frame is None:
            return
        buffer, width, height = frame
        if (width, height) != self._cached_size:
            self._build_bitmap(width, height)
        if not self._bits:
            return
        ctypes.memmove(self._bits, buffer, width * height * 4)
        # The window follows the pixels rather than the other way round:
        # ``UpdateLayeredWindow`` cannot scale, and the canvas is the authority on
        # how big the figure actually is -- it is the one thing both sides can see.
        self._rect = (self._rect[0], self._rect[1], width, height)
        source = wintypes.POINT(0, 0)
        size = wintypes.SIZE(width, height)
        blend = _BlendFunction(AC_SRC_OVER, 0, 255, AC_SRC_ALPHA)
        destination = wintypes.POINT(self._rect[0], self._rect[1])
        screen = _USER32.GetDC(0)
        ok = bool(
            _USER32.UpdateLayeredWindow(
                wintypes.HWND(hwnd),
                screen,
                ctypes.byref(destination),
                ctypes.byref(size),
                wintypes.HDC(self._mem_dc),
                ctypes.byref(source),
                0,
                ctypes.byref(blend),
                ULW_ALPHA,
            )
        )
        _USER32.ReleaseDC(0, screen)
        if ok:
            # Only a frame that actually landed is evidence about where she is: the
            # pointer hit test reads these pixels, and a stale copy would answer for
            # a pose the operator cannot see.
            self._surface = frame
        if not self._painted:
            self._painted = True
            logger.info(
                "first composited pet frame: %sx%s, UpdateLayeredWindow=%s", width, height, ok
            )

    def _build_bitmap(self, width: int, height: int) -> None:  # pragma: no cover
        self._release_bitmap()
        header = _BitmapInfoHeader()
        header.biSize = ctypes.sizeof(_BitmapInfoHeader)
        header.biWidth = width
        # Negative: a top-down bitmap. With the positive height GDI flips the image
        # vertically, and a upside-down figure is not a subtle mistake.
        header.biHeight = -height
        header.biPlanes = 1
        header.biBitCount = 32
        header.biCompression = BI_RGB
        screen = _USER32.GetDC(0)
        self._mem_dc = int(_GDI32.CreateCompatibleDC(wintypes.HDC(screen)) or 0)
        bits = ctypes.c_void_p()
        self._dib = int(
            _GDI32.CreateDIBSection(
                wintypes.HDC(self._mem_dc),
                ctypes.byref(header),
                DIB_RGB_COLORS,
                ctypes.byref(bits),
                None,
                0,
            )
            or 0
        )
        _USER32.ReleaseDC(0, screen)
        if not self._mem_dc or not self._dib:
            logger.warning("the pet compositor could not build a bitmap")
            self._bits = 0
            return
        _GDI32.SelectObject(wintypes.HDC(self._mem_dc), wintypes.HBITMAP(self._dib))
        self._bits = int(bits.value or 0)
        self._cached_size = (width, height)

    def _release_bitmap(self) -> None:  # pragma: no cover
        if self._dib:
            _GDI32.DeleteObject(wintypes.HBITMAP(self._dib))
        if self._mem_dc:
            _GDI32.DeleteDC(wintypes.HDC(self._mem_dc))
        self._dib = 0
        self._mem_dc = 0
        self._bits = 0
        self._cached_size = (0, 0)


def wait_until(predicate: Callable[[], bool], seconds: float) -> bool:
    """Poll ``predicate`` for up to ``seconds``.

    The compositor has no synchronous answer to "did that frame land", and a smoke
    test needs one; the alternative is a test that sleeps a fixed amount and then
    passes for the wrong reason.
    """
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


__all__ = [
    "THINKING_FPS",
    "THINKING_FRAMES",
    "THINKING_TEXT",
    "AlphaWindow",
    "build_bubble",
    "build_thinking_frames",
    "decode_png",
    "in_rect",
    "premultiply",
    "wait_until",
    "wrap_text",
]
