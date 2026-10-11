"""把文字/图片放进 Windows 剪贴板 —— 页面里点一下"复制"就该有的东西。

为什么不交给浏览器：``navigator.clipboard.write`` 在 pywebview 里要焦点、要权限，
而且**图片根本放不进去**（WebView2 的 ClipboardItem 在这套壳里拿不到 png）。
文字那条页面自己能做（``frontend/src/api/clipboard.ts``），所以这里只兜住两件事：
① 页面那条路失败时的后手；② 图片 —— 那只能从这一侧进。

用 ctypes 而不是 win32clipboard：本项目已经全用 ctypes 碰 Win32（合成器、托盘、
投屏控制条），为一个剪贴板再拉一个 pywin32 依赖不值当。
"""

from __future__ import annotations

import ctypes
import io
from ctypes import wintypes
from pathlib import Path
from typing import Final

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

CF_UNICODETEXT: Final = 13
CF_DIB: Final = 8
GMEM_MOVEABLE: Final = 0x0002

_OpenClipboard = user32.OpenClipboard
_CloseClipboard = user32.CloseClipboard
_EmptyClipboard = user32.EmptyClipboard
_SetClipboardData = user32.SetClipboardData
_GetClipboardData = user32.GetClipboardData
_IsFormatAvailable = user32.IsClipboardFormatAvailable
_OpenClipboard.argtypes = [wintypes.HWND]
_SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
_SetClipboardData.restype = wintypes.HANDLE
_GlobalAlloc = kernel32.GlobalAlloc
_GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
_GlobalAlloc.restype = wintypes.HANDLE
_GlobalLock = kernel32.GlobalLock
_GlobalLock.argtypes = [wintypes.HANDLE]
_GlobalLock.restype = ctypes.c_void_p
_GlobalUnlock = kernel32.GlobalUnlock
_GlobalUnlock.argtypes = [wintypes.HANDLE]


class ClipboardError(RuntimeError):
    """一句能直接显示的话（"剪贴板被别的程序占着"就是这一类）。"""


def _open() -> None:
    if not _OpenClipboard(None):
        raise ClipboardError("剪贴板打不开（多半是别的程序正占着它），稍后再试一次")


def _put(format_id: int, blob: bytes) -> None:
    handle = _GlobalAlloc(GMEM_MOVEABLE, len(blob))
    if not handle:
        raise ClipboardError("剪贴板内存没申请下来")
    try:
        target = _GlobalLock(handle)
        if not target:
            raise ClipboardError("剪贴板内存锁不住")
        try:
            ctypes.memmove(target, blob, len(blob))
        finally:
            _GlobalUnlock(handle)
        if not _SetClipboardData(format_id, handle):
            raise ClipboardError("放进剪贴板那一步没成")
    except Exception:
        kernel32.GlobalFree(handle)
        raise


def copy_text(text: str) -> None:
    body = str(text or "").encode("utf-16-le") + b"\x00\x00"
    _open()
    try:
        if not _EmptyClipboard():
            raise ClipboardError("清空剪贴板没成（可能被别的程序占着）")
        _put(CF_UNICODETEXT, body)
    finally:
        _CloseClipboard()


def copy_image(path: object) -> None:
    """把一张 PNG/JPG 以 DIB 放进剪贴板 —— 粘进微信/画图/PPT 里就是图本身。

    CF_DIB 要的是 ``BITMAPINFOHEADER`` + 自底向上的像素，而 BMP 文件正好是
    "14 字节文件头 + 那一坨"，所以存成 BMP 再切掉头 14 字节就是能用的形状。
    """
    from PIL import Image

    where = Path(str(path or "").strip())
    if not where.is_file():
        raise ClipboardError(f"要复制的图片不在了：{where}")
    try:
        image = Image.open(where).convert("RGB")
    except Exception as exc:
        raise ClipboardError(f"这张图打不开：{exc}") from exc
    buffer = io.BytesIO()
    image.save(buffer, format="BMP")
    blob = buffer.getvalue()[14:]
    _open()
    try:
        if not _EmptyClipboard():
            raise ClipboardError("清空剪贴板没成（可能被别的程序占着）")
        _put(CF_DIB, blob)
    finally:
        _CloseClipboard()


def has_text() -> bool:
    """剪贴板现在是不是有一段文字 —— 复制完自己回读一遍，别只信"我调用了"。"""
    if not _OpenClipboard(None):
        return False
    try:
        return bool(_IsFormatAvailable(CF_UNICODETEXT))
    finally:
        _CloseClipboard()


__all__ = [
    "ClipboardError",
    "copy_image",
    "copy_text",
    "has_text",
]
