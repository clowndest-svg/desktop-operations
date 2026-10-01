# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the 小夜 desktop HUD (onedir).

Build:
    .venv/Scripts/python.exe -m PyInstaller --noconfirm packaging/jarvis.spec

Why onedir and not onefile
-------------------------
Onefile unpacks the whole bundle into a fresh ``%TEMP%\\\\_MEIxxxx`` on every
launch -- here 1.5-2 GB, every time, and the directory it lands in is exactly the
one this application offers to clean up. onedir starts in about a second and keeps
the payload where the operator can see and delete it.

Why the data files are listed by hand
-------------------------------------
A frozen app has no ``package-data``: ``pyproject.toml`` only shapes wheels. The
two things the code reads through ``importlib.resources`` / the filesystem --
``defaults.yaml`` and the built Vue bundle -- have to be carried here or the
window opens onto nothing.
"""

import importlib.util
import json
import os
import time

from PyInstaller.utils.hooks import (
    collect_data_files,
    collect_dynamic_libs,
    collect_submodules,
    copy_metadata,
)

# PyInstaller resolves a spec's relative paths against the spec file's own
# directory, so "packaging/entry.py" inside this file means packaging/packaging/.
# Everything below is anchored to the repository root instead, which also makes
# the spec runnable from any working directory.
ROOT = os.path.dirname(os.path.abspath(SPECPATH))
ENTRY = os.path.join(ROOT, "packaging", "entry.py")

hiddenimports = [
    # The shell. pywebview picks its backend at runtime from the gui= argument,
    # so static analysis cannot see that Windows needs these.
    "webview",
    "webview.platforms.winforms",
    "webview.platforms.edgechromium",
    "webview.js",
    "webview._version",
    # Telemetry and the disk sweep.
    "psutil",
    "psutil._pswindows",
    # The notification-area icon. pystray chooses its backend by ``sys.platform``
    # inside a function call, so the frozen build sees "import pystray" and nothing
    # else; Pillow is then reached for by pystray's own win32 code. Naming both here
    # is the version of "the tray worked when I ran it from source" that survives.
    # Desktop control. pyautogui picks its platform modules at runtime and there is
    # no hook for it in hooks-contrib, so a frozen build sees "import pyautogui" and
    # nothing behind it -- the same silent no-op the source install had until the
    # dependency itself went missing.
    "pyautogui",
    "pygetwindow",
    "pyscreeze",
    "pymsgbox",
    "pyrect",
    "pytweening",
    "mouseinfo",
    "pystray",
    "pystray._win32",
    "pystray._util.win32",
    "PIL",
    "PIL.Image",
    "PIL.ImageDraw",
    # Voice stack: every one of these is imported lazily inside an engine's
    # __init__ or start(), which is exactly where PyInstaller goes blind.
    "sounddevice",
    "soundfile",
    "numpy",
    "torch",
    "edge_tts",
    "funasr",
    "funasr.auto.auto_model",
    "modelscope",
    "silero_vad",
    "jarvis.asr.engines",
    "jarvis.tts.engines",
    "jarvis.vad.engines",
    "jarvis.wakeword.engines",
    "jarvis.wakeword.asr_engine",
    "jarvis.orchestration.graph",
    "jarvis.orchestration.player",
    "jarvis.orchestration.voice_pipeline",
    "jarvis.agent.conversational",
    "jarvis.agent.tools",
    "jarvis.app.voice_service",
    "jarvis.app.chat_service",
    "jarvis.ui.desktop",
    "jarvis.ui.pump",
    "jarvis.ui.static_server",
    "jarvis.ui.state_bridge",
    # Phase 11-18 capability packages. All of them are reached through the
    # composition root, which PyInstaller's static analysis does see, but the
    # ones below pull their engine in through a factory or a lazy import.
    "jarvis.database.store",
    "jarvis.database.migrations",
    "jarvis.vector.embedder",
    "jarvis.vector.store",
    "jarvis.memory.extractor",
    "jarvis.knowledge.loader",
    "jarvis.knowledge.chunker",
    "jarvis.tools.registry",
    "jarvis.tools.policy",
    "jarvis.tools.safe_eval",
    "jarvis.tools.builtins",
    "jarvis.planner.decomposer",
    "jarvis.prompt.templates",
    "jarvis.plugins.loader",
    "jarvis.workflow.loader",
    "jarvis.workflow.conditions",
    "jarvis.ocr.engines",
    "jarvis.vision.capture",
    "jarvis.browser.engine",
    "jarvis.computer.controller",
    # APScheduler is a base dependency now (the scheduler is on by default) and
    # it resolves its triggers and executors by name inside start(), which is
    # precisely where a frozen build goes blind. Listing them is cheap.
    "apscheduler.schedulers.background",
    "apscheduler.triggers.cron",
    "apscheduler.triggers.interval",
    "apscheduler.executors.pool",
    "apscheduler.jobstores.memory",
]

# The window's title line says `v0.1.0 · pywebview` for every build ever made, and
# that is how an operator ends up demoing a three-old artifact believing it is the
# new one: five `小夜.exe` on one disk, pixel-identical chrome, and the only thing
# missing from the old one is the feature being asked about. So each build writes
# its own timestamp into the bundle and the HUD shows it.
_buildinfo_dir = os.path.join(ROOT, "build")
os.makedirs(_buildinfo_dir, exist_ok=True)
_buildinfo_file = os.path.join(_buildinfo_dir, "buildinfo.json")
with open(_buildinfo_file, "w", encoding="utf-8") as _handle:
    json.dump({"built_at": time.strftime("%Y-%m-%d %H:%M")}, _handle)

datas = [
    (os.path.join(ROOT, "jarvis", "config", "defaults.yaml"), "jarvis/config"),
    (os.path.join(ROOT, "jarvis", "ui", "web"), "jarvis/ui/web"),
    (_buildinfo_file, "jarvis"),
]

binaries = []

# ``copy_metadata`` because funasr/torch ask for their own version at import
# time through importlib.metadata, which finds nothing in a frozen tree.
for distribution in ("funasr", "modelscope", "torch", "silero-vad", "edge-tts", "pywebview"):
    try:
        datas += copy_metadata(distribution)
    except Exception:  # a missing optional distribution is not a build failure
        pass

try:
    datas += collect_data_files("silero_vad")  # the TorchScript weights ship in the wheel
    binaries += collect_dynamic_libs("silero_vad")
except Exception:
    pass

for heavy in ("modelscope",):
    try:
        datas += collect_data_files(heavy)
    except Exception:
        pass

# funasr discovers its model / tokenizer / frontend classes by walking its own
# package directory at import time (``import_submodules`` in its ``__init__``).
# A frozen tree has no such directory -- ``_MEIPASS/funasr`` holds only the data
# files -- so the walk finds nothing, ``tables.tokenizer_classes`` stays empty,
# and ``AutoModel`` dies with ``TypeError: 'NoneType' object is not callable``
# while building the tokenizer. Shipping the source tree makes funasr's own
# discovery work again; the PYZ still supplies the code (its importer runs first
# on sys.meta_path), and ``inspect.getsource`` gets real files back.
#
# ``collect_data_files("funasr", includes=["**/*.py"])`` looks like it should do
# this and returns zero entries, which is how the previous build shipped an
# empty registry while the spec claimed otherwise.
try:
    _funasr_spec = importlib.util.find_spec("funasr")
    _funasr_dirs = list(_funasr_spec.submodule_search_locations or ()) if _funasr_spec else []
    if _funasr_dirs:
        datas += [(_funasr_dirs[0], "funasr")]
    else:
        print("[jarvis spec] funasr package directory not found; voice will not load")
except Exception as _exc:
    print(f"[jarvis spec] funasr source collection failed: {type(_exc).__name__}: {_exc}")

# ``collect_submodules`` is still what puts the code in the PYZ; the source tree
# above only makes it discoverable.
try:
    hiddenimports += collect_submodules("funasr")
except Exception:
    pass
try:
    hiddenimports += collect_submodules("modelscope")
except Exception:
    pass

a = Analysis(
    [ENTRY],
    pathex=[ROOT],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    # Order matters: the data-root hook has to run before anything that resolves a
    # path, including the funasr hook below (which writes a report into
    # <data root>/logs and was creating a stray %LOCALAPPDATA%\Jarvis on first run).
    runtime_hooks=[
        os.path.join(ROOT, "packaging", "hooks", "runtime_hook_data_root.py"),
        os.path.join(ROOT, "packaging", "hooks", "runtime_hook_funasr.py"),
    ],
    # The QQ bot channel is a separate product (``python -m jarvis.qqbot``) and
    # pulls its own SDK; nothing in the HUD can reach it.
    excludes=["PySide6", "shiboken6", "tkinter", "pytest", "IPython", "botpy", "notebook"],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="小夜",
    debug=False,
    strip=False,
    upx=False,
    # No console: this is a window, not a terminal program. Startup failures stay
    # visible because LoggingService writes %LOCALAPPDATA%\\Jarvis\\logs\\jarvis.log
    # before the window is even built -- the log file is the crash report.
    console=False,
    # Generated by scripts/make_icon.py (run automatically by build_desktop.py).
    # Without it the taskbar and any desktop shortcut show PyInstaller's default,
    # which is the first thing that makes an app look unfinished.
    icon=os.path.join(ROOT, "packaging", "app.ico"),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="小夜",
)
