"""The pet library: silhouettes generated from photos, kept on disk.

What is stored, and what is not
-------------------------------
One row per figure in ``数据/pets/index.json``, plus one PNG preview beside it.
A row holds the polygon, the extrusion depth and the measured cost of making it
-- **not the photo**. The photo is the most private thing in this transaction and
it is also, once the boundary has been traced, unnecessary: a mesh is a list of
points. So the JPEG is decoded, traced and dropped inside one function call
(:meth:`jarvis.app.pet_service.PetService.generate`), and nothing on disk here
can be traced back to it.

That has one consequence the picker has to say out loud: there is no "regenerate
from the same photo". Changing your mind means choosing the file again, which is
one click, and the alternative is keeping somebody's photograph so they can have
a second opinion later.

Why a directory of its own
--------------------------
Same reason ``voices_dir`` has one (see :mod:`jarvis.app.voice_library`): this tree
is stuff the operator made, not weights something downloaded, and it should be a
place they can open, look through and delete without hunting.
"""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Final

from jarvis.vision.silhouette import Outline, Point

logger = logging.getLogger("jarvis.app.pet_library")

__all__ = ["MAX_FIGURES", "PetFigure", "PetLibrary", "PetLibraryError"]


PET_PREFIX = "pet:"
"""Generated figures are namespaced like cloned voices, so one string answers
"which of the three kinds of thing is this id" and no second registry is needed."""

MAX_FIGURES: Final[int] = 12
"""Twelve is more than anybody tries from six photos, and it bounds what one
``pet_assets`` call has to carry (each row brings its preview along)."""

MAX_NAME: Final[int] = 24

_INDEX_VERSION = 1
_SAFE_ID = re.compile(r"^pet:[0-9a-f]{12}$")


@dataclass(frozen=True, slots=True)
class PetFigure:
    """One generated pet."""

    pet_id: str
    name: str
    points: tuple[Point, ...]
    aspect: float
    coverage: float
    tolerance: float
    depth: float
    created_at: str
    cost_ms: int
    preview_path: Path
    style: str = "outline"
    colour: str = ""
    template: str = ""
    texture_path: Path | None = None

    @property
    def lines(self) -> int:
        """How many vertices the outline has -- what "细线/粗线" actually means."""
        return len(self.points)

    def to_public(self) -> dict[str, object]:
        """The picker's row. No polygon: 900 pairs is not list metadata.

        ``has_texture`` is a flag rather than the bytes. Twelve rows carrying a
        thumbnail each would be a megabyte of base64 on every panel open, and the
        picker only needs to know whether to say "这张存了你的照片".
        """
        return {
            "id": self.pet_id,
            "name": self.name,
            "lines": self.lines,
            "aspect": round(self.aspect, 2),
            "coverage": round(self.coverage, 3),
            "depth": round(self.depth, 3),
            "style": self.style,
            "colour": self.colour,
            "template": self.template,
            "has_texture": bool(self.texture_path and self.texture_path.is_file()),
            "created_at": self.created_at,
            "cost_ms": self.cost_ms,
            "preview": _read_preview(self.preview_path),
        }

    def to_model(self) -> dict[str, object]:
        """What the pet page needs to build the geometry -- including the texture.

        The texture crosses here and nowhere else: one figure at a time, only when
        the page is about to draw it.
        """
        payload: dict[str, object] = {
            "id": self.pet_id,
            "name": self.name,
            "depth": round(self.depth, 3),
            "style": self.style,
            "colour": self.colour,
            "template": self.template,
            "points": [[round(x, 4), round(y, 4)] for x, y in self.points],
        }
        if self.texture_path is not None and self.texture_path.is_file():
            payload["texture"] = _read_preview(self.texture_path)
        else:
            payload["texture"] = ""
        return payload


class PetLibraryError(Exception):
    """Raised with a message written for the person holding the mouse."""


class PetLibrary:
    """Directory and index for generated figures. Owns no image model."""

    name = "pet_library"

    def __init__(self, root: Path) -> None:
        self._root = root
        self._index_path = root / "index.json"

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        """Create the directory. An unwritable one is not fatal.

        The pet is one of three things this window can be; a ``数据/pets`` that
        cannot be made (read-only drive, somebody else's folder) must cost the
        operator the generate button and nothing else. :meth:`generate_ready`
        reports it, so the button says why instead of failing after the click.
        """
        try:
            self._root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            logger.warning("pet directory %s unusable: %s", self._root, exc)

    def stop(self) -> None:
        """Nothing held; here so the lifecycle component list stays uniform."""

    # -- reads -------------------------------------------------------------

    def figures(self) -> list[PetFigure]:
        """Every stored figure, in the order they were made."""
        return [figure for figure in (self._from_entry(e) for e in self._read_index()) if figure]

    def resolve(self, pet_id: str) -> PetFigure | None:
        """One figure by id, or ``None``."""
        for figure in self.figures():
            if figure.pet_id == pet_id:
                return figure
        return None

    def generate_ready(self) -> str:
        """Why generating is not possible right now, or ``""``.

        Checked before the file picker opens: "the disk is full" arriving after a
        person has found the photo and chosen it is the same bad news two minutes
        later and twice as annoying.
        """
        if len(self.figures()) >= MAX_FIGURES:
            return f"已经存了 {MAX_FIGURES} 个，先删一个再生成"
        try:
            self._root.mkdir(parents=True, exist_ok=True)
            probe = self._root / ".write-probe"
            probe.write_bytes(b"")
            probe.unlink(missing_ok=True)
        except OSError as exc:
            # The exception text is kept out of the message on purpose: an OSError
            # repr carries double-escaped backslashes, and "写不进 E:\DaiMa\..."
            # is unreadable in a settings panel. It goes to the log instead.
            logger.warning("pet directory %s is not writable: %s", self._root, exc)
            return f"这个目录写不进去：{self._root}（盘满了，或者程序放在只读位置）"
        return ""

    # -- writes ------------------------------------------------------------

    def add(
        self,
        *,
        name: object,
        outline: Outline,
        depth: float,
        cost_ms: int,
        preview: bytes,
        style: str = "outline",
        colour: str = "",
        template: str = "",
        texture: bytes = b"",
    ) -> PetFigure:
        """Store one figure, its preview, and -- for the 照片 style -- its texture.

        The PNGs go first and are dropped again if the index write fails: an index
        row with no preview leaves a picker showing a broken image, while a stray PNG
        beside a healthy index is invisible to the user.

        ``texture`` is the only argument here that writes a picture of somebody's pet
        to disk, and it is empty for the other two styles. Callers do not get to
        forget that: :class:`~jarvis.app.pet_service.PetService` is the one that
        decides, from the style the picker asked for.

        Raises:
            PetLibraryError: the name is unusable, the polygon is empty, or the
                disk refused either write. Messages are shown verbatim.
        """
        clean_name = self._clean_name(name)
        if len(outline.points) < 3:
            raise PetLibraryError("这条轮廓点太少，做不出形状")
        room = MAX_FIGURES - len(self.figures())
        if room <= 0:
            raise PetLibraryError(f"最多存 {MAX_FIGURES} 个，先删一个再生成")

        pet_id = PET_PREFIX + secrets.token_hex(6)
        preview_path = self._preview_path(pet_id)
        texture_path = self._texture_path(pet_id) if texture else None
        try:
            self._root.mkdir(parents=True, exist_ok=True)
            preview_path.write_bytes(preview)
            if texture_path is not None:
                texture_path.write_bytes(texture)
        except OSError as exc:
            preview_path.unlink(missing_ok=True)
            if texture_path is not None:
                texture_path.unlink(missing_ok=True)
            raise PetLibraryError(f"写不进图片文件：{exc}") from exc

        figure = PetFigure(
            pet_id=pet_id,
            name=clean_name,
            points=outline.points,
            aspect=outline.aspect,
            coverage=outline.coverage,
            tolerance=outline.tolerance,
            depth=round(float(depth), 3),
            created_at=datetime.now().isoformat(timespec="seconds"),
            cost_ms=int(cost_ms),
            preview_path=preview_path,
            style=style,
            colour=colour,
            template=template,
            texture_path=texture_path,
        )
        entries = self._read_index()
        entries.append(
            {
                "id": figure.pet_id,
                "name": figure.name,
                "points": [[round(x, 4), round(y, 4)] for x, y in figure.points],
                "aspect": round(figure.aspect, 4),
                "coverage": round(figure.coverage, 4),
                "tolerance": round(figure.tolerance, 5),
                "depth": figure.depth,
                "style": figure.style,
                "colour": figure.colour,
                "template": figure.template,
                "texture": bool(texture_path),
                "created_at": figure.created_at,
                "cost_ms": figure.cost_ms,
            }
        )
        try:
            self._write_index(entries)
        except OSError as exc:
            preview_path.unlink(missing_ok=True)
            if texture_path is not None:
                texture_path.unlink(missing_ok=True)
            raise PetLibraryError(f"写不进宠物清单：{exc}") from exc
        logger.info(
            "stored pet %s (%s, %s, %d lines, depth %.2f, texture=%s)",
            figure.pet_id,
            figure.name,
            figure.style,
            figure.lines,
            figure.depth,
            texture_path is not None,
        )
        return figure

    def remove(self, pet_id: str) -> bool:
        """Forget one, and every picture of it. ``False`` when it was not there.

        Index first, files second -- the same order as :class:`VoiceLibrary` and for
        the same reason: the leftover of a crash is then an orphaned file rather than
        a row that points at nothing. The texture is deleted here rather than by
        callers so that no path to removing a figure can leave a photo behind.
        """
        entries = self._read_index()
        kept = [entry for entry in entries if entry.get("id") != pet_id]
        if len(kept) == len(entries):
            return False
        self._write_index(kept)
        self._preview_path(pet_id).unlink(missing_ok=True)
        self._texture_path(pet_id).unlink(missing_ok=True)
        logger.info("removed pet %s", pet_id)
        return True

    # -- disk --------------------------------------------------------------

    def _texture_path(self, pet_id: str) -> Path:
        return self._root / f"{pet_id.removeprefix(PET_PREFIX)}.tex.png"

    def _preview_path(self, pet_id: str) -> Path:
        return self._root / f"{pet_id.removeprefix(PET_PREFIX)}.png"

    def _read_index(self) -> list[dict[str, object]]:
        try:
            raw = json.loads(self._index_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return []
        except (OSError, ValueError) as exc:
            logger.warning("pet index unreadable (%s); treating it as empty", exc)
            return []
        if not isinstance(raw, dict):
            return []
        entries = raw.get("pets")
        if not isinstance(entries, list):
            return []
        return [entry for entry in entries if isinstance(entry, dict)]

    def _write_index(self, entries: list[dict[str, object]]) -> None:
        """Write through a temporary file and rename (see ``voice_library``)."""
        self._root.mkdir(parents=True, exist_ok=True)
        payload = {"version": _INDEX_VERSION, "pets": entries}
        scratch = self._index_path.with_suffix(".json.tmp")
        scratch.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(scratch, self._index_path)

    def _clean_name(self, name: object) -> str:
        text = " ".join(str(name or "").split())
        if not text:
            raise PetLibraryError("给这只宠物起个名字")
        return text[:MAX_NAME]

    def _from_entry(self, entry: dict[str, object]) -> PetFigure | None:
        pet_id = str(entry.get("id") or "")
        if not _SAFE_ID.match(pet_id):
            return None
        outline = Outline.from_dict(entry)
        if outline is None:
            logger.warning("pet entry %s has no usable polygon; skipping", pet_id)
            return None
        depth = entry.get("depth")
        return PetFigure(
            pet_id=pet_id,
            name=str(entry.get("name") or "未命名"),
            points=outline.points,
            aspect=outline.aspect,
            coverage=outline.coverage,
            tolerance=outline.tolerance,
            depth=(
                float(depth)
                if isinstance(depth, (int, float)) and not isinstance(depth, bool)
                else 0.18
            ),
            created_at=str(entry.get("created_at") or ""),
            cost_ms=_as_int(entry.get("cost_ms"), 0),
            preview_path=self._preview_path(pet_id),
            style=str(entry.get("style") or "outline"),
            colour=str(entry.get("colour") or ""),
            template=str(entry.get("template") or ""),
            # Only claim a texture file exists when the row says it was written: a
            # row from before the 照片 style stays honest as an outline figure rather
            # than pointing the page at a PNG that was never made.
            texture_path=(self._texture_path(pet_id) if entry.get("texture") else None),
        )


def _as_int(value: object, fallback: int) -> int:
    """A number out of an untyped JSON row; a bad one means the fallback."""
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return fallback
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


def _read_preview(path: Path) -> str:
    """The preview as base64, or ``""`` when it is not there.

    A missing PNG is not an error: the row still names a figure that can be put on
    the desktop, and a picker that refuses to list a pet because its thumbnail is
    gone would hide a working feature behind a cosmetic one.
    """
    import base64

    try:
        return base64.b64encode(path.read_bytes()).decode("ascii")
    except OSError:
        return ""
