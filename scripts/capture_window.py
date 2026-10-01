"""Capture a window to a PNG, for checking that the HUD actually rendered.

Why this exists: the desktop window once came up as a solid near-black rectangle
and nothing on the Python side noticed — pywebview reported success, the log said
"opening desktop window", and the only error lived in the webview's console. The
check that was missing is "look at the pixels", and it needs a tool.

Why ``PrintWindow`` rather than a screen-region grab: the HUD is a WebView2
control composited by DirectComposition. A region grab only works while the
window is unobscured and on top, which makes it useless for an automated check —
and a check that quietly captures the desktop behind a minimised window is worse
than no check. ``PW_RENDERFULLCONTENT`` (0x2) asks the window to render itself,
composited children included.

Why the DWM frame is used for the crop: ``GetWindowRect`` includes the invisible
resize border (7px left/right, a taller strip at the bottom on Windows 11), so a
naive crop keeps a band of whatever is behind the window. ``PrintWindow`` draws
in window coordinates, so the DWM bounds are converted into an offset into the
captured bitmap rather than applied to the window.

Why the PNG is encoded by hand: Pillow is an optional dependency of the *vision*
extra and is not installed on a plain desktop install. A 30-line encoder keeps
this script dependency-free, which matters for something whose whole job is to
work when other things do not.

Usage::

    python scripts/capture_window.py --title 小夜 --out logs/hud.png
    python scripts/capture_window.py --pid 1234 --out logs/hud.png
"""

from __future__ import annotations

import argparse
import ctypes
import struct
import sys
import time
import zlib
from ctypes import wintypes
from pathlib import Path

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32
dwmapi = ctypes.windll.dwmapi

PW_RENDERFULLCONTENT = 0x00000002
DWMWA_EXTENDED_FRAME_BOUNDS = 9
SW_RESTORE = 9
SW_SHOW = 5

DIB_RGB_COLORS = 0
BI_RGB = 0


class RECT(ctypes.Structure):
    _fields_ = [
        ("left", wintypes.LONG),
        ("top", wintypes.LONG),
        ("right", wintypes.LONG),
        ("bottom", wintypes.LONG),
    ]


class BITMAPINFOHEADER(ctypes.Structure):
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


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 3)]


def _png(width: int, height: int, rgb_rows: list[bytes]) -> bytes:
    """Encode top-down RGB rows as a PNG. Filter type 0 (None) on every row."""

    def chunk(tag: bytes, payload: bytes) -> bytes:
        body = tag + payload
        return struct.pack(">I", len(payload)) + body + struct.pack(">I", zlib.crc32(body))

    raw = b"".join(b"\x00" + row for row in rgb_rows)
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(raw, 6))
        + chunk(b"IEND", b"")
    )


def find_window(*, title: str | None, pid: int | None) -> tuple[int, str]:
    """Find a visible top-level window by title substring or owning process.

    Raises:
        SystemExit: when nothing matches, with the titles that were considered.
    """
    matches: list[tuple[int, str]] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def visit(hwnd: int, _lparam: int) -> bool:
        if not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if length == 0:
            return True
        buffer = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buffer, length + 1)
        caption = buffer.value
        if pid is not None:
            owner = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
            if owner.value != pid:
                return True
        if title is not None and title not in caption:
            return True
        matches.append((hwnd, caption))
        return True

    user32.EnumWindows(visit, 0)
    if not matches:
        raise SystemExit(
            f"没有找到窗口（title={title!r}, pid={pid}）。"
            "确认小夜桌面端正在运行，或用 --list 看有哪些可见窗口。"
        )
    # EnumWindows walks in z-order, and the pet is always-on-top, so a substring
    # search for "小夜" would return the pet forever. An exact caption wins when
    # the caller gave one -- that is the difference between "capture the HUD"
    # and "capture whichever window happens to float above it".
    if title is not None:
        for hwnd, caption in matches:
            if caption == title:
                return hwnd, caption
    return matches[0]


def list_windows() -> int:
    """Print every visible top-level window, for when a search fails."""
    rows: list[str] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def visit(hwnd: int, _lparam: int) -> bool:
        if not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if length == 0:
            return True
        buffer = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buffer, length + 1)
        owner = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        rows.append(f"  0x{hwnd:08X}  pid={owner.value:<8} {buffer.value}")
        return True

    user32.EnumWindows(visit, 0)
    print(f"可见顶层窗口（{len(rows)} 个）：")
    print("\n".join(rows) if rows else "  （无）")
    return 0


def capture(hwnd: int, out: Path) -> tuple[int, int]:
    """Capture ``hwnd`` into ``out``. Returns the written image size.

    Raises:
        SystemExit: if the window has no area or ``PrintWindow`` refuses.
    """
    window_rect = RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(window_rect)):
        raise SystemExit("GetWindowRect 失败")

    # Ask DWM for the visible frame; fall back to the window rect when the
    # attribute is unavailable (an older Windows, or a non-DWM window).
    frame = RECT()
    if (
        dwmapi.DwmGetWindowAttribute(
            hwnd, DWMWA_EXTENDED_FRAME_BOUNDS, ctypes.byref(frame), ctypes.sizeof(frame)
        )
        != 0
    ):
        frame = window_rect

    full_width = window_rect.right - window_rect.left
    full_height = window_rect.bottom - window_rect.top
    if full_width <= 0 or full_height <= 0:
        raise SystemExit(f"窗口尺寸无效：{full_width}x{full_height}")

    # Bring it forward so the compositor has drawn it at least once. Without this
    # a window that has never been shown returns an empty bitmap.
    user32.ShowWindow(hwnd, SW_RESTORE)
    user32.ShowWindow(hwnd, SW_SHOW)
    user32.SetForegroundWindow(hwnd)
    time.sleep(1.2)

    window_dc = user32.GetWindowDC(hwnd)
    memory_dc = gdi32.CreateCompatibleDC(window_dc)
    bitmap = gdi32.CreateCompatibleBitmap(window_dc, full_width, full_height)
    gdi32.SelectObject(memory_dc, bitmap)
    try:
        if not user32.PrintWindow(hwnd, memory_dc, PW_RENDERFULLCONTENT):
            raise SystemExit("PrintWindow 失败（窗口可能拒绝渲染）")

        info = BITMAPINFO()
        info.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        info.bmiHeader.biWidth = full_width
        info.bmiHeader.biHeight = -full_height  # negative = top-down rows
        info.bmiHeader.biPlanes = 1
        info.bmiHeader.biBitCount = 32
        info.bmiHeader.biCompression = BI_RGB

        buffer = ctypes.create_string_buffer(full_width * full_height * 4)
        if not gdi32.GetDIBits(
            memory_dc, bitmap, 0, full_height, buffer, ctypes.byref(info), DIB_RGB_COLORS
        ):
            raise SystemExit("GetDIBits 失败")
    finally:
        gdi32.DeleteObject(bitmap)
        gdi32.DeleteDC(memory_dc)
        user32.ReleaseDC(hwnd, window_dc)

    # Crop to the DWM frame: the difference between the two rects is the invisible
    # resize border that PrintWindow still draws.
    offset_x = max(0, frame.left - window_rect.left)
    offset_y = max(0, frame.top - window_rect.top)
    width = min(frame.right - frame.left, full_width - offset_x)
    height = min(frame.bottom - frame.top, full_height - offset_y)
    if width <= 0 or height <= 0:
        width, height = full_width, full_height
        offset_x = offset_y = 0

    pixels = buffer.raw
    rows: list[bytes] = []
    for y in range(offset_y, offset_y + height):
        start = (y * full_width + offset_x) * 4
        line = pixels[start : start + width * 4]
        # GetDIBits hands back BGRA; PNG wants RGB with no alpha channel.
        rgb = bytearray(width * 3)
        for index in range(width):
            blue, green, red = line[index * 4], line[index * 4 + 1], line[index * 4 + 2]
            rgb[index * 3] = red
            rgb[index * 3 + 1] = green
            rgb[index * 3 + 2] = blue
        rows.append(bytes(rgb))

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(_png(width, height, rows))
    return width, height


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="把小夜的窗口截图存成 PNG")
    parser.add_argument("--title", default="小夜", help="窗口标题的一部分（默认：小夜）")
    parser.add_argument("--pid", type=int, default=None, help="按进程号找窗口")
    parser.add_argument("--out", type=Path, default=Path("logs/hud.png"), help="输出 PNG 路径")
    parser.add_argument("--list", action="store_true", help="列出所有可见顶层窗口后退出")
    args = parser.parse_args(argv)

    if args.list:
        return list_windows()

    if args.pid is None and not args.title:
        parser.error("至少给一个 --title 或 --pid")
    hwnd, caption = find_window(title=None if args.pid else args.title, pid=args.pid)
    width, height = capture(hwnd, args.out)
    print(f"已保存 {args.out}（{width}x{height}，窗口标题 {caption!r}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
