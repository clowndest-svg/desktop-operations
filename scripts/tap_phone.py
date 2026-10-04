"""按屏幕上的字找到那个元素，点它的中心。

为什么要这个脚本：`uiautomator dump` 的 bounds 才是能点的那套坐标，肉眼从截图上估的
不是同一套（估过两次，把"配对"点到了空白处，还点进了用户自己的应用）。
每次点击前重新 dump 一次——界面会随键盘收起、面板滚动整体位移，上一秒的坐标下一秒就错。

用法：python scripts/tap_phone.py "读相册" [设备序列号]
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import time
from xml.etree import ElementTree

# adb 不在 agent 的 PATH 上（每个 shell 都是新开的），写死找得到的那几个位置。
ADB = os.environ.get("ADB") or next(
    (
        candidate
        for candidate in (
            r"D:\Android\Sdk\platform-tools\adb.exe",
            r"C:\Android\Sdk\platform-tools\adb.exe",
        )
        if os.path.isfile(candidate)
    ),
    "adb",
)


def dump(serial: str) -> ElementTree.Element:
    run = subprocess.run(
        [ADB, "-s", serial, "shell", "uiautomator", "dump"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if run.returncode != 0:
        raise SystemExit(f"dump 失败：{run.stdout}{run.stderr}")
    got = subprocess.run(
        [ADB, "-s", serial, "exec-out", "cat", "/sdcard/window_dump.xml"],
        capture_output=True,
        timeout=60,
    )
    return ElementTree.fromstring(got.stdout)


def find(root: ElementTree.Element, wanted: str) -> ElementTree.Element | None:
    best: ElementTree.Element | None = None
    best_len = -1
    for node in root.iter("node"):
        text = (node.get("text") or "") + (node.get("content-desc") or "")
        # 取最里层的那个：外层容器也"包含"这段文字，点它会点到别处。
        if wanted in text and (best is None or len(text) < best_len):
            best, best_len = node, len(text)
    return best


def bounds(node: ElementTree.Element) -> tuple[int, int]:
    match = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", node.get("bounds") or "")
    if not match:
        raise SystemExit("这个节点没有 bounds")
    left, top, right, bottom = (int(part) for part in match.groups())
    return (left + right) // 2, (top + bottom) // 2


def tap(serial: str, wanted: str, settle: float = 1.2) -> bool:
    node = find(dump(serial), wanted)
    if node is None:
        print(f"没找到「{wanted}」")
        return False
    x, y = bounds(node)
    subprocess.run(
        [ADB, "-s", serial, "shell", "input", "tap", str(x), str(y)],
        capture_output=True,
        timeout=60,
    )
    time.sleep(settle)
    print(f"点了「{wanted}」@ {x},{y}")
    return True


def texts(serial: str, limit: int = 40) -> list[str]:
    rows: list[str] = []
    for node in dump(serial).iter("node"):
        text = (node.get("text") or "").strip()
        if text:
            rows.append(text)
    return rows[-limit:]


def main() -> int:
    serial = sys.argv[2] if len(sys.argv) > 2 else "10AE1F1RQE003N3"
    if sys.argv[1] == "texts":
        for line in texts(serial):
            print(line[:120])
        return 0
    ok = tap(serial, sys.argv[1])
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
