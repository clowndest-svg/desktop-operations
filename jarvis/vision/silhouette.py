"""Photo → the outline of whatever the photo is about, computed on this machine.

Why an outline and not a scanned mesh
-------------------------------------
The ask was "hand the app a photo, get a line-art 3D pet". The pipelines that
actually turn one photo into a solid (Hunyuan3D-2, TRELLIS) want 10 GB+ of VRAM
for the reconstruction pass. This machine has a GTX 1650 with 4 GB, so the choice
was between waiting on somebody else's GPU and doing a different thing locally in
about a second.

What happens here instead: the subject is segmented offline (U^2-Net lite -- 4.57
MB of weights, ONNX Runtime, CPU only), its outer boundary is traced and
simplified to a polygon, and the page extrudes that polygon into a slab. The pet
is therefore genuinely three-dimensional from the front and flat when seen exactly
from the side, and it has **no skeleton**: it can breathe, turn and tilt, and it
cannot sit down or nod. Both facts belong on screen rather than in this docstring,
because a person picks a candidate from a preview, and the preview has to promise
the same thing the desktop delivers.

Measured on this machine (i7-10750H, no GPU used, ``.r60-verify/probe_u2netp.py``)
--------------------------------------------------------------------------------
* one segmentation pass at 320x320: **210-263 ms**, all of it on CPU;
* agreement with rembg's own reference cut-outs on two animal photos, IoU of the
  binarised mask: **0.974** and **0.970** -- and only with RGB channel order plus
  ImageNet mean/std. BGR order, or skipping the normalisation, costs 1-3 points.
  That is why the preprocessing is pinned in :func:`_blob`: a silent channel-order
  change would read as "the cut-out got worse this week" and nothing else.

Nothing here uploads the photo, and nothing here keeps it: :class:`Cut` holds a
boundary and a coverage ratio, which is all a mesh needs.
"""

from __future__ import annotations

import io
import logging
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from jarvis.core.exceptions import VisionError

logger = logging.getLogger("jarvis.vision.silhouette")

__all__ = [
    "Cut",
    "Outline",
    "average_colour",
    "cut_from_mask",
    "preview_png",
    "segment",
    "simplify",
    "thumbnail",
    "weights_gap",
]


Point = tuple[float, float]
"""A vertex in normalised space: ``y`` up, the figure exactly 1.0 tall, ``x`` centred."""

Pixel = tuple[int, int]
"""A vertex in mask pixels, ``(x, y)``, ``y`` growing downward."""

Segmenter = Callable[[Any, int, int], bytes]
"""``(blob, edge, edge) -> probability bytes``; injectable so tests need no weights.

The production one is :func:`_onnx_segmenter`. A test hands in a function that
returns a known 320x320 map, which puts the flood fill, the trace, the
simplification and the preview all under pytest without a native runtime.
"""

WEIGHTS_FILE: Final[Path] = Path(__file__).resolve().parent / "data" / "u2netp.onnx"
"""The segmentation weights, shipped inside the package.

``Path(__file__).parent`` is how this repository locates every bundled resource
(see ``WEB_DIR`` in :mod:`jarvis.ui.desktop`): in a frozen onedir build that
already resolves under ``_MEIPASS``, and the spec's ``datas`` puts the file there.
"""

MODEL_EDGE: Final[int] = 320
"""U^2-Net's input square. Not tunable -- the exported graph is fixed at 320."""

TEXTURE_EDGE: Final[int] = 384
"""Longest side of the silhouette-clipped thumbnail the 照片 style stores.

The model looks at 320 and the page draws her into a 420x640 window, so 384 keeps
the texture from being the softest thing on screen while holding one figure to a
few hundred kilobytes of PNG.
"""

WORK_EDGE: Final[int] = 512
"""Longest side of the mask the boundary is traced on.

Tracing on the original photo (1000-1440 px) buys resolution Douglas-Peucker
throws away a moment later, and costs the flood fill an order of magnitude.
512 is where the flood fill still runs in tens of milliseconds.
"""

REFINE_BELOW: Final[float] = 0.55
"""Refine when the subject fills less than this share of the frame.

This is the fix for the complaint that started the round: a cat sitting in the
middle of a 1111x888 photo occupies 12% of it, so at the model's fixed 320x320
input the animal is ~110 px tall -- ears, whiskers and the tail tip are smaller
than one mask pixel before anyone simplifies them. Cropping to the subject and
cutting a second time puts the same animal across the full 320, which is where
the shape starts to read as a cat instead of as a blob. A subject that already
fills the frame gains nothing, so the second pass is skipped for it.
"""

REFINE_MARGIN: Final[float] = 0.09
"""How much of the crop's own size to add around the box, so ear tips survive."""

MEAN: Final[tuple[float, float, float]] = (0.485, 0.456, 0.406)
STD: Final[tuple[float, float, float]] = (0.229, 0.224, 0.225)
"""ImageNet normalisation in RGB order -- see the IoU note in the module docstring."""

MASK_CUT: Final[float] = 0.5
"""Probability at which a pixel starts counting as subject."""

MIN_COVERAGE: Final[float] = 0.012
"""Below this share of the frame the "subject" is a smudge, not a pet."""

MAX_PHOTO_BYTES: Final[int] = 12_000_000
"""Ceiling on the encoded image. The page resizes before sending; this is the door."""

CLOSE_KERNEL: Final[int] = 5
"""Morphological closing radius in mask pixels.

It stays at 5 because it is what seals the 1-2 px filaments a bilinear upsample of a
320px map leaves behind -- and those filaments are what make a boundary walk slip
into a lobe and come back through the start pixel. What made the ears disappear was
never this radius but the resolution the subject was traced at: on a 360px mask of a
photo where the cat filled 12% of the frame, an ear *is* a few pixels, and a
five-pixel closing eats it. Cropping to the subject first (see :data:`REFINE_BELOW`)
puts the same ear across ~3x more mask pixels, so the closing that keeps the
boundary traceable no longer costs the shape."""

MIN_POINTS: Final[int] = 8
MAX_POINTS: Final[int] = 420

# The four lattice directions, screen coordinates: east, south, west, north, with
# ``y`` growing downward. See :func:`_trace` for why the walk runs on the corners
# between pixels rather than on the pixels themselves.
_DIRS: Final[tuple[Pixel, ...]] = ((1, 0), (0, 1), (-1, 0), (0, -1))


@dataclass(frozen=True, slots=True)
class Outline:
    """The final polygon: what gets stored, and what the page extrudes."""

    points: tuple[Point, ...]
    aspect: float
    """Width over height. A cat sitting upright is near 0.7, a lying dog near 1.8."""

    coverage: float
    """Share of the photo the subject filled -- the only hint of "is this my pet"."""

    tolerance: float
    """The simplification tolerance this shape was cut at, in units of figure height."""

    def to_dict(self) -> dict[str, object]:
        """JSON-ready. Rounding happens here, not at the trace site."""
        return {
            "points": [[round(x, 4), round(y, 4)] for x, y in self.points],
            "aspect": round(self.aspect, 4),
            "coverage": round(self.coverage, 4),
            "tolerance": round(self.tolerance, 5),
        }

    @classmethod
    def from_dict(cls, raw: object) -> Outline | None:
        """Inverse of :meth:`to_dict`, or ``None`` for anything unusable.

        Deliberately not raising: this reads a file a person may have half-edited,
        and a library that throws on one bad row hides the nine good ones behind it.
        """
        if not isinstance(raw, dict):
            return None
        points = raw.get("points")
        if not isinstance(points, list) or len(points) < MIN_POINTS:
            return None
        cleaned: list[Point] = []
        for pair in points:
            if not isinstance(pair, (list, tuple)) or len(pair) != 2:
                return None
            if not all(isinstance(value, (int, float)) for value in pair):
                return None
            cleaned.append((float(pair[0]), float(pair[1])))
        return cls(
            points=tuple(cleaned),
            aspect=float(raw.get("aspect") or 1.0),
            coverage=float(raw.get("coverage") or 0.0),
            tolerance=float(raw.get("tolerance") or 0.0),
        )


@dataclass(frozen=True, slots=True)
class Cut:
    """A traced boundary in mask pixels, before anybody picks a line count.

    Separate from :class:`Outline` because one segmentation costs 230 ms while four
    candidates want the same boundary at four tolerances: holding the trace makes
    "generate" one pass instead of four.
    """

    raw: tuple[Pixel, ...]
    coverage: float
    height_px: int
    refined: bool = False
    """Whether the subject was cropped and cut a second time."""
    source: Any = None
    """The image the last pass looked at, at its own resolution.

    Carried because two other things are cut from it -- the silhouette-clipped
    thumbnail for the 照片 style and the average colour for the 卡通 one -- and both
    have to agree with the boundary vertex for vertex. Recomputing the crop later
    would mean re-running the model to get pixels that are already in hand.
    """
    mask: Any = None
    """The thresholded mask the boundary was traced on, same frame as ``source``."""

    def box(self) -> tuple[int, int, int, int]:
        """The traced region's box in mask pixels: (left, top, right, bottom)."""
        xs = [point[0] for point in self.raw]
        ys = [point[1] for point in self.raw]
        return (min(xs), min(ys), max(xs) + 1, max(ys) + 1)

    """Height of the traced blob in mask pixels -- what a tolerance is scaled by."""

    def outline(self, fraction: float) -> Outline:
        """Simplify at ``fraction`` of the figure's height and normalise to 1.0 tall.

        The tolerance is a *fraction* rather than a pixel count because the four
        candidates have to look like four line densities on a hamster and on a horse
        alike; a fixed pixel count would be mush on one and jitter on the other.
        """
        if len(self.raw) < MIN_POINTS:
            raise VisionError("轮廓太简单，做不出形状", details={"points": len(self.raw)})
        tolerance = max(0.6, float(fraction) * max(1, self.height_px))
        points = _simplify_ring(self.raw, tolerance)
        if len(points) < MIN_POINTS:
            points = _simplify_ring(self.raw, tolerance / 4.0)
        while len(points) > MAX_POINTS:
            # A boundary with a lot of small lobes can arrive here with thousands of
            # vertices. Erroring out would be a photo that "cannot be made into a
            # pet" for no reason the operator can see, so the tolerance is tightened
            # until the shape fits the budget the page is built for.
            tolerance *= 1.6
            points = _simplify_ring(self.raw, tolerance)
        xs = [float(point[0]) for point in points]
        ys = [float(point[1]) for point in points]
        left, right = min(xs), max(xs)
        top, bottom = min(ys), max(ys)
        height = max(1.0, bottom - top)
        middle = (left + right) / 2.0
        normalised = tuple(((x - middle) / height, (bottom - y) / height) for x, y in points)
        return Outline(
            points=normalised,
            aspect=(right - left) / height,
            coverage=self.coverage,
            tolerance=float(fraction),
        )


def weights_gap() -> str:
    """Why the cut-out cannot run here, or ``""`` when it can.

    The UI asks before it lets anybody choose a photo. Failing only after the pick
    would read as "this feature is broken", when the truth is "one file is missing
    next to the program" -- which is also the fix, and belongs in the message.
    """
    if WEIGHTS_FILE.is_file():
        return ""
    return f"抠图模型不在 {WEIGHTS_FILE}"


def segment(photo: bytes, *, work_edge: int = WORK_EDGE, segmenter: Segmenter | None = None) -> Cut:
    """Decode a photo and return the outline of its subject, in mask pixels.

    Raises:
        VisionError: the bytes are not a usable image, the runtime or the weights
            are missing, or no subject above :data:`MIN_COVERAGE` was found. Each
            message names which, because the operator reads it in the panel.
    """
    image = _decode(photo)
    run = segmenter or _onnx_segmenter
    first, coverage = _thresholded(image, run, work_edge)
    if coverage < MIN_COVERAGE:
        raise VisionError(
            "照片里找不出主体（背景太乱，或者整张就是一块颜色）",
            details={"coverage": round(coverage, 4), "min": MIN_COVERAGE},
        )
    mask, working = first, image
    subject = _largest_blob(mask)[0]
    refined = False
    if coverage < REFINE_BELOW:
        crop = _cropped(image, _subject_box(subject))
        if crop is not None:
            mask, _ = _thresholded(crop, run, work_edge)
            working = crop
            subject = _largest_blob(mask)[0]
            refined = True
    boundary = _trace(subject)
    if len(boundary) < MIN_POINTS:
        raise VisionError("主体轮廓太小，做不出形状", details={"points": len(boundary)})
    logger.info(
        "剪影：占画面 %.1f%%%s，描在 %sx%s 上，边界 %s 点",
        coverage * 100.0,
        "（裁了再抠一遍）" if refined else "（主体够大，一遍就够）",
        subject.shape[1],
        subject.shape[0],
        len(boundary),
    )
    ys = [point[1] for point in boundary]
    return Cut(
        raw=boundary,
        coverage=coverage,
        height_px=max(1, max(ys) - min(ys)),
        refined=refined,
        source=working,
        mask=mask,
    )


def _thresholded(image: Any, run: Segmenter, work_edge: int) -> tuple[Any, float]:
    """One segmentation pass: the mask at work scale, and how much it covers.

    Coverage is the whole thresholded map, not just the largest region -- it is
    measured *before* anyone decides which blob is the pet, and a photo whose map is
    mostly noise should not look like a well-filled subject just because one blob is
    tidy.
    """
    import numpy
    from PIL import Image, ImageFilter

    width, height = image.size
    scale = float(work_edge) / float(max(width, height))
    work_width = max(16, round(width * scale))
    work_height = max(16, round(height * scale))
    probabilities = run(_blob(image), MODEL_EDGE, MODEL_EDGE)
    soft = numpy.frombuffer(probabilities, dtype=numpy.uint8).reshape(MODEL_EDGE, MODEL_EDGE)
    mask_image = Image.fromarray(soft, mode="L").resize(
        (work_width, work_height), Image.Resampling.BILINEAR
    )
    closed = mask_image.filter(ImageFilter.MaxFilter(CLOSE_KERNEL)).filter(
        ImageFilter.MinFilter(CLOSE_KERNEL)
    )
    mask = numpy.asarray(closed, dtype=numpy.uint8) >= int(MASK_CUT * 255)
    return mask, float(mask.sum()) / float(work_width * work_height)


def _subject_box(subject: Any) -> tuple[float, float, float, float] | None:
    """The largest region's bounding box, normalised to the frame.

    The largest region and not the whole mask: one speck of background in the opposite
    corner would widen the crop to the entire photo, and the second pass would then
    buy exactly nothing -- which is the failure this function exists to avoid.
    """
    import numpy

    rows = numpy.flatnonzero(subject.any(axis=1))
    if rows.size == 0:
        return None
    columns = numpy.flatnonzero(subject.any(axis=0))
    height, width = subject.shape
    return (
        float(columns[0]) / width,
        float(rows[0]) / height,
        float(columns[-1] + 1) / width,
        float(rows[-1] + 1) / height,
    )


def _cropped(image: Any, box: tuple[float, float, float, float] | None) -> Any:
    """Cut the photo to the subject, plus a margin so ear and tail tips stay inside.

    ``None`` when the crop would buy nothing -- the box already covers the frame, or
    it is too small for the model to look at. The caller keeps the first pass.
    """
    if box is None:
        return None
    width, height = image.size
    left, top, right, bottom = box
    grow_w = (right - left) * REFINE_MARGIN
    grow_h = (bottom - top) * REFINE_MARGIN
    x0 = max(0, int((left - grow_w) * width))
    y0 = max(0, int((top - grow_h) * height))
    x1 = min(width, int((right + grow_w) * width))
    y1 = min(height, int((bottom + grow_h) * height))
    if x1 - x0 < 64 or y1 - y0 < 64:
        return None
    if x1 - x0 >= 0.92 * width and y1 - y0 >= 0.92 * height:
        return None
    return image.crop((x0, y0, x1, y1))


def _simplify_ring(ring: Sequence[Pixel], tolerance: float) -> tuple[Pixel, ...]:
    """Douglas-Peucker on a *closed* outline, split at two opposite extremes.

    Running DP on the ring as if it were a polyline pins the first and last vertex
    against each other -- and on a crack walk those are the same pixel, or two
    neighbouring ones. A baseline that short carries no direction, so the recursion
    splits by "distance from that one point" and the result stops being monotone in
    the tolerance: measured here, the finest rung came back with *fewer* vertices than
    the coarse one. Splitting at the leftmost and rightmost vertices gives two chains
    whose endpoints are as far apart as the shape allows, which is what DP assumes.
    """
    if len(ring) < 4:
        return tuple(ring)
    points = ring[:-1] if ring[0] == ring[-1] else tuple(ring)
    if len(points) < 4:
        return tuple(points)
    left = min(range(len(points)), key=lambda index: points[index][0])
    right = max(range(len(points)), key=lambda index: points[index][0])
    if left == right:  # a vertical sliver: split on y instead
        left = min(range(len(points)), key=lambda index: points[index][1])
        right = max(range(len(points)), key=lambda index: points[index][1])
    first, second = (left, right) if left < right else (right, left)
    chain_a = points[first : second + 1]
    chain_b = tuple(points[second + 1 :]) + tuple(points[: first + 1])
    return simplify(chain_a, tolerance) + simplify(chain_b, tolerance)[1:-1]


def simplify(points: Sequence[Pixel], tolerance: float) -> tuple[Pixel, ...]:
    """Douglas-Peucker, iterative, endpoints pinned.

    The trace visits every pixel of the boundary -- a few hundred points of ±0.5 px
    jitter on a 360px mask. Extruding that produces a wireframe whose lines *are*
    the jitter, so this is the step that decides whether the pet reads as a drawn
    outline or as static. It is also the knob the candidates vary on.
    """
    if len(points) < 3 or tolerance <= 0:
        return tuple(points)
    keep = [False] * len(points)
    keep[0] = keep[-1] = True
    stack = deque([(0, len(points) - 1)])
    while stack:
        first, last = stack.pop()
        if last <= first + 1:
            continue
        ax, ay = points[first]
        bx, by = points[last]
        dx, dy = bx - ax, by - ay
        span = float(dx * dx + dy * dy)
        best = -1.0
        index = -1
        for candidate in range(first + 1, last):
            cx, cy = points[candidate]
            if span == 0.0:
                distance = ((cx - ax) ** 2 + (cy - ay) ** 2) ** 0.5
            else:
                along = max(0.0, min(1.0, ((cx - ax) * dx + (cy - ay) * dy) / span))
                distance = ((cx - (ax + along * dx)) ** 2 + (cy - (ay + along * dy)) ** 2) ** 0.5
            if distance > best:
                best, index = distance, candidate
        if best > tolerance and index >= 0:
            keep[index] = True
            stack.append((first, index))
            stack.append((index, last))
    return tuple(point for point, flag in zip(points, keep, strict=True) if flag)


def _ink(colour: str) -> tuple[int, int, int]:
    """The picker's ink: the skin's cyan, or the cartoon's sampled fur colour."""
    text = str(colour or "").strip().lstrip("#")
    if len(text) != 6:
        return (120, 190, 235)
    try:
        return tuple(int(text[index : index + 2], 16) for index in (0, 2, 4))  # type: ignore[return-value]
    except ValueError:
        return (120, 190, 235)


def _bright(ink: tuple[int, int, int]) -> tuple[int, int, int]:
    """The near-side outline, one notch brighter than the far side."""
    return tuple(min(255, value + 40) for value in ink)  # type: ignore[return-value]


def preview_png(outline: Outline, *, depth: float, box: int = 150, colour: str = "") -> bytes:
    """A wireframe render of *this* geometry, as PNG bytes.

    Why not a screenshot of the real thing: the page is the only renderer, it lives
    in another window, and four previews would mean four hidden WebGL contexts for
    a panel that is open for ten seconds. A slab seen from a fixed three-quarter
    angle is three PIL calls instead -- and it is the same polygon, the same depth,
    the same number of lines, so what the picker shows is what lands on the desktop.

    ``colour`` tints the fill for the 卡通 style; empty keeps the instrument cyan.
    """
    from PIL import Image, ImageDraw

    ink = _ink(colour)

    height = max(40, int(box))
    pad = max(3.0, height * 0.05)
    back_dx = 0.46 * max(0.0, depth)
    back_dy = 0.22 * max(0.0, depth)
    unit = (height - 2 * pad) / (1.0 + back_dy)
    ink_x = unit * (outline.aspect + back_dx)
    width = max(40, round(ink_x + 2 * pad))
    shift_x = pad + (width - ink_x) / 2.0
    shift_y = pad + unit * back_dy / 2.0

    def place(x: float, y: float, climb: float = 0.0, lift: float = 0.0) -> tuple[float, float]:
        return (
            shift_x + (x + outline.aspect / 2.0 + climb) * unit,
            shift_y + (1.0 - y - lift) * unit,
        )

    front = [place(x, y) for x, y in outline.points]
    behind = [place(x, y, climb=back_dx, lift=back_dy) for x, y in outline.points]
    canvas = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(canvas)
    every = max(1, len(front) // 14)
    for index in range(0, len(front), every):
        draw.line([front[index], behind[index]], fill=(*ink, 110), width=1)
    draw.polygon(behind, outline=(*ink, 105), width=1)
    draw.line(
        [*front, front[0]],
        fill=(*_bright(ink), 235),
        width=2,
        joint="curve",
    )
    buffer = io.BytesIO()
    canvas.save(buffer, format="PNG")
    return buffer.getvalue()


def _decode(photo: bytes) -> Any:
    from PIL import Image

    if not photo:
        raise VisionError("照片是空的")
    if len(photo) > MAX_PHOTO_BYTES:
        raise VisionError(
            f"照片太大（{len(photo) // 1024 // 1024} MB，"
            f"上限 {MAX_PHOTO_BYTES // 1024 // 1024} MB）",
            details={"bytes": len(photo), "limit": MAX_PHOTO_BYTES},
        )
    try:
        image = Image.open(io.BytesIO(photo))
        image.load()
    except Exception as exc:
        raise VisionError(
            f"这张照片读不出来：{type(exc).__name__}: {exc}",
            details={"bytes": len(photo)},
        ) from exc
    if image.width < 32 or image.height < 32:
        raise VisionError(
            "照片太小（至少 32x32）",
            details={"width": image.width, "height": image.height},
        )
    return image.convert("RGB")


def _blob(image: Any) -> Any:
    """The 320x320 float32 input, shape (1, 3, edge, edge), RGB, ImageNet-normalised."""
    import numpy
    from PIL import Image

    small = image.resize((MODEL_EDGE, MODEL_EDGE), Image.Resampling.BILINEAR)
    pixels = numpy.asarray(small, dtype=numpy.float32)[..., :3] / 255.0
    mean = numpy.array(MEAN, dtype=numpy.float32).reshape(1, 1, 3)
    std = numpy.array(STD, dtype=numpy.float32).reshape(1, 1, 3)
    data = (pixels - mean) / std
    return numpy.ascontiguousarray(numpy.transpose(data, (2, 0, 1))[None])


def _onnx_segmenter(blob: Any, width: int, height: int) -> bytes:
    """Run the weights; return the probability map as 320x320 bytes of 0-255.

    The session is built per call and dropped with it. Loading costs a few hundred
    milliseconds, while keeping it alive would pin tens of megabytes of native
    memory in a 16 GB machine that already carries a speech model, a TTS worker and
    a WebView2 -- for a feature a person uses once an hour at most.
    """
    import numpy
    import onnxruntime

    gap = weights_gap()
    if gap:
        raise VisionError(gap, details={"weights": str(WEIGHTS_FILE)})
    try:
        session = onnxruntime.InferenceSession(
            str(WEIGHTS_FILE), providers=["CPUExecutionProvider"]
        )
    except Exception as exc:
        raise VisionError(
            f"抠图模型加载失败：{type(exc).__name__}: {exc}",
            details={"weights": str(WEIGHTS_FILE)},
        ) from exc
    name = session.get_inputs()[0].name
    output = session.run(None, {name: blob})[0]
    soft = numpy.clip(numpy.asarray(output, dtype=numpy.float32)[0, 0], 0.0, 1.0)
    painted: bytes = (soft * 255.0).astype(numpy.uint8).tobytes()
    return painted


def _largest_blob(mask: Any) -> tuple[Any, int]:
    """Keep the biggest 8-connected region; the rest is background fooling the model.

    Scanline flood fill: the per-pixel version on a 360x360 mask is a few hundred
    thousand Python iterations, which would be longer than the segmentation itself.
    """
    import numpy

    height, width = mask.shape
    visited = numpy.zeros((height, width), dtype=bool)
    best: Any = None
    best_count = 0
    for start_y in range(height):
        for seed in numpy.flatnonzero(mask[start_y] & ~visited[start_y]):
            if visited[start_y, seed]:
                continue
            region, count = _flood(mask, visited, start_y, int(seed))
            if count > best_count:
                best, best_count = region, count
    if best is None or not best_count:
        raise VisionError("照片里没有主体", details={"coverage": 0.0})
    return best, best_count


def _flood(mask: Any, visited: Any, row: int, column: int) -> tuple[Any, int]:
    import numpy

    height, width = mask.shape
    region = numpy.zeros((height, width), dtype=bool)
    stack = deque([(row, column)])
    count = 0
    while stack:
        y, x = stack.pop()
        if visited[y, x] or not mask[y, x]:
            continue
        left = x
        while left > 0 and mask[y, left - 1] and not visited[y, left - 1]:
            left -= 1
        right = x
        while right + 1 < width and mask[y, right + 1] and not visited[y, right + 1]:
            right += 1
        for pixel in range(left, right + 1):
            if not visited[y, pixel]:
                visited[y, pixel] = True
                region[y, pixel] = True
                count += 1
        for neighbour in (y - 1, y + 1):
            if 0 <= neighbour < height:
                for pixel in range(left, right + 1):
                    if mask[neighbour, pixel] and not visited[neighbour, pixel]:
                        stack.append((neighbour, pixel))
    return region, count


def _trace(mask: Any) -> tuple[Pixel, ...]:
    """The outer boundary of ``mask``, walked along the cracks between pixels.

    This replaced a pixel-hopping Moore trace, and the reason is measured rather than
    theoretical: the area enclosed by the traced polygon against the number of filled
    pixels was **1.00** here and 0.00-0.04 for the cartoon templates, 0.73 for one of
    three real photos. A walk that steps from pixel centre to pixel centre has to
    decide what to do at a pinch -- an ear tip, the neck where a tail meets a body --
    and every rule for that is wrong some of the time: it turns back and closes a loop
    around the lobe it just visited, and the rest of the animal is simply not in the
    outline. Walking the *edges between* pixels with the filled side kept on the right
    has no such decision to make; the boundary of a union of squares is a closed
    polyline, and this walks it.

    Holes (the gap inside a curled tail) come out filled. For a silhouette that is the
    answer we want anyway -- a slab with a hole in it is a slab that shows the desktop
    through the cat.
    """
    import numpy

    ys, xs = numpy.nonzero(mask)
    if ys.size == 0:  # pragma: no cover - callers check coverage first
        raise VisionError("照片里没有主体", details={"points": 0})
    first_y = int(ys.min())
    start_x = int(xs[ys == first_y].min())
    point: Pixel = (start_x, first_y)
    heading = 0  # east: along the top edge of that first pixel
    path: list[Pixel] = [point]
    seen: set[tuple[Pixel, int]] = set()
    for _ in range(8 * mask.shape[0] * mask.shape[1]):
        state = (point, heading)
        if state in seen:
            break
        seen.add(state)
        for turn in (1, 0, 3, 2):  # right, straight, left, back
            direction = (heading + turn) % 4
            if not _edge_is_boundary(mask, point, direction):
                continue
            dx, dy = _DIRS[direction]
            point = (point[0] + dx, point[1] + dy)
            heading = direction
            path.append(point)
            break
        else:
            break
    return tuple(path)


def _edge_is_boundary(mask: Any, point: Pixel, direction: int) -> bool:
    """Is this lattice edge a boundary, with the fill on the right of travel?

    The four cases are written out rather than derived from a rotation matrix: which
    of the two cells flanking an edge is "on the right" is exactly the kind of thing
    that reads fine in a loop and walks the outline inside-out when it is off by one.
    """
    x, y = point
    dx, dy = _DIRS[direction]
    if dx == 1:  # east:  right hand is the cell below, left is the cell above
        return _filled(mask, x, y) and not _filled(mask, x, y - 1)
    if dx == -1:  # west:  right hand is the cell above-left, left is below-left
        return _filled(mask, x - 1, y - 1) and not _filled(mask, x - 1, y)
    if dy == 1:  # south: right hand is the cell to the west, left is to the east
        return _filled(mask, x - 1, y) and not _filled(mask, x, y)
    return _filled(mask, x, y - 1) and not _filled(mask, x - 1, y - 1)  # north


def _filled(mask: Any, x: int, y: int) -> bool:
    """Is this cell part of the subject? Outside the frame counts as empty."""
    return 0 <= y < mask.shape[0] and 0 <= x < mask.shape[1] and bool(mask[y, x])


def cut_from_mask(mask: Any, *, coverage: float = 1.0, source: Any = None) -> Cut:
    """A :class:`Cut` from a mask somebody else drew -- the cartoon templates go this way.

    Routing hand-drawn shapes through the same function a photo goes through is the
    point: they get the same boundary walk, the same simplification, the same
    normalisation. A template that bypassed it would need its own camera framing, its
    own preview, and its own set of bugs.
    """
    import numpy

    subject, _pixels = _largest_blob(numpy.asarray(mask, dtype=bool))
    boundary = _trace(subject)
    ys = [point[1] for point in boundary]
    return Cut(
        raw=boundary,
        coverage=coverage,
        height_px=max(1, max(ys) - min(ys)),
        refined=False,
        source=source,
        mask=subject,
    )


def thumbnail(cut: Cut, edge: int = TEXTURE_EDGE) -> bytes:
    """The photo, clipped to the silhouette, as an RGBA PNG.

    The frame is the **boundary's own box**, not the crop's: the polygon the page
    extrudes is normalised to that same box, so a texture aligned to anything else
    slides off the face of the slab -- a cat whose stripes sit half a head to the left
    is worse than no stripes at all.

    This is the file that makes the 照片 style cost something: it is derived from
    somebody's photograph and it stays on disk until the figure is deleted. The picker
    says so before the photo is chosen, and ``PetLibrary.remove`` takes it with the row.
    """
    import numpy
    from PIL import Image

    if cut.source is None or cut.mask is None:
        raise VisionError("这条轮廓没有留下原图，贴不了照片")
    mask = cut.mask
    left, top, right, bottom = cut.box()
    scale_x = cut.source.width / mask.shape[1]
    scale_y = cut.source.height / mask.shape[0]
    region = cut.source.crop(
        (
            max(0, int(left * scale_x)),
            max(0, int(top * scale_y)),
            min(cut.source.width, int(right * scale_x)),
            min(cut.source.height, int(bottom * scale_y)),
        )
    )
    crop_mask = mask[top : bottom + 1, left : right + 1]
    ratio = min(1.0, float(edge) / float(max(1, max(crop_mask.shape))))
    size = (
        max(8, round(crop_mask.shape[1] * ratio)),
        max(8, round(crop_mask.shape[0] * ratio)),
    )
    alpha = Image.fromarray((crop_mask * 255).astype(numpy.uint8), mode="L").resize(
        size, Image.Resampling.BILINEAR
    )
    picture = region.resize(size, Image.Resampling.LANCZOS).convert("RGB")
    picture.putalpha(alpha)
    buffer = io.BytesIO()
    picture.save(buffer, format="PNG")
    return buffer.getvalue()


def average_colour(cut: Cut) -> str:
    """The subject's mean colour as ``#rrggbb`` -- all the 卡通 style takes from a photo.

    The *subject* and not the frame, because a cat on a white sofa would otherwise come
    back cream. And deliberately one number: the template's shape is authored rather
    than estimated, so this style keeps the promise the other two cannot -- no image
    derived from somebody's photograph is written to disk.
    """
    import numpy
    from PIL import Image

    if cut.source is None or cut.mask is None:
        return "#8fd6ff"
    mask = cut.mask
    small = cut.source.convert("RGB").resize(
        (mask.shape[1], mask.shape[0]), Image.Resampling.LANCZOS
    )
    pixels = numpy.asarray(small, dtype=numpy.float32)
    weights = mask.astype(numpy.float32)[:, :, None]
    total = float(weights.sum())
    if total <= 0:  # pragma: no cover - the caller checks coverage first
        return "#8fd6ff"
    mean = (pixels * weights).sum(axis=(0, 1)) / total
    tint = [max(0, min(255, int(float(value) + 0.5))) for value in mean]
    return "#{:02x}{:02x}{:02x}".format(*tint)
