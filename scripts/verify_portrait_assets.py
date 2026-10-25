"""Check the shipped 20-character atlas output without a GPU or network."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    folder = ROOT / "frontend/src/assets/portraits"
    rigs = json.loads((folder / "rigs.json").read_text(encoding="utf-8"))
    identities: set[str] = set()
    total = 0
    hd_folder = ROOT / "frontend/public/portraits-hd"
    hd_manifest = json.loads((hd_folder / "manifest.json").read_text(encoding="utf-8"))
    assert {row["target"] for row in hd_manifest["frames"]} == {
        f"p01-{frame}.webp" for frame in range(6)
    }
    for number in range(1, 21):
        character = f"p{number:02}"
        hashes: set[str] = set()
        for frame in range(6):
            path = folder / f"{character}-{frame}.webp"
            with Image.open(path) as image:
                image.load()
                assert image.size == (1024, 1024), path
                assert image.mode == "RGBA", path
                assert image.getchannel("A").getextrema()[0] == 0, path
                digest = hashlib.sha256(image.tobytes()).hexdigest()
                hashes.add(digest)
                if frame == 0:
                    identities.add(digest)
            total += path.stat().st_size
        assert len(hashes) == 6, f"Missing independent expression: {character}"
        assert len(rigs[character]["eyes"]) == 2
        for box in [rigs[character]["face"], rigs[character]["mouth"], *rigs[character]["eyes"]]:
            x, y, width, height = box
            assert 0 <= x < x + width <= 1 and 0 <= y < y + height <= 1, character
        with Image.open(folder / f"{character}-thumb.webp") as thumb:
            assert thumb.size == (192, 192)
        if character == "p01":
            for frame in range(6):
                with Image.open(hd_folder / f"p01-{frame}.webp") as image:
                    image.load()
                    assert image.size == (
                        (4096, 4096) if frame == 0 else (2048, 2048)
                    ), image.filename
                    assert image.mode == "RGBA", image.filename
    assert len(identities) == 20
    report = {
        "ok": True,
        "characters": 20,
        "distinct_expression_frames": 120,
        "frame_size": 1024,
        "hd_character": "p01",
        "hd_frame_size": 4096,
        "frame_bytes": total,
    }
    output = ROOT / "reports/portraits-r127/assets.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report))


if __name__ == "__main__":
    main()
