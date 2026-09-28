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

for heavy in ("funasr", "modelscope"):
    try:
        datas += collect_data_files(heavy)
    except Exception:
        pass

# ``inspect.getsource`` is the reason a frozen funasr cannot register anything:
# its model modules read their own source at import time, and a PYZ archive holds
# compiled code only -- the failure is ``OSError: could not get source code``.
# Shipping the .py files next to the .pyc paths lets ``inspect`` find them.
for source_only in ("funasr",):
    try:
        datas += collect_data_files(source_only, includes=["**/*.py"])
    except Exception:
        pass

# funasr builds a model by looking its class up in a registry populated by
# ``@tables.register`` decorators at import time. Static analysis keeps the
# registry but drops the model modules that were never imported directly, so the
# lookup returns None and the frozen app dies with
# ``TypeError: 'NoneType' object is not callable`` inside auto_model.build_model.
# Collecting every submodule is what makes the registry complete.
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
