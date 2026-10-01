"""JARVIS: a voice-first, agentic Windows AI assistant."""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

__version__ = "0.1.0"
"""Package version, shown in the HUD's title line next to the build stamp."""


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
    bundle = getattr(sys, "_MEIPASS", None)
    if bundle:
        candidate = Path(bundle) / "jarvis" / "buildinfo.json"
    else:
        candidate = Path(__file__).resolve().parent.parent / "build" / "buildinfo.json"
    try:
        payload = json.loads(candidate.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "source" if not bundle else ""
    built = str(payload.get("built_at", "")).strip()
    if not built:
        return ""
    try:
        return datetime.strptime(built, "%Y-%m-%d %H:%M").strftime("%m-%d %H:%M")
    except ValueError:
        return built
