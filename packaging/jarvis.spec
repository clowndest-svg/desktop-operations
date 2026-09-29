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
import os

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
    "jarvis.ui.state_bridge",
]

datas = [
    (os.path.join(ROOT, "jarvis", "config", "defaults.yaml"), "jarvis/config"),
    (os.path.join(ROOT, "jarvis", "ui", "web"), "jarvis/ui/web"),
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
    # funasr discovers its model classes by scanning its own package directory,
    # which a frozen tree does not have; this hook re-imports them at start-up.
    runtime_hooks=[os.path.join(ROOT, "packaging", "hooks", "runtime_hook_funasr.py")],
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
    icon=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="小夜",
)
