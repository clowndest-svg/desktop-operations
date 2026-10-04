"""Launch the packaged HUD and capture it, to check the two-level model pickers render.

Same shape as ``_verify_tab.py`` and for the same reason: this execution environment
tears down a background process as soon as the shell call that started it returns, so
launch + wait + capture has to happen inside one call.

What it is looking for, in pixels: the chat header carrying **two** selects (provider,
then model) plus the two knobs. A `<select>` popup is native, so it is not in the DOM
and synthetic clicks cannot open it -- what this can prove is that the new bundle
renders and that the elements exist at the expected size, which is the half the unit
tests cannot see.
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

EXE_DIR = Path(r"E:\BianChengGongJu\JarvisBuild\r30\dist\小夜")
EXE = EXE_DIR / "小夜.exe"
DATA = Path(r"E:\BianChengGongJu\JarvisData")
OUT = Path(r"E:\DaiMa\AILiaoTianXiangMu\logs")
CAPTURE = Path(r"E:\DaiMa\AILiaoTianXiangMu\scripts\capture_window.py")

SW_RESTORE = 9


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


def shot(name: str) -> None:
    subprocess.run(
        [sys.executable, str(CAPTURE), "--title", "小夜", "--out", str(OUT / name)],
        cwd=r"E:\DaiMa\AILiaoTianXiangMu",
        check=False,
    )


def main() -> int:
    env = dict(os.environ)
    env.update(
        {
            # The REAL data root, so the packaged app reads the migrated config.yaml.
            # That is the acceptance criterion: double-click, and it opens rather than
            # dying on the old single-model field.
            "JARVIS_HOME": str(DATA),
            "MODELSCOPE_CACHE": str(DATA / "models" / "modelscope"),
            "HF_HOME": str(DATA / "models" / "huggingface"),
            "TORCH_HOME": str(DATA / "models" / "torch"),
            "WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS": "--no-sandbox",
        }
    )
    lock = DATA / "desktop.lock"
    if lock.exists():
        lock.unlink()

    log = OUT / "r30-launch.log"
    handle = log.open("w", encoding="utf-8", errors="replace")
    process = subprocess.Popen(
        [str(EXE)], cwd=str(EXE_DIR), env=env, stdout=handle, stderr=subprocess.STDOUT
    )
    print(f"launched pid={process.pid}, log -> {log}")
    try:
        hwnd = 0
        for _ in range(40):
            time.sleep(2)
            hwnd = find_hud(process.pid)
            if hwnd:
                break
        if not hwnd:
            print("!! 没找到窗口（打包产物起不来？看日志）")
            return 1
        print(f"window hwnd=0x{hwnd:08X}")
        user32.ShowWindow(hwnd, SW_RESTORE)
        time.sleep(2)
        print("letting the page settle")
        time.sleep(14)
        shot("r30-1-base.png")
        print("done")
        return 0
    finally:
        import psutil

        try:
            parent = psutil.Process(process.pid)
            for child in parent.children(recursive=True):
                child.kill()
            parent.kill()
        except Exception as exc:
            print(f"  (cleanup: {exc})")
        time.sleep(3)
        handle.close()
        text = log.read_text(encoding="utf-8", errors="replace")
        errors = [line for line in text.splitlines() if "ERROR" in line or "Traceback" in line]
        print(f"log lines with ERROR/Traceback: {len(errors)}")
        for line in errors[:15]:
            print("   ", line)


if __name__ == "__main__":
    sys.exit(main())
