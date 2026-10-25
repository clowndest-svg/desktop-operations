"""Split generated six-pose atlases and calibrate facial patches (build-time only).

Run with OpenCV 4.x on PYTHONPATH; the application does not depend on OpenCV.
Atlas cells are row-major neutral / speaking / blink / happy / pout / soft.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageOps

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "frontend/src/assets/portraits"


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    detector = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
    eyes = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_eye.xml")
    rig_path = OUT / "rigs.json"
    rigs = json.loads(rig_path.read_text(encoding="utf-8")) if rig_path.exists() else {}
    sheet = Image.new("RGB", (1000, 4 * 220), "#211d26")
    draw = ImageDraw.Draw(sheet)
    for number, path in enumerate(sorted((ROOT / "assets-src/portraits").glob("p*.png"))):
        if len(sys.argv) > 1 and int(path.stem[1:]) < int(sys.argv[1]):
            thumb = Image.open(OUT / f"{path.stem}-thumb.webp").convert("RGB")
            left, top = (number % 5) * 200, (number // 5) * 220
            sheet.paste(thumb, (left, top))
            draw.text((left + 8, top + 195), path.stem, fill="white")
            continue
        atlas = Image.open(path).convert("RGBA")
        width, height = atlas.width // 2, atlas.height // 3
        frames = [
            atlas.crop(
                ((i % 2) * width, (i // 2) * height, (i % 2 + 1) * width, (i // 2 + 1) * height)
            )
            for i in range(6)
        ]
        frames = [ImageOps.fit(frame, (512, 512), centering=(0.5, 0.5)) for frame in frames]
        width = height = 512
        gray = cv2.cvtColor(np.array(frames[0].convert("RGB")), cv2.COLOR_RGB2GRAY)
        faces = detector.detectMultiScale(gray, 1.05, 5)
        if not len(faces):
            raise ValueError(f"No calibrated face: {path}")
        x, y, w, h = max(faces, key=lambda box: box[2] * box[3])
        candidates = eyes.detectMultiScale(gray[y : y + h // 2, x : x + w], 1.05, 5)
        found = sorted(candidates, key=lambda box: box[2] * box[3], reverse=True)[:2]
        found = sorted(found, key=lambda box: box[0])
        eye_boxes = [
            [(x + ex) / width, (y + ey + eh * 0.25) / height, ew / width, eh * 0.55 / height]
            for ex, ey, ew, eh in found
        ]
        if len(eye_boxes) != 2:
            eye_boxes = [
                [(x + w * f) / width, (y + h * 0.30) / height, w * 0.28 / width, h * 0.13 / height]
                for f in (0.15, 0.57)
            ]
        rigs[path.stem] = {
            "face": [float(x / width), float(y / height), float(w / width), float(h / height)],
            "eyes": [[float(v) for v in box] for box in eye_boxes],
            "mouth": [
                float((x + w * 0.24) / width),
                float((y + h * 0.64) / height),
                float(w * 0.53 / width),
                float(h * 0.25 / height),
            ],
        }
        for i, frame in enumerate(frames):
            frame.save(OUT / f"{path.stem}-{i}.webp", quality=88, method=4)
        thumb = frames[0].resize((192, 192), Image.Resampling.LANCZOS)
        thumb.save(OUT / f"{path.stem}-thumb.webp", quality=82, method=6)
        tile = thumb.convert("RGB")
        left, top = (number % 5) * 200, (number // 5) * 220
        sheet.paste(tile, (left, top))
        draw.text((left + 8, top + 195), path.stem, fill="white")
    (OUT / "rigs.json").write_text(json.dumps(rigs, indent=2), encoding="utf-8")
    sheet.save(ROOT / "assets-src/portraits/contact-sheet.jpg", quality=90)
    print(f"Prepared {len(rigs)} characters")


if __name__ == "__main__":
    main()
