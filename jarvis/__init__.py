"""JARVIS: a voice-first, agentic Windows AI assistant."""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

__version__ = "0.1.0"
"""Package version, shown in the HUD's title line next to the build stamp.

This does **not** identify a delivery. It only changes when a human remembers to
change it, and no build since the project started has ever changed it -- so comparing
it against a release manifest answers every buyer "you are on the newest one" forever.
The thing that actually changes per artifact is :func:`release_round`.
"""


def _buildinfo() -> dict[str, object] | None:
    """The JSON the spec writes into the bundle, or ``None`` when there is none.

    ``None`` and ``{}`` are different answers and have to stay that way: "this was
    never built" and "this was built by a recipe that forgot a field" deserve
    different sentences, and a caller that cannot tell them apart will print
    ``source`` on a packaged build whose buildinfo is corrupt.
    """
    bundle = getattr(sys, "_MEIPASS", None)
    if bundle:
        candidate = Path(bundle) / "jarvis" / "buildinfo.json"
    else:
        candidate = Path(__file__).resolve().parent.parent / "build" / "buildinfo.json"
    try:
        payload = json.loads(candidate.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else {}


def release_round() -> str:
    """Which build round this artifact is, like ``r107``; ``""`` when unknown.

    The round is the only per-artifact identity the packaging pipeline produces, so
    it is stamped in at build time instead of being a constant someone has to
    remember: the spec reads it back out of the ``--distpath`` it was handed.
    ``check_update`` compares these as integers -- and refuses to guess when this one
    is empty, because "no round" on a buyer's machine means the recipe regressed,
    while silently falling back to ``0`` would tell them a newer build exists forever.

    Only a frozen artifact has a round. A source checkout keeps reading
    ``build/buildinfo.json`` left over from whoever last ran PyInstaller here, and
    reporting *that* round would make the answer depend on what this machine happened
    to build -- which is how a test passes on one afternoon and fails the next.
    """
    if not getattr(sys, "frozen", False):
        return ""
    return str((_buildinfo() or {}).get("round", "") or "").strip()


def is_buyer() -> bool:
    return bool(getattr(sys, "frozen", False) and (_buildinfo() or {}).get("edition") == "buyer")


def build_stamp() -> str:
    """When *this artifact* was produced, as ``YYYY-MM-DD HH:MM``, or ``""``.

    The HUD has to answer "am I looking at the build I meant to run". Five packaged
    revisions sitting on one machine look identical on screen, and the difference
    between them is the feature someone is about to demo, so the answer has to be
    visible without opening a file manager.

    Read from a file the spec writes at build time rather than from the exe's
    modification time: unpacking a delivery rewrites that mtime to the moment of
    unpacking, and a stamp that lies about when something was built is worse than
    no stamp at all. A source checkout has never been built, so it says ``source``
    -- which is also a real answer, and the one the browser preview needs.
    """
    loaded = _buildinfo()
    if loaded is None:
        return "source" if not getattr(sys, "_MEIPASS", None) else ""
    built = str(loaded.get("built_at", "")).strip()
    if not built:
        return ""
    try:
        return datetime.strptime(built, "%Y-%m-%d %H:%M").strftime("%m-%d %H:%M")
    except ValueError:
        return built
