"""Photo → outline, with the neural part swapped out.

The weights are 4.57 MB and take ~230 ms; the geometry around them is pure numpy
and PIL and takes microseconds. Everything below therefore injects a fake
segmenter that paints a known mask, which is the only way to assert "the trace
went around the big blob and not the small one" without hoping a real network
feels like cooperating today. What the real weights actually do is measured in
``.r60-verify/probe_silhouette.py`` and quoted in the module docstring.
"""

from __future__ import annotations

import io
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from jarvis.core.exceptions import VisionError
from jarvis.vision import silhouette
from jarvis.vision.silhouette import Outline, preview_png, segment, simplify, weights_gap

MASK_EDGE = 320


def _photo(width: int = 200, height: int = 300) -> bytes:
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (width, height), (30, 40, 50)).save(buffer, format="PNG")
    return buffer.getvalue()


def _painter(shape: str) -> Callable[[Any, int, int], bytes]:
    """A segmenter that returns a hard-edged mask instead of a learned one."""
    import numpy

    def run(blob: Any, width: int, height: int) -> bytes:
        canvas = numpy.zeros((height, width), dtype=numpy.uint8)
        yy, xx = numpy.mgrid[0:height, 0:width]
        if shape == "circle":
            canvas[
                (xx - width / 2) ** 2 + (yy - height / 2) ** 2 < (min(width, height) // 3) ** 2
            ] = 255
        elif shape == "square":
            canvas[height // 4 : 3 * height // 4, width // 4 : 3 * width // 4] = 255
        elif shape == "top":
            canvas[0 : height // 8, width // 4 : 3 * width // 4] = 255
        elif shape == "spike":
            # A body with a one-pixel filament sticking up from it. The filament's tip
            # is the topmost-leftmost pixel, so a boundary walk that stops the first
            # time it comes back there traces the filament and nothing else -- which is
            # exactly what happened on a real photo with 22 points of a 360-pixel body.
            canvas[height // 2 : 3 * height // 4, width // 4 : 3 * width // 4] = 255
            canvas[height // 8 : height // 2, width // 3] = 255
        elif shape == "two":
            # A small blob starting on row 8 and a big one starting on row 40: the
            # scan meets the small one first, so "keep the largest" is the only
            # thing that can make the trace come out on the right.
            canvas[height // 16 : height // 8, 0 : width // 16] = 255
            canvas[height // 8 : 7 * height // 8, width // 2 : 7 * width // 8] = 255
        return canvas.tobytes()

    return run


# -- the trace ---------------------------------------------------------------


@pytest.mark.parametrize("shape", ["circle", "square"])
def test_a_subject_bigger_than_the_floor_is_accepted(shape: str) -> None:
    cut = segment(_photo(), segmenter=_painter(shape))

    assert len(cut.raw) >= silhouette.MIN_POINTS
    assert cut.coverage > silhouette.MIN_COVERAGE
    assert cut.height_px > 0


def test_the_biggest_region_wins_when_the_background_fools_the_model() -> None:
    """Two blobs: the trace must belong to the larger one, not the first one found."""
    cut = segment(_photo(), segmenter=_painter("two"))
    xs = [point[0] for point in cut.raw]
    ys = [point[1] for point in cut.raw]
    width = max(xs) - min(xs)
    height = max(ys) - min(ys)

    # The trace is in the *crop's* frame once the subject is cut a second time, so
    # this asks a frame-independent question: the traced region is tall and narrow,
    # which only the 120x240 blob is. The 40x20 one in the corner is the other way round.
    assert height > 2.5 * width, (width, height)
    assert cut.refined is True
    # 24000/102400 = 0.234 of the map; the bilinear resize and the closing both
    # inflate a hard edge, so this is a band, not a number.
    assert 0.20 < cut.coverage < 0.35


def test_a_photo_with_nothing_in_it_is_refused_not_emptied() -> None:
    with pytest.raises(VisionError) as raised:
        segment(_photo(), segmenter=_painter("empty"))

    assert "主体" in str(raised.value)


@pytest.mark.parametrize(
    ("photo", "wanted"),
    [
        (b"", "空的"),
        (b"not an image at all", "读不出来"),
        (_photo(8, 8), "太小"),
    ],
)
def test_a_photo_that_cannot_be_used_says_which_kind_of_cannot(photo: bytes, wanted: str) -> None:
    with pytest.raises(VisionError) as raised:
        segment(photo, segmenter=_painter("circle"))

    assert wanted in str(raised.value)


def test_an_oversized_file_is_refused_before_anything_is_decoded() -> None:
    with pytest.raises(VisionError, match="太大"):
        segment(b"0" * (silhouette.MAX_PHOTO_BYTES + 1), segmenter=_painter("circle"))


# -- normalisation -----------------------------------------------------------


def test_the_outline_is_one_unit_tall_centred_and_measured_upwards() -> None:
    cut = segment(_photo(), segmenter=_painter("top"))
    outline = cut.outline(0.02)

    assert max(y for _, y in outline.points) == pytest.approx(1.0, abs=1e-6)
    assert min(y for _, y in outline.points) == pytest.approx(0.0, abs=1e-6)
    assert (
        max(x for x, _ in outline.points) + min(x for x, _ in outline.points)
    ) / 2 == pytest.approx(0.0, abs=1e-6)
    assert outline.aspect > 2.0, "a wide flat blob must stay wide"


def test_the_first_traced_pixel_is_at_the_top_and_the_first_vertex_is_at_the_ceiling() -> None:
    """The trace runs in image rows (y downward); the polygon must not inherit that."""
    cut = segment(_photo(), segmenter=_painter("top"))

    assert cut.raw[0][1] < 8, "the trace starts on the topmost row"
    assert cut.outline(0.02).points[0][1] > 0.9, "the first vertex must be at the top"


# -- simplification ----------------------------------------------------------


def test_a_looser_tolerance_leaves_fewer_points_and_still_closes() -> None:
    cut = segment(_photo(), segmenter=_painter("circle"))
    fine = cut.outline(0.004)
    coarse = cut.outline(0.06)

    assert len(coarse.points) < len(fine.points)
    assert len(coarse.points) >= silhouette.MIN_POINTS
    assert coarse.points[0] != coarse.points[-1], "a closed ring is not two coincident points"


def test_simplify_pins_both_endpoints_and_drops_the_bulge() -> None:
    points = [(0, 0)] + [(step, 1) for step in range(1, 10)] + [(10, 0)]

    kept = simplify(points, 3.0)

    assert kept[0] == (0, 0)
    assert kept[-1] == (10, 0)
    assert len(kept) < len(points)


def test_simplify_keeps_a_bulge_that_the_tolerance_cannot_cross() -> None:
    shallow = [(0, 0), (1, 1), (2, 0)]
    deep = [(0, 0), (1, 9), (2, 0)]

    assert simplify(shallow, 5.0) == ((0, 0), (2, 0)), "1 px of wobble is not a corner"
    assert simplify(deep, 5.0) == tuple(deep)
    assert simplify(shallow, 0.0) == tuple(shallow), "a zero tolerance must not delete"


# -- cutting a second time ---------------------------------------------------


def test_a_small_subject_is_cropped_and_segmented_again() -> None:
    """The fix for "根本没有描绘出猫咪的形状": a cat in 12% of the frame is ~110 px
    tall to the model, which is not enough to keep an ear. Cropping to the subject and
    cutting again is what puts it across the full 320."""
    calls: list[int] = []
    seen: list[Any] = []

    def counting(blob: Any, width: int, height: int) -> bytes:
        calls.append(width)
        seen.append(blob)
        return _painter("top")(blob, width, height)

    cut = segment(_photo(1200, 900), segmenter=counting)

    assert len(calls) == 2, "the second pass is the whole point"
    assert cut.refined is True
    # Both passes look at the same model edge; the second one is fed a crop.
    assert calls == [MASK_EDGE, MASK_EDGE]


def test_a_subject_that_already_fills_the_frame_is_cut_once() -> None:
    calls: list[int] = []

    def filling(blob: Any, width: int, height: int) -> bytes:
        import numpy

        calls.append(width)
        return numpy.full(height * width, 255, dtype=numpy.uint8).tobytes()

    cut = segment(_photo(), segmenter=filling)

    assert len(calls) == 1, "refining a full-frame subject buys nothing"
    assert cut.refined is False


def test_a_one_pixel_filament_does_not_become_the_whole_outline() -> None:
    """The regression the operator actually reported, as a unit test.

    The walk starts at the filament's tip. "Back at the start means done" returns a
    stub of ~20 points and the pet comes out as a rounded blob; the real criterion is
    that the walk has to be *repeating* its own second point before it is finished.
    """
    cut = segment(_photo(), segmenter=_painter("spike"))
    xs = [point[0] for point in cut.raw]
    ys = [point[1] for point in cut.raw]

    assert max(xs) - min(xs) > 60, "只描到了那根细须，身子没走完"
    assert max(ys) - min(ys) > 60
    assert len(cut.raw) > 200


# -- the preview -------------------------------------------------------------


def test_the_preview_is_a_png_sized_to_the_figure_and_its_slab() -> None:
    outline = Outline(
        points=((-0.4, 0.0), (0.4, 0.0), (0.4, 1.0), (-0.4, 1.0)),
        aspect=0.8,
        coverage=0.3,
        tolerance=0.01,
    )

    png = preview_png(outline, depth=0.2)

    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    from PIL import Image

    flat, thick = Image.open(io.BytesIO(png)), Image.open(
        io.BytesIO(preview_png(outline, depth=0.9))
    )
    assert thick.width > flat.width, "a thicker slab has to be drawn wider"


# -- the weights door --------------------------------------------------------


def test_a_missing_weights_file_names_the_file_it_wants(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wanted = tmp_path / "data" / "u2netp.onnx"
    monkeypatch.setattr(silhouette, "WEIGHTS_FILE", wanted)

    assert str(wanted) in weights_gap()
    wanted.parent.mkdir(parents=True)
    wanted.write_bytes(b"x")
    assert weights_gap() == ""


def test_the_real_weights_are_where_the_package_says_they_are() -> None:
    """A spec line that stops matching this path is a broken install, not a test failure."""
    assert silhouette.WEIGHTS_FILE.is_file(), silhouette.WEIGHTS_FILE
    assert silhouette.WEIGHTS_FILE.stat().st_size > 4_000_000
