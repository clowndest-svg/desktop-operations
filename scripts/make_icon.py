"""Create the Windows icon from the supplied XiaoYe logo."""

from __future__ import annotations

import argparse
import io
import struct
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "assets-src/brand/xiaoye-logo.jpg"
ICO_SIZES = (16, 24, 32, 48, 64, 128, 256)


def render_rgba(size: int) -> bytes:
    """Resize the actual logo with an antialiased round silhouette."""
    with Image.open(SOURCE) as source:
        image = source.convert("RGBA").resize((size, size), Image.Resampling.LANCZOS)
    mask = Image.new("L", (size * 4, size * 4))
    ImageDraw.Draw(mask).ellipse((0, 0, size * 4 - 1, size * 4 - 1), fill=255)
    image.putalpha(mask.resize((size, size), Image.Resampling.LANCZOS))
    data = bytearray(image.tobytes())
    for at in range(0, len(data), 4):
        if data[at + 3] == 0:
            data[at : at + 3] = b"\0\0\0"
    return bytes(data)


def build_ico(sizes: tuple[int, ...] = ICO_SIZES) -> bytes:
    """PNG-in-ICO, including taskbar and Explorer sizes."""
    images = []
    for size in sizes:
        stream = io.BytesIO()
        Image.frombytes("RGBA", (size, size), render_rgba(size)).save(stream, format="PNG")
        images.append((size, stream.getvalue()))
    directory = struct.pack("<HHH", 0, 1, len(images))
    offset = len(directory) + 16 * len(images)
    entries = bytearray()
    for size, payload in images:
        entries += struct.pack(
            "<BBBBHHII", size % 256, size % 256, 0, 0, 1, 32, len(payload), offset
        )
        offset += len(payload)
    return directory + bytes(entries) + b"".join(payload for _, payload in images)


def main() -> int:
    parser = argparse.ArgumentParser(description="从小夜原图生成多尺寸应用图标")
    parser.add_argument("--out", type=Path, default=ROOT / "packaging/app.ico")
    args = parser.parse_args()
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_bytes(build_ico())
    print(f"已生成 {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
