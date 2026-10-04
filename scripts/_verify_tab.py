"""Open the 「自动化」 tab in the running HUD by synthetic clicks, and capture it.

Why a script: this execution environment tears down a background process as soon as
the shell call that started it returns, so "start it, then poke at it in a later
call" cannot work. Everything -- launch, click, capture -- has to happen inside one
call, and that means one program.

The clicks are real input events (``SendInput``), not messages posted at a window:
the HUD is a WebView2 control, and a posted ``WM_LBUTTONDOWN`` never reaches the
page. Coordinates are given in *client* space and converted with ``ClientToScreen``,
so the answer does not depend on where the window happens to sit.
"""

from __future__ import annotations

import ctypes
import os
import subprocess
import sys
import time
from ctypes import wintypes
from pathlib import Path

user32 = ctypes.WinDLL("user32", use_last_error=True)

EXE_DIR = Path(r"E:\BianChengGongJu\JarvisBuild\r28\dist\小夜")
EXE = EXE_DIR / "小夜.exe"
DATA = Path(r"E:\BianChengGongJu\JarvisBuild\r28-verify")
OUT = Path(r"E:\DaiMa\AILiaoTianXiangMu\logs")
CAPTURE = Path(r"E:\DaiMa\AILiaoTianXiangMu\scripts\capture_window.py")

# Client-space coordinates. The script restores the window first (SW_RESTORE) so the
# frame is the remembered 1266x793 instead of a maximised 1920x1032 -- a maximised
# window has no frame, and clicking "the same place" in both states lands on
# different controls.
HELPER_BUTTON = (932, 51)
# The 「自动化」 tab: fourth chip in the popup header. Filled in after the first pass.
AUTOMATION_TAB = (523, 76)

SW_RESTORE = 9


class MOUSEINPUT(ctypes.Structure):
    _fields_ = (
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    )


class INPUT(ctypes.Structure):
    """x64 ``INPUT`` is 40 bytes: DWORD type, 4 bytes of padding, then MOUSEINPUT.

    Hand-padding it to "make it 40" produced 48 instead -- ctypes already aligns the
    union member to 8 -- and ``SendInput`` answers a wrong-sized structure with 0
    rather than an error. That silent 0 is the whole reason this note exists.
    """

    _fields_ = (("type", wintypes.DWORD), ("mi", MOUSEINPUT))


MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_ABSOLUTE = 0x8000
MOUSEEVENTF_MOVE = 0x0001


def find_hud(pid: int) -> int:
    """The HUD's top-level window handle, or 0."""
    found: list[int] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def visit(hwnd: int, _lparam: int) -> bool:
        if not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if length == 0:
            return True
        owner = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value == pid:
            buffer = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buffer, length + 1)
            if buffer.value == "小夜":
                found.append(hwnd)
                return False
        return True

    user32.EnumWindows(visit, 0)
    return found[0] if found else 0


def click(hwnd: int, client_xy: tuple[int, int]) -> None:
    """One real left click at a point given in the window's client space."""
    user32.SetForegroundWindow(hwnd)
    time.sleep(0.4)
    point = wintypes.POINT(client_xy[0], client_xy[1])
    user32.ClientToScreen(hwnd, ctypes.byref(point))
    user32.SetCursorPos(point.x, point.y)
    time.sleep(0.15)
    for flag in (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP):
        event = INPUT(type=0, mi=MOUSEINPUT(0, 0, 0, flag, 0, None))
        sent = user32.SendInput(1, ctypes.byref(event), ctypes.sizeof(INPUT))
        if sent != 1:
            print(f"  !! SendInput 返回 {sent}（0 = 结构体尺寸不对）")
        time.sleep(0.12)


def shot(name: str) -> None:
    """Capture the HUD with the project's own tool."""
    out = OUT / name
    subprocess.run(
        [sys.executable, str(CAPTURE), "--title", "小夜", "--out", str(out)],
        cwd=r"E:\DaiMa\AILiaoTianXiangMu",
        check=False,
    )


def main() -> int:
    print(f"INPUT size = {ctypes.sizeof(INPUT)} (x64 wants 40)")
    env = dict(os.environ)
    env.update(
        {
            "JARVIS_HOME": str(DATA),
            "MODELSCOPE_CACHE": r"E:\BianChengGongJu\JarvisData\models\modelscope",
            "HF_HOME": r"E:\BianChengGongJu\JarvisData\models\huggingface",
            "TORCH_HOME": r"E:\BianChengGongJu\JarvisData\models\torch",
            "WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS": "--no-sandbox",
        }
    )
    lock = DATA / "desktop.lock"
    if lock.exists():
        lock.unlink()

    process = subprocess.Popen([str(EXE)], cwd=str(EXE_DIR), env=env)
    print(f"launched pid={process.pid}")
    try:
        hwnd = 0
        for _ in range(40):
            time.sleep(2)
            hwnd = find_hud(process.pid)
            if hwnd:
                break
        if not hwnd:
            print("!! 没找到窗口")
            return 1
        print(f"window hwnd=0x{hwnd:08X}; restoring it to a known frame")
        user32.ShowWindow(hwnd, SW_RESTORE)
        time.sleep(2)
        print("letting the page settle")
        time.sleep(12)
        shot("r28-tab-0-base.png")

        print(f"clicking 助手 at {HELPER_BUTTON}")
        click(hwnd, HELPER_BUTTON)
        time.sleep(2.0)
        shot("r28-tab-1-assistant.png")

        print(f"clicking 自动化 at {AUTOMATION_TAB}")
        click(hwnd, AUTOMATION_TAB)
        time.sleep(2.0)
        shot("r28-tab-2-automation.png")
        print("done")
        return 0
    finally:
        # The whole tree, not just the launcher: WebView2 runs the page in child
        # processes, and a surviving one keeps the user-data directory locked. The
        # next launch then comes up with a page that cannot reach its bridge --
        # which reads as "the new build is broken" when it is only a leftover.
        import psutil

        try:
            parent = psutil.Process(process.pid)
            for child in parent.children(recursive=True):
                child.kill()
            parent.kill()
        except Exception as exc:
            print(f"  (cleanup: {exc})")
        time.sleep(3)
        inside = [
            p.info["pid"]
            for p in psutil.process_iter(["pid", "exe"])
            if (p.info["exe"] or "").startswith(str(EXE_DIR))
        ]
        print(f"leftover processes under the bundle: {len(inside)}")


if __name__ == "__main__":
    sys.exit(main())
