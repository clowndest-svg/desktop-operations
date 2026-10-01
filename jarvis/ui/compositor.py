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
mouse event. The one thing on the pet that takes a click is the handle strip, and the
page can only say where it drew it. So the hit test happens here, against the
rectangle the page reported: outside it the window is ``WS_EX_TRANSPARENT`` and the
desktop keeps working; inside it the window takes the press, drags itself, and treats
a press-and-release without movement as "收起".

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
import threading
import time
from collections.abc import Callable
from ctypes import wintypes
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
SWP_NOACTIVATE = 0x0010
HWND_TOPMOST = -1

GRAB_SLOP = 6
"""Pixels of movement that still count as a click rather than a drag.

A person pressing and releasing on a 60-pixel-wide pill moves a couple of pixels;
without this the tap would become a one-pixel drag and the pet would not hide.
"""


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
_USER32: Any = getattr(ctypes.windll, "user32", None)
_KERNEL32: Any = getattr(ctypes.windll, "kernel32", None)
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


def _build_pill(box: tuple[int, int, int, int]) -> Any:
    """Render the label once, at the rectangle the page reported."""
    from PIL import Image, ImageDraw, ImageFont

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
    font = None
    for path in FONT_CANDIDATES:
        try:
            font = ImageFont.truetype(path, max(12, radius))
            break
        except OSError:
            continue
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


_PILL_CACHE: dict[tuple[object, ...], Any] = {}
"""The pill, drawn once per size and pasted onto every frame after that.

Text shaping at 20 frames a second is CPU spent re-laying-out the same four
words; ``alpha_composite`` is a single C-level blend.
"""


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
    data_url: str, grab: tuple[float, float, float, float] | None = None
) -> tuple[bytes, int, int]:
    """``data:image/png;base64,...`` → ``(premultiplied bytes, width, height)``.

    ``grab`` is the handle rectangle, drawn while the pixels are still a plain
    RGBA image: after ``premultiply`` the buffer is opaque bytes and GDI's, and
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

    def __init__(self, *, on_tap: Callable[[], object] | None = None) -> None:
        self._hwnd = 0
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._frame: tuple[bytes, int, int] | None = None
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
    def rect(self) -> tuple[int, int, int, int]:
        """Where the figure is on the desktop, in device pixels."""
        return self._rect

    def set_visible(self, visible: bool) -> None:
        if self._hwnd:
            _USER32.ShowWindow(wintypes.HWND(self._hwnd), 5 if visible else 0)

    def set_grab(self, rect: tuple[float, float, float, float] | None) -> None:
        """Where the handle strip is, as fractions of the window (from the page)."""
        self._grab = rect

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
        name = "XiaoYePetCompositor"
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
        # Measured on this machine: this window is created with *no* extended styles at
        # all. Asking for WS_EX_LAYERED (or the topmost/tool-window combination) at
        # creation fails with 1400 -- ``ERROR_INVALID_WINDOW_HANDLE``, which says
        # nothing about what is wrong -- while setting the same styles on a window that
        # already exists works every time. So the styles arrive below, from
        # ``_apply_click_through``, which was already the one place that owns them.
        ex = 0
        x, y, width, height = self._rect
        self._hwnd = int(
            _USER32.CreateWindowExW(
                ex,
                name,
                "小夜",
                WS_POPUP | WS_VISIBLE,
                x,
                y,
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
        self._apply_click_through()
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
        if self._press is not None:
            if abs(x - self._press[0]) > GRAB_SLOP or abs(y - self._press[1]) > GRAB_SLOP:
                self._moved = True
            if self._moved:
                # The press grabbed the pill, so the pill stays under the cursor.
                self._rect = (
                    self._rect[0] + x - self._press[0],
                    self._rect[1] + y - self._press[1],
                    self._rect[2],
                    self._rect[3],
                )
                _USER32.SetWindowPos(
                    wintypes.HWND(self._hwnd),
                    wintypes.HWND(HWND_TOPMOST),
                    self._rect[0],
                    self._rect[1],
                    0,
                    0,
                    SWP_NOACTIVATE | SWP_NOSIZE,
                )
            return
        over = self._over_grab(x, y)
        if over != (not self._click_through):
            self._click_through = not over
            self._apply_click_through()

    def _over_grab(self, x: int, y: int) -> bool:
        grab = self._grab
        if grab is None:
            return False
        return in_rect(grab, x, y, self._rect[2], self._rect[3])

    def _release(self, hwnd: Any) -> None:
        _USER32.ReleaseCapture()
        pressed, dragged = self._press, self._moved
        self._press = None
        self._moved = False
        if (
            pressed is not None
            and self._on_tap is not None
            and not dragged
            and self._over_grab(*pressed)
        ):
            self._on_tap()

    def _apply_click_through(self) -> None:
        ex = WS_EX_LAYERED | WS_EX_TOPMOST | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE
        if self._click_through:
            ex |= WS_EX_TRANSPARENT
        _USER32.SetWindowLongW(wintypes.HWND(self._hwnd), -20, ex)

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


__all__ = ["AlphaWindow", "decode_png", "in_rect", "premultiply", "wait_until"]
