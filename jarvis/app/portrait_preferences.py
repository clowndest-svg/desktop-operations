"""Bounded, non-secret presentation choices persisted by the desktop shell."""

from typing import Any


def portrait_preferences(value: object) -> dict[str, Any]:
    raw = value if isinstance(value, dict) else {}
    ids = {f"p{i:02}" for i in range(1, 21)}
    bindings = raw.get("bindings", {})
    favorites: list[str] = []
    seen: set[str] = set()
    for item in raw.get("favorites", []) if isinstance(raw.get("favorites"), list) else []:
        if isinstance(item, str) and item in ids and item not in seen:
            seen.add(item)
            favorites.append(item)
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
        "favorites": favorites[:20],
    }
