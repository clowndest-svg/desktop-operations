"""Bounded, non-secret presentation choices persisted by the desktop shell."""

from typing import Any


def portrait_preferences(value: object) -> dict[str, Any]:
    raw = value if isinstance(value, dict) else {}
    ids = {f"p{i:02}" for i in range(1, 21)}
    bindings = raw.get("bindings", {})
    return {
        "character": (
            raw.get("character")
            if isinstance(raw.get("character"), str) and raw.get("character") in ids
            else "p01"
        ),
        "gaze": raw.get("gaze", True) is not False,
        "emotions": raw.get("emotions", True) is not False,
        "bindings": {
            key: voice
            for key, voice in (bindings.items() if isinstance(bindings, dict) else [])
            if key in ids and isinstance(voice, str) and 0 < len(voice) <= 160
        },
    }
