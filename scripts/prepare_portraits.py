"""Register generated poses by eyes/nose before making facial animation patches.

Build-time: OpenCV 4.x, NumPy, Pillow, MIT YuNet from opencv/opencv_zoo.
Set XIAOYE_PORTRAIT_LANDMARK_MODEL to the downloaded 2023mar ONNX model.
No facial detector runs inside the application.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageOps

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "frontend/src/assets/portraits"
MODEL_SHA = "8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4"
SIZE = 1024
SCALE = SIZE / 512.0


def main() -> None:
    model = Path(
        os.environ.get(
            "XIAOYE_PORTRAIT_LANDMARK_MODEL",
            str(ROOT.parent / "XiaoYeBuild/asset-tools/face_detection_yunet_2023mar.onnx"),
        )
    )
    if hashlib.sha256(model.read_bytes()).hexdigest() != MODEL_SHA:
        raise ValueError("Unexpected facial-landmark model bytes")
    detector = cv2.FaceDetectorYN.create(str(model), "", (SIZE, SIZE), 0.8)
    OUT.mkdir(parents=True, exist_ok=True)
    rigs, registration = {}, {}
    sheet = Image.new("RGB", (1000, 880), "#211d26")
    draw = ImageDraw.Draw(sheet)

    def landmarks(image: Image.Image) -> np.ndarray:
        pixels = cv2.cvtColor(np.array(image.convert("RGB")), cv2.COLOR_RGB2BGR)
        _, found = detector.detect(pixels)
        if found is None or len(found) != 1:
            raise ValueError("Each cell must contain exactly one detectable face")
        return found[0]

    for number, path in enumerate(sorted((ROOT / "assets-src/portraits").glob("p*.png"))):
        atlas = Image.open(path).convert("RGBA")
        width, height = atlas.width // 2, atlas.height // 3
        frames = [
            ImageOps.fit(
                atlas.crop(
                    ((i % 2) * width, (i // 2) * height, (i % 2 + 1) * width, (i // 2 + 1) * height)
                ),
                (SIZE, SIZE),
            )
            for i in range(6)
        ]
        original = landmarks(frames[0])
        eye_points = original[4:8].reshape(2, 2)
        midpoint = eye_points.mean(axis=0)
        zoom = (80.0 * SCALE) / float(np.linalg.norm(eye_points[1] - eye_points[0]))
        canonical = np.array(
            [
                [zoom, 0, 290 * SCALE - midpoint[0] * zoom],
                [0, zoom, 205 * SCALE - midpoint[1] * zoom],
            ],
            dtype=np.float64,
        )
        # Keep all characters at the same comfortable head scale and height.
        frames = [
            Image.fromarray(
                cv2.warpAffine(
                    np.array(frame),
                    canonical,
                    (SIZE, SIZE),
                    flags=cv2.INTER_CUBIC,
                    borderMode=cv2.BORDER_CONSTANT,
                )
            )
            for frame in frames
        ]
        base = landmarks(frames[0])
        target = base[4:10].reshape(3, 2)
        scores = []
        for index in range(1, 6):
            moving = landmarks(frames[index])
            source = moving[4:10].reshape(3, 2)
            # Pin both eyelid centres exactly; generated eyebrows/nose positions
            # can change with expression, so robust three-point fitting could pick
            # the nose and leave an eye visibly displaced.
            v = source[1] - source[0]
            u = target[1] - target[0]
            denominator = float(v @ v)
            aa = float(u @ v) / denominator
            bb = float(v[0] * u[1] - v[1] * u[0]) / denominator
            transform = np.array([[aa, -bb, 0], [bb, aa, 0]], dtype=np.float64)
            transform[:, 2] = target[0] - transform[:, :2] @ source[0]
            scale = float(np.hypot(aa, bb))
            projected = cv2.transform(source[None], transform)[0]
            residual = float(np.max(np.linalg.norm(projected - target, axis=1)))
            if residual > 20 * SCALE or not 0.8 < scale < 1.2:
                raise ValueError(f"Unreliable landmarks {path.stem}/{index}: {residual}, {scale}")
            if index == 1:
                # Only this frame's lips are composited at runtime. Pin their
                # midpoint too, avoiding a double lip at the feathered boundary.
                source_mouth = moving[10:14].reshape(2, 2).mean(axis=0)
                mapped = transform[:, :2] @ source_mouth + transform[:, 2]
                transform[:, 2] += base[10:14].reshape(2, 2).mean(axis=0) - mapped
            pixels = cv2.warpAffine(
                np.array(frames[index]),
                transform,
                (SIZE, SIZE),
                flags=cv2.INTER_CUBIC,
                borderMode=cv2.BORDER_REFLECT,
            )
            frames[index] = Image.fromarray(pixels)
            scores.append(
                {"frame": index, "residual_px": residual, "transform": transform.tolist()}
            )
        x, y, w, h = [float(v) for v in base[:4]]
        eyes = sorted(base[4:8].reshape(2, 2).tolist())
        spacing = eyes[1][0] - eyes[0][0]
        lips = base[10:14].reshape(2, 2)
        mx, my = lips.mean(axis=0)
        mouth_width = max(60.0 * SCALE, float(abs(lips[1, 0] - lips[0, 0])) * 1.4)
        rigs[path.stem] = {
            "face": [x / SIZE, y / SIZE, w / SIZE, h / SIZE],
            "eyes": [
                [
                    (ex - spacing * 0.34) / SIZE,
                    (ey - spacing * 0.17) / SIZE,
                    spacing * 0.68 / SIZE,
                    spacing * 0.34 / SIZE,
                ]
                for ex, ey in eyes
            ],
            "mouth": [
                float((mx - mouth_width / 2) / SIZE),
                float((my - 21 * SCALE) / SIZE),
                mouth_width / SIZE,
                58 * SCALE / SIZE,
            ],
        }
        registration[path.stem] = scores
        # Canonical scaling can bring the old cell boundary into view. Feather
        # those boundaries consistently, so shoulders never end in a rectangle.
        yy, xx = np.mgrid[:SIZE, :SIZE]
        left = max(0.0, float(canonical[0, 2]))
        right = min(float(SIZE), float(canonical[0, 2] + SIZE * zoom))
        top = max(0.0, float(canonical[1, 2]))
        bottom = min(float(SIZE), float(canonical[1, 2] + SIZE * zoom))
        feather = np.minimum(
            np.clip((xx - left) / (48 * SCALE), 0, 1),
            np.clip((right - xx) / (48 * SCALE), 0, 1),
        )
        feather *= np.minimum(
            np.clip((yy - top) / (24 * SCALE), 0, 1),
            np.clip((bottom - yy) / (72 * SCALE), 0, 1),
        )
        softened = []
        for frame in frames:
            pixels = np.array(frame)
            pixels[:, :, 3] = (pixels[:, :, 3].astype(float) * feather).astype(np.uint8)
            softened.append(Image.fromarray(pixels))
        frames = softened
        for i, frame in enumerate(frames):
            frame.save(OUT / f"{path.stem}-{i}.webp", quality=92, method=4)
        thumb = frames[0].resize((192, 192), Image.Resampling.LANCZOS)
        thumb.save(OUT / f"{path.stem}-thumb.webp", quality=86, method=4)
        left, top = number % 5 * 200, number // 5 * 220
        sheet.paste(thumb.convert("RGB"), (left, top))
        draw.text((left + 8, top + 195), path.stem, fill="white")
        maximum = max(row["residual_px"] for row in scores)
        print(f"Registered {path.stem}: max residual {maximum:.2f}px", flush=True)
    (OUT / "rigs.json").write_text(json.dumps(rigs, indent=2), encoding="utf-8")
    sheet.save(ROOT / "assets-src/portraits/contact-sheet.jpg", quality=90)
    report = ROOT / "reports/portraits-r127/registration.json"
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(json.dumps(registration, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
