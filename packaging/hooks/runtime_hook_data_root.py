"""Runtime hook: give the frozen app a data root before anything else asks for one.

Why a hook and not ``packaging/entry.py``
----------------------------------------
The entry script looked like the right place, and shipped a bug only the packaged
form could show: PyInstaller runs **runtime hooks before the entry script**, and
``runtime_hook_funasr`` already creates ``<data root>/logs`` for its own report. By
the time the entry script asked whether an install existed at the old location, the
funasr hook had just created one there -- so every first launch "found" an existing
install and left its data on the system drive.

This hook runs first, so the rest of the process -- hooks, entry script, library --
sees one answer. The rule itself stays in :func:`jarvis.config.paths.bundle_data_root`
rather than being copied here; two copies of a fallback is how they disagree.

Everything is best-effort: a hook that raises takes the window down with it, and a
startable default beats a correct crash.
"""

import os
import sys
from pathlib import Path


def _seed() -> None:
    from jarvis.config.paths import ENV_HOME, bundle_data_root

    local_app_data = os.environ.get("LOCALAPPDATA", "").strip()
    if not local_app_data:
        return  # no known Windows profile; leave the library's own order alone
    if not sys.argv or not sys.argv[0]:  # pragma: no cover - a launcher always has one
        return
    chosen = bundle_data_root(
        os.environ,
        Path(sys.argv[0]).resolve().parent,
        Path(local_app_data) / "Jarvis",
    )
    if chosen is None:
        return
    chosen.mkdir(parents=True, exist_ok=True)
    os.environ[ENV_HOME] = str(chosen)
    print(f"[jarvis][data-root hook] JARVIS_HOME := {chosen}", flush=True)


try:
    _seed()
except Exception as exc:
    print(f"[jarvis][data-root hook] skipped: {type(exc).__name__}: {exc}", file=sys.stderr)
