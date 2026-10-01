"""Draw the application icon and write it as a multi-size .ico.

Why hand-drawn pixels rather than an asset: the icon is a dozen geometric
primitives in the HUD's own palette, and generating it here keeps the repository
free of a binary blob nobody can edit. It also means the icon is *reproducible* —
changing the accent colour is a one-line diff, not a round trip through a design
tool.

Why not Pillow: this runs before any install exists -- ``pyinstaller`` is invoked
from a bare checkout, and the icon is a build input. It stays stdlib so the build
never depends on whether an extra happened to be installed. (The *tray* icon is a
different matter: :mod:`jarvis.ui.tray` needs to recolour the same mark at runtime,
and Pillow ships with the desktop extra for exactly that.)

The shape: a dark rounded square (the HUD background, lifted a little so it reads
against a dark taskbar) with a cyan ring and a glowing core — an "always
listening" sensor mark. It is deliberately legible at 16 px: two concentric
circles survive downscaling, whereas anything with fine detail turns to mud in
the taskbar.

Usage::

    python scripts/make_icon.py                     # writes jarvis/ui/web/app.ico
    python scripts/make_icon.py --out some/path.ico
"""

from __future__ import annotations

import argparse
import math
import struct
import sys
import zlib
from pathlib import Path

# --- palette --------------------------------------------------------------

BACKDROP_TOP = (14, 22, 38)
"""Rounded-square fill, top. Slightly lighter than the HUD's #04070d so the icon
is visible on a dark taskbar instead of dissolving into it."""

BACKDROP_BOTTOM = (4, 7, 13)
"""Rounded-square fill, bottom — the exact HUD background, for continuity."""

ACCENT = (56, 189, 248)
"""Ring colour: the cyan the HUD already uses for live values."""

CORE = (186, 240, 255)
"""Core dot, brighter than the ring so the mark has a focal point at 16 px."""

# --- geometry, as fractions of the icon size ------------------------------

CORNER_RADIUS = 0.22
RING_OUTER = 0.355
RING_INNER = 0.300
CORE_RADIUS = 0.105
GLOW_RADIUS = 0.26

ICO_SIZES = (16, 24, 32, 48, 64, 128, 256)
"""Every size Windows asks for. 16 is the taskbar, 256 is the file-explorer
"extra large icons" view; generating the whole ladder beats letting Windows
resample one bitmap badly."""

SUPERSAMPLE = 3
"""Per-axis supersampling when rasterising. 3 is the knee: 9 samples per output
pixel removes the stair-stepping on the ring without a visible cost."""


def _smoothstep(edge0: float, edge1: float, value: float) -> float:
    """Hermite ramp from 0 at ``edge0`` to 1 at ``edge1`` (either order)."""
    if edge0 == edge1:
        return 0.0 if value < edge0 else 1.0
    t = (value - edge0) / (edge1 - edge0)
    t = max(0.0, min(1.0, t))
    return t * t * (3.0 - 2.0 * t)


def _rounded_square_coverage(x: float, y: float, size: float) -> float:
    """Coverage of the rounded square, for one sample point in pixel units."""
    radius = CORNER_RADIUS * size
    half = size / 2.0
    # Distance from the square's edge, with the corners treated as arcs.
    dx = abs(x - half) - (half - radius)
    dy = abs(y - half) - (half - radius)
    if dx <= 0.0 or dy <= 0.0:
        return 1.0
    distance = math.hypot(dx, dy) - radius
    return 1.0 - _smoothstep(-0.5, 0.5, distance)


def _sample(x: float, y: float, size: float) -> tuple[int, int, int, int]:
    """Colour of one sample point. Returns straight RGBA."""
    coverage = _rounded_square_coverage(x, y, size)
    if coverage <= 0.0:
        return 0, 0, 0, 0

    # Vertical gradient across the rounded square.
    blend = max(0.0, min(1.0, y / size))
    red = BACKDROP_TOP[0] + (BACKDROP_BOTTOM[0] - BACKDROP_TOP[0]) * blend
    green = BACKDROP_TOP[1] + (BACKDROP_BOTTOM[1] - BACKDROP_TOP[1]) * blend
    blue = BACKDROP_TOP[2] + (BACKDROP_BOTTOM[2] - BACKDROP_TOP[2]) * blend

    centre = size / 2.0
    distance = math.hypot(x - centre, y - centre) / size

    # Soft halo behind the mark, so the core reads as emitting light.
    halo = 1.0 - _smoothstep(CORE_RADIUS, GLOW_RADIUS, distance)
    halo *= 0.30
    red += (ACCENT[0] - red) * halo
    green += (ACCENT[1] - green) * halo
    blue += (ACCENT[2] - blue) * halo

    # Ring: full opacity between the two radii, feathered at both edges.
    ring = _smoothstep(RING_INNER - 0.012, RING_INNER + 0.012, distance) * (
        1.0 - _smoothstep(RING_OUTER - 0.012, RING_OUTER + 0.012, distance)
    )
    # Core dot.
    core = 1.0 - _smoothstep(CORE_RADIUS - 0.012, CORE_RADIUS + 0.012, distance)

    red = red + (ACCENT[0] - red) * ring
    green = green + (ACCENT[1] - green) * ring
    blue = blue + (ACCENT[2] - blue) * ring
    red = red + (CORE[0] - red) * core
    green = green + (CORE[1] - green) * core
    blue = blue + (CORE[2] - blue) * core

    alpha = coverage * 255.0
    return (
        int(red + 0.5),
        int(green + 0.5),
        int(blue + 0.5),
        int(alpha + 0.5),
    )


def render_rgba(size: int) -> bytes:
    """Rasterise one square icon into straight RGBA rows."""
    step = 1.0 / SUPERSAMPLE
    samples = SUPERSAMPLE * SUPERSAMPLE
    rows = bytearray()
    for pixel_y in range(size):
        for pixel_x in range(size):
            red = green = blue = alpha = 0
            for sub_y in range(SUPERSAMPLE):
                for sub_x in range(SUPERSAMPLE):
                    sample = _sample(
                        pixel_x + (sub_x + 0.5) * step,
                        pixel_y + (sub_y + 0.5) * step,
                        float(size),
                    )
                    # Premultiply before averaging so the antialiased edge does
                    # not pick up colour from fully transparent neighbours.
                    red += sample[0] * sample[3]
                    green += sample[1] * sample[3]
                    blue += sample[2] * sample[3]
                    alpha += sample[3]
            if alpha == 0:
                rows += b"\x00\x00\x00\x00"
                continue
            # Round the alpha *first*: a pixel whose coverage rounds down to 0
            # must be written as fully transparent. Emitting its colour with an
            # alpha byte of 0 leaves a dark speck on the antialiased rim that
            # some renderers composite anyway.
            out_alpha = min(255, int(alpha / samples + 0.5))
            if out_alpha == 0:
                rows += b"\x00\x00\x00\x00"
                continue
            rows += bytes(
                (
                    min(255, int(red / alpha + 0.5)),
                    min(255, int(green / alpha + 0.5)),
                    min(255, int(blue / alpha + 0.5)),
                    out_alpha,
                )
            )
    return bytes(rows)


def _png(size: int, rgba: bytes) -> bytes:
    """Encode straight RGBA rows as a PNG (colour type 6)."""

    def chunk(tag: bytes, payload: bytes) -> bytes:
        body = tag + payload
        return struct.pack(">I", len(payload)) + body + struct.pack(">I", zlib.crc32(body))

    stride = size * 4
    raw = b"".join(b"\x00" + rgba[y * stride : (y + 1) * stride] for y in range(size))
    header = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b"")
    )


def build_ico(sizes: tuple[int, ...] = ICO_SIZES) -> bytes:
    """Assemble a multi-size .ico whose entries are PNG payloads.

    PNG-in-ICO is supported from Windows Vista on and is what every modern
    toolchain emits: a 256x256 BMP entry would be 256 KB uncompressed, the PNG
    is a few KB, and the shell decodes it natively.
    """
    images = [(size, _png(size, render_rgba(size))) for size in sizes]
    directory = struct.pack("<HHH", 0, 1, len(images))
    offset = len(directory) + 16 * len(images)
    entries = bytearray()
    for size, payload in images:
        # 0 means 256: the field is one byte.
        entries += struct.pack(
            "<BBBBHHII",
            size if size < 256 else 0,
            size if size < 256 else 0,
            0,
            0,
            1,
            32,
            len(payload),
            offset,
        )
        offset += len(payload)
    return bytes(directory + bytes(entries) + b"".join(payload for _, payload in images))


def main(argv: list[str] | None = None) -> int:
    # Written into ``packaging/`` rather than ``jarvis/ui/web/`` on purpose:
    # the web bundle is gitignored (hash-named assets churn), so an icon kept
    # there would vanish on a fresh checkout and the build would silently fall
    # back to PyInstaller's default.
    default_out = Path(__file__).resolve().parent.parent / "packaging" / "app.ico"
    parser = argparse.ArgumentParser(description="生成小夜的应用图标（多尺寸 .ico）")
    parser.add_argument("--out", type=Path, default=default_out, help="输出 .ico 路径")
    args = parser.parse_args(argv)

    data = build_ico()
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_bytes(data)
    print(f"已生成 {args.out}（{len(ICO_SIZES)} 种尺寸，{len(data) / 1024:.1f} KiB）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
