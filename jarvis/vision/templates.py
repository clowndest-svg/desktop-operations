"""Four hand-drawn shapes for the 卡通 style, as masks.

Why masks and not coordinate lists
----------------------------------
A cute animal is a union of circles and wedges, and a polygon soups that union by
hand is how you get a cat with a step where her cheek should curve. So these draw
the *shape* into a bitmap -- which is what a child draws, incidentally -- and
:func:`jarvis.vision.silhouette.cut_from_mask` walks the outline the same way a
photograph's does. Same simplification, same normalisation, same preview, so the
cartoon and the traced silhouette are the same kind of object to the page.

What the photo contributes
--------------------------
One colour (:func:`jarvis.vision.pet_service` samples the subject) and nothing else.
The shape is authored, so this is the one style of the three where no image derived
from somebody's photograph is written to disk.
"""

from __future__ import annotations

from typing import Final

__all__ = ["TEMPLATE_KINDS", "TEMPLATE_LABELS", "mask_for"]

TEMPLATE_EDGE: Final[int] = 320
"""The canvas these are drawn on. Square; the outline is normalised afterwards."""

TEMPLATE_KINDS: Final[tuple[str, ...]] = ("cat", "bird", "dog", "bun")
TEMPLATE_LABELS: Final[dict[str, str]] = {
    "cat": "坐猫",
    "bird": "团鸡",
    "dog": "垂耳狗",
    "bun": "团子",
}


def _disc(pixels: object, cx: float, cy: float, radius: float, edge: int) -> None:
    import numpy

    yy, xx = numpy.mgrid[0:edge, 0:edge]
    pixels[(xx - cx) ** 2 + (yy - cy) ** 2 <= radius * radius] = True  # type: ignore[index]


def _wedge(pixels: object, points: list[tuple[float, float]], edge: int) -> None:
    """A triangle, by filling it with PIL's polygon rasteriser.

    Doing it in numpy (three half-planes) is possible and not worth the reading;
    this runs once per generated figure on a 320px canvas.
    """
    from PIL import Image, ImageDraw

    overlay = Image.new("L", (edge, edge), 0)
    ImageDraw.Draw(overlay).polygon(points, fill=255)
    import numpy

    pixels |= numpy.asarray(overlay) > 127  # type: ignore[operator]


def mask_for(kind: str, edge: int = TEMPLATE_EDGE) -> object:
    """The mask for one template.

    Raises:
        ValueError: an unknown kind. The caller maps it to a message; a template
            that quietly fell back to "cat" would be a bug report nobody can read.
    """
    import numpy

    if kind not in TEMPLATE_KINDS:
        raise ValueError(f"没有这种形状：{kind}")
    pixels = numpy.zeros((edge, edge), dtype=bool)
    unit = edge / 100.0

    def at(percent: float) -> float:
        return percent * unit

    if kind == "cat":
        # Head with two triangular ears, a body that widens as it sits, and a tail
        # that comes up on the right and curls in.
        _disc(pixels, at(50), at(26), at(17), edge)
        _wedge(pixels, [(at(37), at(20)), (at(43), at(3)), (at(48), at(17))], edge)
        _wedge(pixels, [(at(63), at(20)), (at(57), at(3)), (at(52), at(17))], edge)
        _disc(pixels, at(50), at(66), at(26), edge)
        _disc(pixels, at(50), at(84), at(22), edge)
        # The tail has to overlap the body, not touch it: the outline keeps the
        # largest connected region, and a tail that only meets the haunch at a corner
        # is a tail that quietly disappears from the pet.
        _wedge(
            pixels,
            [(at(58), at(80)), (at(92), at(66)), (at(92), at(78)), (at(62), at(94))],
            edge,
        )
        _disc(pixels, at(88), at(70), at(9), edge)
    elif kind == "bird":
        # One round body, a crest, a beak, two feet: the chick shape, read as a
        # silhouette rather than as a drawing.
        _disc(pixels, at(50), at(52), at(30), edge)
        _disc(pixels, at(50), at(26), at(16), edge)
        _wedge(pixels, [(at(46), at(14)), (at(52), at(2)), (at(56), at(15))], edge)
        _wedge(pixels, [(at(64), at(26)), (at(80), at(30)), (at(64), at(34))], edge)
        _wedge(pixels, [(at(40), at(80)), (at(44), at(96)), (at(34), at(96))], edge)
        _wedge(pixels, [(at(58), at(80)), (at(64), at(96)), (at(52), at(96))], edge)
    elif kind == "dog":
        # A muzzle, two floppy ears hanging outside the head, a body on short legs.
        _disc(pixels, at(50), at(28), at(18), edge)
        _disc(pixels, at(68), at(32), at(9), edge)
        _wedge(pixels, [(at(33), at(14)), (at(24), at(44)), (at(38), at(40))], edge)
        _wedge(pixels, [(at(67), at(14)), (at(76), at(44)), (at(62), at(40))], edge)
        _disc(pixels, at(48), at(62), at(20), edge)
        _wedge(
            pixels, [(at(30), at(76)), (at(66), at(76)), (at(62), at(96)), (at(34), at(96))], edge
        )
        _wedge(pixels, [(at(64), at(60)), (at(86), at(40)), (at(80), at(66))], edge)
    else:
        # 团子: a ball with two tiny ears and nothing else to argue about.
        _disc(pixels, at(50), at(56), at(34), edge)
        _disc(pixels, at(30), at(28), at(11), edge)
        _disc(pixels, at(70), at(28), at(11), edge)
    return pixels
