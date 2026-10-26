"""Build feature-shaped portrait patches from the registered expression frames.

The old runtime composited rectangular radial-gradient patches, which is why a
smiling mouth could arrive with a cheek ring and a blink could carry an eyebrow.
Here the masks are shaped from the actual mouth/eye regions (lip colour + the rig
boxes) and the patch is stored full-size, so the runtime only has to draw one
image with a soft alpha edge.

Build-time only: OpenCV/Pillow. The application never runs this script.
"""

from __future__ import annotations

import json
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
STD = ROOT / "frontend/src/assets/portraits"
HD = ROOT / "frontend/public/portraits-hd"
OUT = STD / "patches"
REPORT = ROOT / "reports/portraits-r128"


def rgba(path: Path) -> np.ndarray:
    return np.array(Image.open(path).convert("RGBA"))


def ellipse_mask(
    size: tuple[int, int], box: list[float], rx: float, ry: float, feather: float
) -> np.ndarray:
    width, height = size
    x, y, bw, bh = box
    mask = np.zeros((height, width), np.uint8)
    center = (int((x + bw / 2) * width), int((y + bh / 2) * height))
    axes = (max(1, int(bw * width * rx)), max(1, int(bh * height * ry)))
    cv2.ellipse(mask, center, axes, 0, 0, 360, 255, -1)
    if feather:
        mask = cv2.GaussianBlur(mask, (0, 0), feather)
    return mask


def lip_mask(image: np.ndarray, box: list[float]) -> np.ndarray:
    height, width = image.shape[:2]
    x, y, bw, bh = box
    px, py = x * width, y * height
    pw, ph = bw * width, bh * height
    pad = int(ph * 0.25)
    x0, y0 = max(0, int(px - pad)), max(0, int(py - pad))
    x1, y1 = min(width, int(px + pw + pad)), min(height, int(py + ph + pad))
    crop = image[y0:y1, x0:x1, :3].astype(np.float32)
    red = crop[..., 0] - (crop[..., 1] + crop[..., 2]) / 2
    hsv = cv2.cvtColor(crop.astype(np.uint8), cv2.COLOR_RGB2HSV)
    sat = hsv[..., 1].astype(np.float32)
    value = hsv[..., 2].astype(np.float32)
    raw = ((red > 7) & (sat > 55) & (value > 35)).astype(np.uint8) * 255
    prior = np.zeros(raw.shape, np.uint8)
    cv2.ellipse(
        prior,
        (int(px + pw / 2 - x0), int(py + ph / 2 - y0)),
        (max(1, int(pw * 0.95)), max(1, int(ph * 1.05))),
        0,
        0,
        360,
        255,
        -1,
    )
    raw = cv2.bitwise_and(raw, prior)
    raw = cv2.morphologyEx(raw, cv2.MORPH_CLOSE, np.ones((7, 7), np.uint8))
    contours, _ = cv2.findContours(raw, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    filled = np.zeros_like(raw)
    if contours:
        cv2.drawContours(filled, [max(contours, key=cv2.contourArea)], -1, 255, -1)
    filled = cv2.dilate(filled, np.ones((5, 5), np.uint8))
    mask = np.zeros((height, width), np.uint8)
    mask[y0:y1, x0:x1] = filled
    area = float((mask > 16).sum()) / (width * height)
    if area < 0.001 or area > 0.12:
        mask = ellipse_mask((width, height), box, 0.55, 0.52, 4)
    return cv2.GaussianBlur(mask, (0, 0), max(2.0, ph * 0.035))


def save_patch(name: str, image: np.ndarray, mask: np.ndarray) -> dict[str, object]:
    output = image.copy()
    output[..., 3] = np.minimum(output[..., 3], mask).astype(np.uint8)
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / f"{name}.webp"
    Image.fromarray(output, "RGBA").save(path, quality=92, method=4)
    visible = int((mask > 24).sum())
    return {
        "file": path.name,
        "width": int(output.shape[1]),
        "height": int(output.shape[0]),
        "mask_pixels": visible,
        "bytes": path.stat().st_size,
    }


def main() -> None:
    rigs = json.loads((STD / "rigs.json").read_text(encoding="utf-8"))
    manifest: dict[str, object] = {"version": 1, "characters": {}}
    rows = {}
    for number in range(1, 21):
        character = f"p{number:02}"
        rig = rigs[character]
        mouth = rig["mouth"]
        eyes = rig["eyes"]
        high = character == "p01"
        folder = HD if high else STD
        frames = {
            index: rgba(folder / f"{character}-{index}.webp")
            for index in range(1, 6)
            if (folder / f"{character}-{index}.webp").is_file()
        }
        if high and 1 not in frames:
            frames = {index: rgba(STD / f"{character}-{index}.webp") for index in range(1, 6)}
            high = False
        size = frames[1].shape[1::-1]
        entries = {
            "blink": save_patch(
                f"{character}-blink",
                frames[2],
                np.maximum.reduce(
                    [ellipse_mask(size, eye, 0.48, 0.46, size[0] * 0.008) for eye in eyes]
                ),
            ),
            "cavity": save_patch(
                f"{character}-cavity",
                frames[1],
                ellipse_mask(size, mouth, 0.36, 0.30, size[0] * 0.006),
            ),
            "speaking": save_patch(
                f"{character}-speaking",
                frames[1],
                ellipse_mask(size, mouth, 0.50, 0.40, size[0] * 0.008),
            ),
            "happy": save_patch(f"{character}-happy", frames[3], lip_mask(frames[3], mouth)),
            "pout": save_patch(f"{character}-pout", frames[4], lip_mask(frames[4], mouth)),
            "soft": save_patch(f"{character}-soft", frames[5], lip_mask(frames[5], mouth)),
        }
        brow_boxes = [
            [eye[0], max(0.0, eye[1] - eye[3] * 1.45), eye[2], eye[3] * 0.9] for eye in eyes
        ]
        entries["pout-brows"] = save_patch(
            f"{character}-pout-brows",
            frames[4],
            np.maximum.reduce(
                [ellipse_mask(size, box, 0.62, 0.55, size[0] * 0.006) for box in brow_boxes]
            ),
        )
        work = 2048 if high else 1024
        base_image = Image.open((HD if high else STD) / f"{character}-0.webp").convert("RGBA")
        if base_image.size != (work, work):
            base_image = base_image.resize((work, work), Image.Resampling.LANCZOS)
        entries["mouth"] = jaw_frames(character, np.array(base_image), mouth, rig["face"], work)
        manifest["characters"][character] = {"high": high, "patches": entries}
        rows[character] = entries
        print(f"patches {character}: {len(entries)} groups", flush=True)
    (OUT / "patches.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    REPORT.mkdir(parents=True, exist_ok=True)
    (REPORT / "patches.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    total = 0
    count = 0
    for record in rows.values():
        for entry in record.values():
            if isinstance(entry, dict) and "bytes" in entry:
                total += int(entry["bytes"])
                count += 1
            elif isinstance(entry, dict):
                for nested in entry.values():
                    total += int(nested["bytes"])
                    count += 1
    print(f"total {total / 1024 / 1024:.1f} MiB, {count} files")


def jaw_frames(
    character: str, base: np.ndarray, mouth: list[float], face: list[float], work: int
) -> dict[str, object]:
    """Four pre-warped open-mouth frames, so the runtime only cross-fades them.

    The old square mouth patch had to blend a smiling open mouth over a closed
    neutral one. These frames move the lower lip and chin themselves; the cavity
    patch is composited into the opening, so the motion is a jaw drop rather than
    a second mouth appearing.
    """
    height, width = base.shape[:2]
    cx, cy = (mouth[0] + mouth[2] / 2) * width, (mouth[1] + mouth[3] / 2) * height
    mw, mh = mouth[2] * width, mouth[3] * height
    top = (mouth[1] + mouth[3] * 0.06) * height
    bottom = min(float(height), (face[1] + face[3] * 0.94) * height)
    yy, xx = np.mgrid[0:height, 0:width].astype(np.float32)
    t = np.clip((yy - top) / max(1.0, bottom - top), 0.0, 1.0)
    bell = np.sin(np.pi * t)
    horizontal = np.exp(-(((xx - cx) / max(1.0, mw * 1.25)) ** 4))
    cavity = Image.open(OUT / f"{character}-cavity.webp").convert("RGBA")
    if cavity.size != (width, height):
        cavity = cavity.resize((width, height), Image.Resampling.LANCZOS)
    entries = {}
    for level, openness in (("25", 0.25), ("50", 0.50), ("75", 0.75), ("100", 1.0)):
        drop = mh * 0.55 * openness
        disp = drop * bell * horizontal
        map_x = xx
        map_y = yy - disp
        rgb = cv2.remap(base[..., :3], map_x, map_y, cv2.INTER_CUBIC, borderMode=cv2.BORDER_REFLECT)
        alpha = cv2.remap(
            base[..., 3], map_x, map_y, cv2.INTER_CUBIC, borderMode=cv2.BORDER_REFLECT
        )
        warped = np.dstack([rgb, alpha])
        scale_y = 1 + openness * 0.55
        matrix = np.array(
            [[1.0, 0.0, cx * 0.0], [0.0, scale_y, cy * (1 - scale_y) + drop * 0.16]],
            dtype=np.float32,
        )
        cavity_moved = cv2.warpAffine(
            np.array(cavity), matrix, (width, height), flags=cv2.INTER_CUBIC
        )
        composite = Image.alpha_composite(Image.fromarray(warped), Image.fromarray(cavity_moved))
        # A rectangular blend showed a seam at the neck. Fade a soft ellipse around
        # the mouth/chin instead; where the displacement is zero the base shows
        # through, and where it is large the ellipse is opaque.
        yy2, xx2 = np.mgrid[0:height, 0:width].astype(np.float32)
        ellipse = (
            ((xx2 - cx) / max(1.0, mw * 1.55)) ** 2
            + ((yy2 - (top + bottom) / 2) / max(1.0, (bottom - top) * 0.62)) ** 2
        ) <= 1.0
        mask = (
            cv2.GaussianBlur(ellipse.astype(np.uint8) * 255, (0, 0), max(2.0, mw * 0.09)).astype(
                np.float32
            )
            / 255.0
        )
        out = np.array(composite)
        out[..., 3] = (out[..., 3].astype(np.float32) * mask).astype(np.uint8)
        path = OUT / f"{character}-mouth-{level}.webp"
        Image.fromarray(out, "RGBA").save(path, quality=92, method=4)
        entries[level] = {
            "file": path.name,
            "width": int(width),
            "height": int(height),
            "bytes": path.stat().st_size,
        }
    return entries


if __name__ == "__main__":
    main()
