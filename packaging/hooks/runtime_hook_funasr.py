"""PyInstaller runtime hook: keep a frozen funasr able to register its models.

funasr fills ``tables.model_classes`` / ``tokenizer_classes`` / ``frontend_classes``
by importing its own submodules, and it discovers them by walking its package
directory (``import_submodules`` in ``funasr/__init__.py``). A frozen build has no
such directory, so the walk finds nothing and ``AutoModel`` later fails as
``TypeError: 'NoneType' object is not callable`` -- an error that names no module
and no fix. The spec ships ``funasr/**/*.py`` alongside the PYZ so the walk works;
this hook checks that it actually did.

Two things happen here, both cheap:

1. ``inspect.getsource`` / ``getsourcelines`` are wrapped so a missing source file
   returns empty text instead of raising. The shipped tree should cover every
   module, but a decorator that cannot read its source should degrade to empty
   metadata rather than drop a class from the registry.
2. The bundle is inspected for the funasr source tree and the answer is written to
   ``<data>/logs/funasr-hook.log``. This is a windowed app: stdout goes nowhere,
   and a hook that swallows its own diagnosis is how the next regression becomes
   another afternoon of guessing.

funasr is deliberately *not* imported here. Its discovery imports roughly three
hundred modules, and doing that at bootstrap would delay the window for every
launch to serve a feature most launches never open. The import happens when the
user enables voice, inside the phase the HUD already labels 加载中.
"""

from __future__ import annotations

import inspect
import os
import sys

_SOURCE_FALLBACKS: list[str] = []
_RAW_GETSOURCE = inspect.getsource
_RAW_GETLINES = inspect.getsourcelines


def _label(obj: object) -> str:
    return getattr(obj, "__qualname__", None) or getattr(obj, "__name__", None) or repr(obj)[:60]


def _safe_getsource(obj: object) -> str:
    """``getsource`` is allowed to fail in a frozen build; empty text is not fatal."""
    try:
        return _RAW_GETSOURCE(obj)
    except OSError:
        _SOURCE_FALLBACKS.append(_label(obj))
        return ""


def _safe_getsourcelines(obj: object) -> tuple[list[str], int]:
    try:
        return _RAW_GETLINES(obj)
    except OSError:
        _SOURCE_FALLBACKS.append(_label(obj))
        return [], 0


inspect.getsource = _safe_getsource  # type: ignore[assignment]
inspect.getsourcelines = _safe_getsourcelines  # type: ignore[assignment]


def _funasr_source_tree() -> tuple[str, int]:
    """Return ``(directory, number of .py files)`` for the shipped funasr sources."""
    root = getattr(sys, "_MEIPASS", None)
    if not root:
        return "", -1  # running from source: the real package directory is on disk
    directory = os.path.join(root, "funasr")
    if not os.path.isdir(directory):
        return directory, 0
    count = 0
    for _dirpath, _names, filenames in os.walk(directory):
        count += sum(1 for name in filenames if name.endswith(".py"))
    return directory, count


_source_dir, _source_count = _funasr_source_tree()
lines = [
    f"frozen={getattr(sys, 'frozen', False)} funasr_py_files={_source_count} "
    f"source_dir={_source_dir or '(not frozen)'}"
]
if _source_count == 0:
    lines.append(
        "  funasr source tree is MISSING from the bundle: its registry will be "
        "empty and AutoModel will fail with \"'NoneType' object is not callable\""
    )

report = "\n".join(lines)

print(f"[jarvis][funasr hook] {report}", flush=True)
try:
    _root = os.environ.get("JARVIS_HOME") or os.path.join(
        os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "Jarvis"
    )
    _logs = os.path.join(_root, "logs")
    os.makedirs(_logs, exist_ok=True)
    with open(os.path.join(_logs, "funasr-hook.log"), "a", encoding="utf-8") as _sink:
        _sink.write(report + "\n")
except Exception:
    pass
