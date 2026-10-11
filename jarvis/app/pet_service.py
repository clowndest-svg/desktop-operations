"""From one photo to four pets -- and the one the desktop actually shows.

Where the work is split
-----------------------
:mod:`jarvis.vision.silhouette` turns pixels into a polygon and knows nothing about
storage; :mod:`jarvis.app.pet_library` stores polygons and knows nothing about
images. This module is the transaction in between: it runs the photo through the
segmenter **once**, asks for four simplifications of that one boundary, writes four
figures, and remembers which of them (if any) the operator chose.

Once, not four times, because the pass costs ~230 ms on this machine's CPU while
the four simplifications together cost under 30 ms -- and because four independent
segmentations of the same photo would produce four slightly different animals,
which is not a choice anybody wants to make.

Why the answer is a polygon and not a mesh
------------------------------------------
The page extrudes the polygon (``ExtrudeGeometry``). Shipping a GLB instead would
mean writing a binary container here, parsing it there, and putting a 400-vertex
mesh on disk where 400 vertex pairs will do. The contour is the whole product;
the third dimension is arithmetic the renderer is better at than we are.
"""

from __future__ import annotations

import base64
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final

from jarvis.app import pet_gallery
from jarvis.app.pet_library import MAX_FIGURES, PetFigure, PetLibrary, PetLibraryError
from jarvis.app.preferences import PET_FIGURE, Preferences
from jarvis.core.exceptions import VisionError
from jarvis.vision.silhouette import (
    Cut,
    Outline,
    average_colour,
    cut_from_mask,
    preview_png,
    segment,
    thumbnail,
    weights_gap,
)
from jarvis.vision.templates import TEMPLATE_KINDS, TEMPLATE_LABELS, mask_for

logger = logging.getLogger("jarvis.app.pet_service")

__all__ = ["CANDIDATES", "STYLES", "PetService"]


@dataclass(frozen=True, slots=True)
class Reading:
    """One of the four figures a photo is turned into.

    Either it names a simplification of the traced outline (``template`` empty) or it
    names an authored shape to trace instead (``cartoon``). Both go through the same
    :class:`~jarvis.vision.silhouette.Outline`, which is why the page never learns
    which kind it was given.
    """

    label: str
    fraction: float
    """Simplification tolerance, as a fraction of the figure's own height."""

    depth: float
    """Extrusion thickness, same units: 0.18 is "a slab", 0.36 is "a brick"."""

    template: str = ""
    """A shape from :mod:`jarvis.vision.templates`, when the outline is not the photo's."""

    def outline(self, cut: Cut) -> Outline:
        if self.template:
            return cut_from_mask(mask_for(self.template)).outline(self.fraction)
        return cut.outline(self.fraction)


CANDIDATES: Final[tuple[Reading, ...]] = (
    Reading("细线", 0.004, 0.10),
    Reading("常规", 0.008, 0.18),
    Reading("粗线", 0.015, 0.26),
    Reading("厚块", 0.028, 0.36),
)
"""The 线框 and 照片 rungs: one outline, four densities of line and slab.

Measured over three real photos and the four templates: 291/86/48/27 down to
70/42/26/16, descending every time. The fractions used to be 0.002/0.004/0.008/0.016,
which sit under one pixel on a 195-pixel-tall subject -- the clamp at 0.6 px then made
the two finest rungs the same shape, and the vertex cap tightened the finest one until
it came back with *fewer* points than the next. A ladder whose numbers do not descend
is a ladder the picker cannot print honestly.

These are not four guesses at what the pet looks like -- the segmentation already
decided that, and pretending otherwise would have a person comparing four
thumbnails of one shape. What genuinely is a choice is how far the line gets
simplified and how thick the slab is, so that is what varies. The picker says so
in words, because "挑一个你喜欢的" over four identical silhouettes reads as broken.
"""

MAX_ENCODED_BYTES: Final[int] = 20_000_000
"""Ceiling on the base64 the page may hand over (the page resizes to ~768 px first).

Checked before decoding: a 200 MB string would otherwise be turned into a 150 MB
``bytes`` by the decoder before anything got a chance to object.
"""

STYLES: Final[tuple[str, ...]] = ("outline", "photo", "cartoon")
STYLE_LABELS: Final[dict[str, str]] = {"outline": "线框", "photo": "照片", "cartoon": "卡通"}
"""What a photo is turned into. Three answers to "make it look like my pet".

``outline`` keeps the original promise: a polygon and nothing else, so no image
derived from anybody's photograph reaches the disk.

``photo`` is the one that costs the operator something, and it is on purpose that the
cost is stated before the photo is chosen: the figure's face is a thumbnail of the
photo clipped to the silhouette, which is what makes it read as *their* cat -- stripes,
fur, the white sock -- and it is also an image of their cat, sitting in
``数据/pets`` until they delete the row.

``cartoon`` takes the opposite trade: an authored shape (sitting cat, round chick,
floppy-ear dog, bun) tinted with one number sampled from the subject. Cute, and the
only style where "the photo is not kept" is literally true while still looking like
somebody's pet.
"""

CARTOON_DEPTH: Final[float] = 0.22
"""How thick the cartoon slabs are. One value for all four: when the shapes differ,
the interesting choice is *which* animal, not how fat the extrusion is."""


class PetService:
    """The photo → pet transaction, plus the "which one is on the desktop" answer."""

    name = "pets"

    def __init__(
        self,
        library: PetLibrary,
        preferences: Preferences,
        *,
        cut: Callable[[bytes], Cut] = segment,
        shot: Callable[..., bytes] = preview_png,
        paint: Callable[[Cut], bytes] = thumbnail,
        tint: Callable[[Cut], str] = average_colour,
    ) -> None:
        self._library = library
        self._preferences = preferences
        self._cut = cut
        self._shot = shot
        self._paint = paint
        self._tint = tint
        self._busy = threading.Lock()

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        self._library.start()

    def stop(self) -> None:
        self._library.stop()

    # -- reads -------------------------------------------------------------

    def assets(self) -> dict[str, object]:
        """Everything the picker shows: the list, the current choice, and whether
        generating is possible at all right now."""
        return {
            "ok": True,
            "pets": [figure.to_public() for figure in self._library.figures()],
            "models": [entry.to_public() for entry in pet_gallery.models()],
            "current": self.current_id(),
            "can_generate": self.gap() == "",
            "reason": self.gap(),
            "error": "",
        }

    def gap(self) -> str:
        """Why a photo cannot be turned into a pet on this machine right now.

        Two independent reasons, both worth naming: the weights file is not where
        it should be (a broken install), and the library is full or unwritable (an
        operator action). Collapsing them into "生成失败" would send somebody to
        reinstall the program when the answer is "delete two of these".
        """
        gap = weights_gap() or self._library.generate_ready()
        if gap:
            return gap
        room = MAX_FIGURES - len(self._library.figures())
        if room < len(CANDIDATES):
            # Checked as a batch, not per figure: writing two of four and then
            # reporting "full" would leave a half-finished set on the desktop and
            # no way to tell which two were meant to be compared.
            return f"只剩 {room} 个位置，一次要 {len(CANDIDATES)} 个，先删几个"
        return ""

    def current_id(self) -> str:
        """The chosen figure, or ``""`` for the built-in one.

        A stored id that no longer resolves is cleared here rather than reported:
        the desktop and the picker must not disagree about who is standing there.
        ``builtin:<Key>`` is a second namespace -- the shipped models -- and it is
        checked against the files actually in the package, not against the list of
        names, so a model dropped from a release stops being claimed.
        """
        wanted = self._preferences.text(PET_FIGURE)
        if not wanted:
            return ""
        if wanted.startswith(pet_gallery.BUILTIN_PREFIX):
            if pet_gallery.resolve(wanted) is not None:
                return wanted
        elif self._library.resolve(wanted) is not None:
            return wanted
        logger.warning("chosen pet %s is gone; falling back to the built-in figure", wanted)
        self._preferences.forget(PET_FIGURE)
        return ""

    def model(self) -> dict[str, object]:
        """What the pet page asks for on load and on every change.

        ``points: []`` is a real answer, not a failure: it means "draw the built-in
        figure", which is the state a fresh install is in. ``kind`` says which of the
        three bodies the page should build -- a shipped model, a traced silhouette, or
        none of them.
        """
        chosen = self.current_id()
        builtin = pet_gallery.resolve(chosen)
        if builtin is not None:
            return {
                "ok": True,
                "kind": "builtin",
                "id": builtin.preference,
                "name": builtin.label,
                "model": builtin.to_public(),
                "depth": 0.0,
                "style": "",
                "colour": "",
                "template": "",
                "points": [],
                "texture": "",
            }
        figure = self._figure(chosen)
        if figure is None:
            return {
                "ok": True,
                "kind": "",
                "id": "",
                "name": "",
                "depth": 0.0,
                "style": "",
                "colour": "",
                "template": "",
                "points": [],
                "texture": "",
            }
        return {"ok": True, "kind": "silhouette", **figure.to_model()}

    # -- writes ------------------------------------------------------------

    def generate(
        self, photo: object, name: object = None, style: object = "outline"
    ) -> dict[str, object]:
        """One photo in, four stored figures out, in the chosen style.

        What the photo is allowed to become differs by style, and that is the whole
        design: ``outline`` keeps a polygon, ``cartoon`` keeps one colour, and only
        ``photo`` keeps an image -- a thumbnail clipped to the silhouette, which is
        what makes it look like *their* animal. The picker says which before the file
        dialog opens, so nobody discovers the trade afterwards.

        Returns ``{"ok": False, "error": ...}`` rather than raising: the caller is a
        settings panel, and the reason belongs on the screen in Chinese.
        """
        wanted = str(style or "outline").strip()
        if wanted not in STYLES:
            return {**self.assets(), "ok": False, "error": f"没这种风格：{wanted}"}
        gap = self.gap()
        if gap:
            return {**self.assets(), "ok": False, "error": f"现在生成不了：{gap}"}
        if not self._busy.acquire(blocking=False):
            return {**self.assets(), "ok": False, "error": "上一张还在生成，稍等一下"}
        try:
            data = _decode_payload(photo)
            base = _clean_base(name)
            started = time.perf_counter()
            cut = self._cut(data)
            traced_ms = int((time.perf_counter() - started) * 1000)
            texture = self._paint(cut) if wanted == "photo" else b""
            colour = self._tint(cut)
            made: list[PetFigure] = []
            for candidate in self._candidates_for(wanted, cut):
                outline = candidate.outline(cut)
                preview = self._shot(
                    outline, depth=candidate.depth, colour=colour if wanted == "cartoon" else ""
                )
                made.append(
                    self._library.add(
                        name=f"{base}·{candidate.label}",
                        outline=outline,
                        depth=candidate.depth,
                        cost_ms=traced_ms,
                        preview=preview,
                        style=wanted,
                        colour=colour,
                        template=candidate.template,
                        texture=texture,
                    )
                )
            total_ms = int((time.perf_counter() - started) * 1000)
            logger.info(
                "generated %d %s pets from one photo in %d ms (segmentation %d ms)",
                len(made),
                wanted,
                total_ms,
                traced_ms,
            )
            return {
                **self.assets(),
                "ok": True,
                "style": wanted,
                "colour": colour,
                "made": [figure.pet_id for figure in made],
                "cost_ms": total_ms,
                "segment_ms": traced_ms,
                "error": "",
            }
        except (VisionError, PetLibraryError) as exc:
            logger.warning("pet generation refused: %s", exc)
            return {**self.assets(), "ok": False, "error": f"没生成出来：{exc}"}
        except Exception as exc:  # pragma: no cover - a native failure we did not name
            logger.exception("pet generation crashed")
            return {
                **self.assets(),
                "ok": False,
                "error": f"生成时出了意外：{type(exc).__name__}: {exc}",
            }
        finally:
            self._busy.release()

    @staticmethod
    def _candidates_for(style: str, cut: Cut) -> tuple[Reading, ...]:
        """Four of what, for this style.

        The cartoon's four are four *animals* -- with authored shapes there is no
        reason to offer one shape four times -- and they still go through the same
        outline machinery, so the page receives one kind of object either way.
        """
        if style != "cartoon":
            return tuple(
                Reading(candidate.label, candidate.fraction, candidate.depth)
                for candidate in CANDIDATES
            )
        available = len(TEMPLATE_KINDS)
        if available >= len(CANDIDATES):
            return tuple(
                Reading(TEMPLATE_LABELS[kind], 0.012, CARTOON_DEPTH, template=kind)
                for kind in TEMPLATE_KINDS[: len(CANDIDATES)]
            )
        # Fewer templates than rungs: repeat the ladder's thicknesses over what exists
        # rather than handing back a short batch nobody asked for.
        return tuple(
            Reading(
                f"{TEMPLATE_LABELS[TEMPLATE_KINDS[index % available]]}·{CANDIDATES[index].label}",
                0.012,
                CANDIDATES[index].depth,
                template=TEMPLATE_KINDS[index % available],
            )
            for index in range(len(CANDIDATES))
        )

    def pick(self, pet_id: object) -> dict[str, object]:
        """Put one figure on the desktop. ``""`` puts the built-in one back.

        Two namespaces share this door: ``pet:<hex>`` for a traced figure and
        ``builtin:<Key>`` for a shipped model. Both are resolved before the preference
        is written, so a stale id from a deleted figure or a model that did not ship
        gets refused on screen instead of leaving her standing at nothing.
        """
        wanted = str(pet_id or "").strip()
        if wanted and self._figure(wanted) is None and pet_gallery.resolve(wanted) is None:
            return {**self.assets(), "ok": False, "error": "这只宠物已经不在了"}
        if not self._preferences.set(PET_FIGURE, wanted):
            return {**self.assets(), "ok": False, "error": "这个选择存不下来（preferences 拒写）"}
        logger.info("pet figure set to %s", wanted or "built-in")
        return {**self.assets(), "ok": True, "error": ""}

    def remove(self, pet_id: object) -> dict[str, object]:
        """Delete one figure. If it was the one showing, the desktop goes back to built-in."""
        wanted = str(pet_id or "").strip()
        if wanted.startswith(pet_gallery.BUILTIN_PREFIX):
            # Not a failure to paper over: the shipped models are part of the program,
            # and deleting one would mean editing the installed files.
            return {
                **self.assets(),
                "ok": False,
                "error": "内置模型随程序一起来，删不掉；想换就再挑一只",
            }
        if not wanted or self._figure(wanted) is None:
            return {**self.assets(), "ok": False, "error": "没有这只宠物，或者已经删掉了"}
        try:
            removed = self._library.remove(wanted)
        except OSError as exc:
            logger.warning("pet %s could not be deleted: %s", wanted, exc)
            return {**self.assets(), "ok": False, "error": f"删不掉：{exc}"}
        if not removed:
            return {**self.assets(), "ok": False, "error": "没有这只宠物，或者已经删掉了"}
        if self.current_id() == wanted:  # :meth:`current_id` already cleared it
            logger.info("the deleted pet was on screen; the built-in figure is back")
        return {**self.assets(), "ok": True, "error": ""}

    # -- helpers -----------------------------------------------------------

    def _figure(self, pet_id: str) -> PetFigure | None:
        return self._library.resolve(pet_id) if pet_id else None


def _clean_base(name: object) -> str:
    text = " ".join(str(name or "").split())
    return (text or "照片")[:16]


def _decode_payload(photo: object) -> bytes:
    """Accept a ``data:`` URL or bare base64 and return the encoded image bytes.

    Raises:
        VisionError: nothing usable was sent. The message is what the panel shows.
    """
    if isinstance(photo, (bytes, bytearray)):
        # Raw bytes are what a test or a future non-page caller hands over; the
        # ceiling on them is enforced where the pixels are read, not twice here.
        return bytes(photo)
    text = str(photo or "").strip()
    if not text:
        raise VisionError("没收到照片")
    if len(text) > MAX_ENCODED_BYTES:
        raise VisionError(
            f"照片太大了（{len(text) // 1024 // 1024} MB）",
            details={"encoded": len(text), "limit": MAX_ENCODED_BYTES},
        )
    if text.startswith("data:"):
        _, _, text = text.partition(",")
    try:
        return base64.b64decode(text, validate=True)
    except Exception as exc:
        raise VisionError("照片没传过来（base64 解不开），再选一次") from exc
