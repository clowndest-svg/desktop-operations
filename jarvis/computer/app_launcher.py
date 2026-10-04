"""按名字找程序：从「微信」两个字到一条真能启动的路径。

为什么单独一个模块：模型看不见屏幕，"打开某个程序"是它少数能真做成的事之一 —— 但这件小事
在真实机器上会从三个地方一起失败。本机实测（用户报的正是这个）：程序换过名（微信 4.x 是
``Weixin.exe``，不是 ``WeChat.exe``）、装在非标准盘（``D:\\RuanJian\\微信\\Weixin\\Weixin.exe``）、
注册表 ``App Paths`` 里根本没有它。一个只会翻 ``Program Files\\Tencent`` 的手写脚本必然空手而归，
然后告诉用户"这台电脑上找不到微信" —— 而它当时就开着。

这里按**人怎么找**的顺序查，每一处都留下"在哪找到的"：

1. **正在运行的进程**（含主窗口标题）—— 已经在跑就不该再启动一个；
2. **开始菜单快捷方式**（``.lnk``）—— 安装器自己放的那一份，最接近用户脑子里的名字；
3. **注册表 App Paths** —— 管的是 PATH 型启动，没有就得往下走；
4. **卸载记录的 InstallLocation** —— 非标准盘的安装（微信在 D 盘就是靠这条找到）；
5. **``%LOCALAPPDATA%\\Programs``** —— 用户级安装（VS Code 那类）；
6. **PATH** —— 命令行程序。

找不到就明确说找不到，并列出查过哪些地方；找到多个同等匹配就把它们都报出来。
全程只读；启动是另一个函数，且只接一个已经解析好的路径。
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import logging
import os
import shutil
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from jarvis.core.exceptions import ToolError

logger = logging.getLogger("jarvis.computer.app_launcher")

try:  # pragma: no cover - platform specific
    import winreg as _winreg_module
except ImportError:  # pragma: no cover - non-Windows
    _winreg_module = None  # type: ignore[assignment]

# Any 化一次: mypy 在 win32 上认定 winreg 一定在, 于是 "is None" 那条分支会被判成
# 不可达; 而它在别的平台上确实存在, 运行时的兜底要留着.
winreg: Any = _winreg_module


def _user32() -> Any:
    """user32, or ``None`` where there is no such library."""
    return getattr(getattr(ctypes, "windll", None), "user32", None)


LAUNCHABLE_SUFFIXES: Final[tuple[str, ...]] = (".exe", ".lnk")

_UNINSTALLER_HINTS: Final[tuple[str, ...]] = (
    "unins",
    "uninstall",
    "setup",
    "update",
    "updater",
    "installer",
    "repair",
    "helper",
    "crash",
)

_SOURCE_RUNNING: Final[str] = "正在运行"
_SOURCE_START_MENU: Final[str] = "开始菜单"
_SOURCE_APP_PATHS: Final[str] = "注册表 App Paths"
_SOURCE_INSTALLED: Final[str] = "卸载记录里的安装位置"
_SOURCE_USER_PROGRAMS: Final[str] = "用户程序目录"
_SOURCE_PATH: Final[str] = "PATH"
_SOURCE_BUILTIN: Final[str] = "系统自带"

ALL_SOURCES: Final[tuple[str, ...]] = (
    _SOURCE_RUNNING,
    _SOURCE_START_MENU,
    _SOURCE_APP_PATHS,
    _SOURCE_INSTALLED,
    _SOURCE_USER_PROGRAMS,
    _SOURCE_PATH,
    _SOURCE_BUILTIN,
)

BUILTIN_APPS: Final[dict[str, str]] = {
    # Windows' own apps have no .lnk to find and no App Paths entry, and their
    # Chinese names appear nowhere on disk -- 记事本 is not a file. A short table is
    # the honest fix; everything else has to be found by name.
    "计算器": "calc.exe",
    "calculator": "calc.exe",
    "记事本": "notepad.exe",
    "notepad": "notepad.exe",
    "画图": "mspaint.exe",
    "paint": "mspaint.exe",
    "任务管理器": "taskmgr.exe",
    "task manager": "taskmgr.exe",
    "资源管理器": "explorer.exe",
    "文件管理器": "explorer.exe",
    "命令提示符": "cmd.exe",
    "终端": "wt.exe",
    "windows terminal": "wt.exe",
    "注册表编辑器": "regedit.exe",
    "注册表": "regedit.exe",
    "控制面板": "control.exe",
    "截图": "snippingtool.exe",
    "任务计划": "taskschd.msc",
    "服务": "services.msc",
    "事件查看器": "eventvwr.msc",
    "设备管理器": "devmgmt.msc",
    "磁盘管理": "diskmgmt.msc",
}


@dataclass(frozen=True, slots=True)
class AppCandidate:
    """One way to start (or reach) the program the user named."""

    source: str
    name: str
    """What it is called where it was found -- the shortcut's filename, the
    registry's DisplayName, the process's name."""

    target: str
    """What to hand the shell: an ``.exe`` or an ``.lnk``. Empty for a running app
    whose executable path could not be read (permissions) -- then ``pid`` is the
    whole answer."""

    running: bool = False
    pid: int = 0
    window_title: str = ""
    note: str = ""

    @property
    def is_running(self) -> bool:
        return self.running and self.pid > 0

    def label(self) -> str:
        return self.window_title or self.name


def _normalise(text: str) -> str:
    """Lowercase, drop the punctuation installers like, collapse spaces."""
    out = []
    for char in text.strip().lower():
        out.append(" " if char in "·_-–—()（）[]【】" else char)
    return " ".join("".join(out).split())


def _matches(query: str, *names: str) -> bool:
    """Whether ``query`` names any of ``names``, in the forward direction only.

    Forward (the query is inside the candidate) and never the reverse: a query like
    "不存在的程序xyz" must not match a Chrome app shortcut called "X" just because
    the letter is in there somewhere. The mismatch between a Chinese query and an
    English exe name (微信 / Weixin.exe) is handled where it belongs -- by the
    window title, the install record's DisplayName, and :data:`BUILTIN_APPS`.
    """
    wanted = _normalise(query)
    if not wanted:
        return False
    return any(wanted in _normalise(name) for name in names if name)


def _score(query: str, *names: str) -> int:
    """How good the fit is: exact stem (3) > prefix (2) > substring (1) > no (0)."""
    wanted = _normalise(query)
    best = 0
    for name in names:
        candidate = _normalise(name).removesuffix(".exe").removesuffix(".lnk")
        if not candidate:
            continue
        if candidate == wanted:
            return 3
        if candidate.startswith(wanted) or wanted.startswith(candidate):
            best = max(best, 2)
        elif wanted in candidate:
            best = max(best, 1)
    return best


# -- 1. what is already running -------------------------------------------


def _window_titles_by_pid() -> dict[int, tuple[str, bool]]:
    """Titled top-level windows, keyed by pid: ``(title, currently visible)``.

    Invisible windows are kept, not skipped, because that is exactly the state of an
    app the user "closed" to the tray: 微信 4.x hides its ``微信`` window when you
    close it, so a visible-only scan answers "there is no WeChat window" about a
    program that is running and can be brought straight back. The flag travels with
    the title so the caller can say which of the two states it is.
    """
    titles: dict[int, tuple[str, bool]] = {}
    user32 = _user32()
    if user32 is None:  # pragma: no cover - non-Windows
        return titles
    user32.GetWindowTextW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
    user32.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]

    def visit(hwnd: int, _lparam: int) -> bool:
        title = ctypes.create_unicode_buffer(256)
        user32.GetWindowTextW(hwnd, title, 256)
        text = title.value.strip()
        if not text or text in ("MSCTFIME UI", "Default IME"):
            return True
        pid = wt.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        owner = int(pid.value)
        visible = bool(user32.IsWindowVisible(hwnd))
        existing = titles.get(owner)
        # Prefer a visible window; otherwise the first titled one we saw.
        if existing is None or (visible and not existing[1]):
            titles[owner] = (text, visible)
        return True

    try:
        user32.EnumWindows(ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)(visit), 0)
    except Exception:  # pragma: no cover - a title we cannot read is not fatal
        logger.debug("窗口标题枚举失败", exc_info=True)
    return titles


def _running_candidates() -> list[AppCandidate]:
    try:
        import psutil
    except ImportError:  # pragma: no cover - psutil is a hard dependency elsewhere
        return []
    titles = _window_titles_by_pid()
    by_path: dict[str, AppCandidate] = {}
    try:
        processes = list(psutil.process_iter(["pid", "name", "exe"]))
    except Exception:  # pragma: no cover - platform specific
        return []
    for proc in processes:
        info = getattr(proc, "info", {}) or {}
        pid = int(info.get("pid") or 0)
        name = str(info.get("name") or "")
        if not pid or not name:
            continue
        try:
            target = str(info.get("exe") or proc.exe() or "")
        except Exception:
            target = ""
        title, visible = titles.get(pid, ("", False))
        key = (target or f"{name}:{pid}").lower()
        existing = by_path.get(key)
        has_window = bool(title)
        if existing is not None and not (has_window and not existing.window_title):
            continue
        note = "已经在运行" if visible else "已经在运行（窗口收在托盘里）"
        by_path[key] = AppCandidate(
            source=_SOURCE_RUNNING,
            name=name,
            target=target,
            running=True,
            pid=pid,
            window_title=title,
            note=note,
        )
    return list(by_path.values())


# -- 2. the Start Menu ----------------------------------------------------


def _start_menu_roots() -> list[Path]:
    roots: list[Path] = []
    appdata = os.environ.get("APPDATA")
    programdata = os.environ.get("PROGRAMDATA")
    if appdata:
        roots.append(Path(appdata) / "Microsoft/Windows/Start Menu/Programs")
    if programdata:
        roots.append(Path(programdata) / "Microsoft/Windows/Start Menu/Programs")
    return roots


def _start_menu_candidates() -> list[AppCandidate]:
    found: list[AppCandidate] = []
    seen: set[str] = set()
    for root in _start_menu_roots():
        if not root.is_dir():
            continue
        try:
            entries: Iterator[Path] = root.rglob("*.lnk")
            for link in entries:
                name = link.stem
                if name.startswith("卸载"):  # 卸载微信.lnk 是卸载器, 不是程序
                    continue
                key = name.lower()
                if key in seen:
                    continue
                seen.add(key)
                found.append(
                    AppCandidate(
                        source=_SOURCE_START_MENU,
                        name=name,
                        target=str(link),
                        note=str(link),
                    )
                )
        except OSError:  # pragma: no cover - unreadable menu folder
            continue
    return found


# -- 3/4/5/6. registry and directories ------------------------------------


def _iter_registry_subkeys(root: Any, path: str) -> Iterator[tuple[str, Any]]:
    if winreg is None:  # pragma: no cover - non-Windows
        return
    for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
        try:
            with winreg.OpenKey(root, path, 0, winreg.KEY_READ | view) as handle:
                index = 0
                while True:
                    try:
                        subkey = winreg.EnumKey(handle, index)
                    except OSError:
                        break
                    index += 1
                    try:
                        yield subkey, winreg.OpenKey(handle, subkey, 0, winreg.KEY_READ | view)
                    except OSError:
                        continue
        except OSError:
            continue


def _app_paths_candidates() -> list[AppCandidate]:
    if winreg is None:  # pragma: no cover - non-Windows
        return []
    base = r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths"
    found: list[AppCandidate] = []
    for root in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        for name, handle in _iter_registry_subkeys(root, base):
            with handle:
                try:
                    target, _kind = winreg.QueryValueEx(handle, "")
                except OSError:
                    continue
                if not isinstance(target, str) or not target:
                    continue
                found.append(
                    AppCandidate(
                        source=_SOURCE_APP_PATHS,
                        name=name.removesuffix(".exe"),
                        target=target,
                        note=f"HKEY {'HKLM' if root == winreg.HKEY_LOCAL_MACHINE else 'HKCU'}",
                    )
                )
    return found


def _executables_under(folder: Path, *, depth: int = 1) -> list[Path]:
    """Executables near the top of an install folder -- not the whole tree."""
    out: list[Path] = []
    if not folder.is_dir():
        return out
    try:
        for entry in folder.iterdir():
            if entry.is_file() and entry.suffix.lower() in LAUNCHABLE_SUFFIXES:
                out.append(entry)
            elif entry.is_dir() and depth > 0:
                try:
                    for inner in entry.iterdir():
                        if inner.is_file() and inner.suffix.lower() in LAUNCHABLE_SUFFIXES:
                            out.append(inner)
                except OSError:
                    continue
    except OSError:
        return out
    return out


def _looks_like_uninstaller(path: Path) -> bool:
    stem = path.stem.lower()
    return any(hint in stem for hint in _UNINSTALLER_HINTS)


def _pick_executable(folder: Path, query: str, display: str) -> Path | None:
    """Which exe in an install folder is 'the program'.

    Real installs make this easy or impossible, so the rules are ordered: a name
    that matches the query or the DisplayName wins; then an exe named after its own
    folder (微信 4.x 装在 ``\\Weixin\\`` 里，跑的是 ``Weixin.exe``，而 DisplayName 是
    「微信」—— 两条都对不上一个字符串，但目录名对得上); then the only exe left.
    """
    candidates = [path for path in _executables_under(folder) if not _looks_like_uninstaller(path)]
    if not candidates:
        return None
    for path in candidates:
        if _score(query, path.stem) or _score(display, path.stem):
            return path
    folder_name = folder.name
    for path in candidates:
        if _normalise(path.stem) == _normalise(folder_name):
            return path
    real = [path for path in candidates if path.suffix.lower() == ".exe"]
    return real[0] if len(real) == 1 else None


def _install_location_candidates() -> list[AppCandidate]:
    if winreg is None:  # pragma: no cover - non-Windows
        return []
    base = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"
    found: list[AppCandidate] = []
    for root in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        for _key, handle in _iter_registry_subkeys(root, base):
            with handle:
                # 不写成闭包: 循环变量 handle 会被下一个条目覆盖, 而这里只想读两次
                try:
                    raw_display, _kind = winreg.QueryValueEx(handle, "DisplayName")
                except OSError:
                    continue
                try:
                    raw_location, _kind = winreg.QueryValueEx(handle, "InstallLocation")
                except OSError:
                    raw_location = ""
                display = raw_display if isinstance(raw_display, str) else ""
                location = raw_location if isinstance(raw_location, str) else ""
                if not display:
                    continue
                found.append(
                    AppCandidate(
                        source=_SOURCE_INSTALLED,
                        name=display,
                        target="",
                        note=location,
                    )
                )
    return found


def _user_program_candidates() -> list[AppCandidate]:
    local = os.environ.get("LOCALAPPDATA")
    if not local:
        return []
    root = Path(local) / "Programs"
    found: list[AppCandidate] = []
    if not root.is_dir():
        return found
    try:
        folders = [entry for entry in root.iterdir() if entry.is_dir()]
    except OSError:  # pragma: no cover - unreadable
        return found
    for folder in folders:
        for path in _executables_under(folder):
            if _looks_like_uninstaller(path):
                continue
            found.append(
                AppCandidate(
                    source=_SOURCE_USER_PROGRAMS,
                    name=path.stem,
                    target=str(path),
                    note=str(folder),
                )
            )
    return found


def _path_hits() -> list[AppCandidate]:
    """Every executable on PATH, so the query can be matched like the rest."""
    found: list[AppCandidate] = []
    seen: set[str] = set()
    for folder in os.environ.get("PATH", "").split(os.pathsep):
        if not folder:
            continue
        try:
            entries = list(os.scandir(folder))
        except OSError:
            continue
        for entry in entries:
            name = entry.name
            lower = name.lower()
            if not lower.endswith(".exe") or lower in seen:
                continue
            seen.add(lower)
            found.append(
                AppCandidate(
                    source=_SOURCE_PATH,
                    name=name.removesuffix(".exe"),
                    target=entry.path,
                    note="PATH 里",
                )
            )
    return found


def _system_alias(query: str) -> AppCandidate | None:
    """Windows' own apps, which no file on disk is named after."""
    target = BUILTIN_APPS.get(_normalise(query).replace(" ", ""))
    if not target and _normalise(query) in BUILTIN_APPS:
        target = BUILTIN_APPS[_normalise(query)]
    if not target:
        return None
    resolved = shutil.which(target)
    if not resolved:
        system_root = os.environ.get("SYSTEMROOT", r"C:\Windows")
        guess = Path(system_root) / "System32" / target
        resolved = str(guess) if guess.exists() else ""
    if not resolved:
        return None
    return AppCandidate(
        source=_SOURCE_BUILTIN,
        name=query.strip(),
        target=resolved,
        note="Windows 自带",
    )


def find_apps(query: str, *, limit: int = 5) -> tuple[tuple[AppCandidate, ...], tuple[str, ...]]:
    """Candidates for ``query``, best first, plus the sources that were consulted.

    The sources are returned rather than logged because "找不到" is only half an
    answer: the useful half is where it looked, and the model is the one that has to
    say it out loud.
    """
    wanted = query.strip()
    if not wanted:
        return (), ()
    pool: list[AppCandidate] = []
    alias = _system_alias(wanted)
    if alias is not None:
        pool.append(alias)
    pool.extend(_running_candidates())
    pool.extend(_start_menu_candidates())
    pool.extend(_app_paths_candidates())
    pool.extend(_install_location_candidates())
    pool.extend(_user_program_candidates())
    pool.extend(_path_hits())

    scored: list[tuple[int, int, AppCandidate]] = []
    for candidate in pool:
        score = _score(wanted, candidate.name)
        if candidate.window_title:
            score = max(score, _score(wanted, candidate.window_title))
        if not score:
            continue
        if candidate.source == _SOURCE_INSTALLED and not candidate.target:
            folder = Path(candidate.note) if candidate.note else None
            picked = _pick_executable(folder, wanted, candidate.name) if folder else None
            if picked is None:
                continue
            candidate = AppCandidate(
                source=candidate.source,
                name=candidate.name,
                target=str(picked),
                note=candidate.note,
            )
        # Running first, then how good the fit is, then a stable tiebreak.
        running_rank = 0 if candidate.running else 1
        scored.append((running_rank, -score, candidate))
    scored.sort(key=lambda row: (row[0], row[1], row[2].name.lower()))

    return tuple(row[2] for row in scored[:limit]), ALL_SOURCES


def launch_target(target: str) -> None:
    """Start an ``.exe`` or ``.lnk`` through the shell. No arguments, ever.

    ``os.startfile`` is ShellExecute: it resolves a shortcut the same way a
    double-click does, without this module ever parsing the ``.lnk`` format or
    building a command line. The signature takes a path and nothing else on purpose
    -- an argument here would be a shell-string injection waiting to happen.
    """
    path = Path(target)
    if not path.exists():
        raise ToolError(f"要启动的东西不在了：{target}")
    if path.suffix.lower() not in LAUNCHABLE_SUFFIXES:
        raise ToolError(f"只启动 .exe 或 .lnk，不接：{target}")
    try:
        os.startfile(str(path))  # ShellExecute, not a shell string
    except OSError as exc:
        raise ToolError(f"启动失败：{exc}", details={"target": target}) from exc


def bring_to_front(pid: int) -> bool:
    """Try to put ``pid``'s main window in front, showing it first if it is hidden.

    Windows only lets the foreground process hand the foreground on, so this can
    legitimately fail from a background app -- the caller reports that instead of
    claiming the window came up. ``SW_SHOWNORMAL`` before the raise is what makes
    "打开微信" work on a client that is sitting in the tray: its window still exists,
    it is just not visible.
    """
    user32 = _user32()
    if user32 is None or pid <= 0:  # pragma: no cover - non-Windows
        return False
    match: list[int] = []

    def visit(hwnd: int, _lparam: int) -> bool:
        owner = wt.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if int(owner.value) != pid:
            return True
        title = ctypes.create_unicode_buffer(256)
        user32.GetWindowTextW(hwnd, title, 256)
        text = title.value.strip()
        if not text or text in ("MSCTFIME UI", "Default IME"):
            return True
        rect = wt.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        if rect.right - rect.left < 200 or rect.bottom - rect.top < 200:
            return True  # 托盘图标那类小窗, 不是主窗
        match.append(int(hwnd))
        return True

    try:
        user32.EnumWindows(ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)(visit), 0)
    except Exception:  # pragma: no cover - platform specific
        return False
    if not match:
        return False
    hwnd = match[0]
    try:
        user32.ShowWindow(wt.HWND(hwnd), 1)  # SW_SHOWNORMAL: 显示 + 还原
        return bool(user32.SetForegroundWindow(wt.HWND(hwnd)))
    except Exception:  # pragma: no cover - platform specific
        return False


__all__ = [
    "ALL_SOURCES",
    "AppCandidate",
    "bring_to_front",
    "find_apps",
    "launch_target",
]
