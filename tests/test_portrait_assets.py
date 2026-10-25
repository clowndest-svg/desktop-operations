"""Guard the shipped portrait resolution and the desktop-only 4K copies."""

from __future__ import annotations

import json
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
STANDARD = ROOT / "frontend/src/assets/portraits"
HD = ROOT / "frontend/public/portraits-hd"


def test_twenty_characters_have_six_native_1024_frames() -> None:
    rigs = json.loads((STANDARD / "rigs.json").read_text(encoding="utf-8"))
    assert len(rigs) == 20
    for number in range(1, 21):
        character = f"p{number:02}"
        assert len(rigs[character]["eyes"]) == 2
        hashes = set()
        for frame in range(6):
            path = STANDARD / f"{character}-{frame}.webp"
            with Image.open(path) as image:
                image.load()
                assert image.size == (1024, 1024), path
                assert image.mode == "RGBA", path
                assert image.getchannel("A").getextrema()[0] == 0, path
                hashes.add(image.tobytes())
        assert len(hashes) == 6, character
        for box in (
            rigs[character]["face"],
            rigs[character]["mouth"],
            *rigs[character]["eyes"],
        ):
            x, y, width, height = box
            assert 0 <= x < x + width <= 1 and 0 <= y + height <= 1, character


def test_default_character_has_a_complete_4k_set() -> None:
    manifest = json.loads((HD / "manifest.json").read_text(encoding="utf-8"))
    targets = {row["target"] for row in manifest["frames"]}
    assert targets == {f"p01-{frame}.webp" for frame in range(6)}
    for frame in range(6):
        path = HD / f"p01-{frame}.webp"
        with Image.open(path) as image:
            image.load()
            expected = (4096, 4096) if frame == 0 else (2048, 2048)
            assert image.size == expected, path
            assert image.mode == "RGBA", path


def test_face_registration_still_lands_inside_the_4k_frame() -> None:
    rigs = json.loads((STANDARD / "rigs.json").read_text(encoding="utf-8"))
    face = rigs["p01"]["face"]
    # Uniform scaling must keep the same normalized box; a different crop would
    # make the mouth patch land on the cheek.
    assert 0.2 < face[1] < 0.4
    assert 0.2 < face[3] < 0.6
