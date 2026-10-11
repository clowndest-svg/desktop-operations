"""The pet store: an index, one PNG per figure, and no photograph anywhere.

The last clause is the reason this file exists. Everything else -- atomic writes,
rows that fail to parse being skipped rather than fatal -- is the same bargain
``test_app_voice_library`` strikes for recordings. What is different here is that
the input was a person's photo, so "the store holds only what a mesh needs" is a
promise the tests have to keep, not a comment in a docstring.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

from jarvis.app.pet_library import (
    MAX_FIGURES,
    MAX_NAME,
    PET_PREFIX,
    PetFigure,
    PetLibrary,
    PetLibraryError,
)
from jarvis.vision.silhouette import Outline

POINTS = tuple(
    (x / 10.0, y / 10.0)
    for x, y in [(0, 0), (1, 0), (1, 1), (0, 2), (-1, 1), (-1, 0), (0, -1), (1, -1)]
)


def _library(tmp_path: Path) -> PetLibrary:
    library = PetLibrary(tmp_path / "pets")
    library.start()
    return library


def _outline(points: tuple[tuple[float, float], ...] = POINTS) -> Outline:
    return Outline(points=points, aspect=1.0, coverage=0.31, tolerance=0.012)


def _add(library: PetLibrary, *, name: str = "白虎·常规", depth: float = 0.18) -> PetFigure:
    return library.add(
        name=name, outline=_outline(), depth=depth, cost_ms=742, preview=b"\x89PNG fake"
    )


# -- what a row holds --------------------------------------------------------


def test_a_figure_is_stored_listed_and_resolved(tmp_path: Path) -> None:
    library = _library(tmp_path)
    figure = _add(library)

    assert figure.pet_id.startswith(PET_PREFIX)
    assert figure.name == "白虎·常规"
    assert figure.lines == len(POINTS)
    assert figure.cost_ms == 742
    assert figure.preview_path.is_file()
    assert [row.pet_id for row in library.figures()] == [figure.pet_id]
    assert library.resolve(figure.pet_id) == figure


def test_the_store_holds_the_outline_and_only_one_kind_of_picture(tmp_path: Path) -> None:
    """What each style is allowed to leave on disk, asserted per style.

    The original version of this test said "no photograph, under any name", and that
    was true while one style existed. The photo style buys its likeness by keeping a
    thumbnail clipped to the silhouette, so the promise had to become narrower and
    precise instead of being quietly dropped: an outline or a cartoon writes nothing
    but the picker's preview, a photo figure writes exactly one texture file, and
    deleting the row takes both of them with it.
    """
    library = _library(tmp_path)
    outline_figure = _add(library)
    cartoon = library.add(
        name="团子",
        outline=_outline(),
        depth=0.22,
        cost_ms=1,
        preview=b"PREVIEW",
        style="cartoon",
        colour="#c8a04a",
        template="bun",
    )
    photo = library.add(
        name="我家猫",
        outline=_outline(),
        depth=0.18,
        cost_ms=1,
        preview=b"PREVIEW",
        style="photo",
        colour="#988d89",
        texture=b"TEXTURE",
    )

    files = {path.name for path in (tmp_path / "pets").iterdir()}
    assert files == {
        "index.json",
        f"{outline_figure.pet_id[4:]}.png",
        f"{cartoon.pet_id[4:]}.png",
        f"{photo.pet_id[4:]}.png",
        f"{photo.pet_id[4:]}.tex.png",
    }, "只有照片风格能多出一张图片"
    assert not list((tmp_path / "pets").glob("*.jpg")) + list((tmp_path / "pets").glob("*.jpeg"))
    index = json.loads((tmp_path / "pets" / "index.json").read_text(encoding="utf-8"))
    assert set(index["pets"][0]) == {
        "id",
        "name",
        "points",
        "aspect",
        "coverage",
        "tolerance",
        "depth",
        "style",
        "colour",
        "template",
        "texture",
        "created_at",
        "cost_ms",
    }
    assert [row.style for row in (outline_figure, cartoon, photo)] == [
        "outline",
        "cartoon",
        "photo",
    ]
    assert [bool(row.to_public()["has_texture"]) for row in (outline_figure, cartoon, photo)] == [
        False,
        False,
        True,
    ]

    library.remove(photo.pet_id)

    assert not (tmp_path / "pets" / f"{photo.pet_id[4:]}.tex.png").exists()
    assert not (tmp_path / "pets" / f"{photo.pet_id[4:]}.png").exists()


def test_a_photo_row_still_finds_its_texture_after_a_restart(tmp_path: Path) -> None:
    """The row says "texture", so the page must be told the bytes are there.

    A figure re-read from the index that lost its texture renders as a flat silhouette
    with no explanation -- the same class of bug as a cloned voice whose reference clip
    went missing, and it is caught here rather than on the desktop.
    """
    import base64

    library = _library(tmp_path)
    stored = library.add(
        name="我家猫",
        outline=_outline(),
        depth=0.18,
        cost_ms=1,
        preview=b"PREVIEW",
        style="photo",
        colour="#988d89",
        texture=b"TEXTURE",
    )

    again = PetLibrary(tmp_path / "pets")
    again.start()
    found = again.resolve(stored.pet_id)

    assert found is not None
    assert found.style == "photo"
    assert found.texture_path is not None and found.texture_path.is_file()
    texture = found.to_model()["texture"]
    assert isinstance(texture, str)
    assert base64.b64decode(texture) == b"TEXTURE"


def test_the_cartoon_style_never_writes_a_picture_of_the_photo(tmp_path: Path) -> None:
    """One colour crosses the line for a cartoon, and it is a hex string."""
    library = _library(tmp_path)
    figure = library.add(
        name="团鸡",
        outline=_outline(),
        depth=0.22,
        cost_ms=1,
        preview=b"PREVIEW",
        style="cartoon",
        colour="#f2c14e",
        template="bird",
    )
    model = figure.to_model()

    assert model["template"] == "bird"
    assert model["colour"] == "#f2c14e"
    assert model["texture"] == ""
    assert not (tmp_path / "pets" / f"{figure.pet_id[4:]}.tex.png").exists()


def test_the_polygon_survives_a_restart(tmp_path: Path) -> None:
    library = _library(tmp_path)
    figure = _add(library)

    again = PetLibrary(tmp_path / "pets")
    again.start()
    stored = again.resolve(figure.pet_id)

    assert stored is not None
    assert list(stored.points) == list(figure.points)
    assert stored.depth == pytest.approx(figure.depth)


def test_the_picker_row_carries_a_png_and_no_polygon(tmp_path: Path) -> None:
    figure = _add(_library(tmp_path))
    row = figure.to_public()

    assert "points" not in row
    assert row["lines"] == len(POINTS)
    preview = row["preview"]
    assert isinstance(preview, str)
    assert base64.b64decode(preview) == b"\x89PNG fake"


def test_a_row_whose_thumbnail_vanished_still_lists(tmp_path: Path) -> None:
    """A missing preview must not hide a figure that would render perfectly."""
    figure = _add(_library(tmp_path))
    figure.preview_path.unlink()

    row = figure.to_public()

    assert row["preview"] == ""
    assert row["id"] == figure.pet_id


# -- names, limits, deletion -------------------------------------------------


def test_a_name_is_trimmed_and_capped(tmp_path: Path) -> None:
    library = _library(tmp_path)

    figure = library.add(
        name=f"  我家   猫\n  {'呀' * 60}  ",
        outline=_outline(),
        depth=0.2,
        cost_ms=1,
        preview=b"x",
    )

    assert figure.name.startswith("我家 猫 呀呀")
    assert len(figure.name) == MAX_NAME


@pytest.mark.parametrize("name", ["", "   ", None])
def test_an_unnamed_figure_is_refused_rather_than_stored_as_blank(
    tmp_path: Path, name: object
) -> None:
    with pytest.raises(PetLibraryError, match="名字"):
        _library(tmp_path).add(name=name, outline=_outline(), depth=0.2, cost_ms=1, preview=b"x")


def test_a_shape_with_nothing_to_extrude_is_refused(tmp_path: Path) -> None:
    library = _library(tmp_path)

    with pytest.raises(PetLibraryError, match="轮廓"):
        library.add(
            name="一点点",
            outline=_outline(points=((0.0, 0.0), (1.0, 1.0))),
            depth=0.2,
            cost_ms=1,
            preview=b"x",
        )


def test_the_library_stops_at_twelve_figures(tmp_path: Path) -> None:
    library = _library(tmp_path)
    for index in range(MAX_FIGURES):
        _add(library, name=f"第{index}只")

    assert len(library.figures()) == MAX_FIGURES
    assert "先删一个" in library.generate_ready()
    with pytest.raises(PetLibraryError, match="12"):
        _add(library, name="第十三个")


def test_deleting_writes_the_index_first_and_leaves_nothing_behind(tmp_path: Path) -> None:
    library = _library(tmp_path)
    figure = _add(library)

    assert library.remove(figure.pet_id) is True
    assert library.figures() == []
    assert not figure.preview_path.exists()
    assert library.remove(figure.pet_id) is False


def test_an_index_that_cannot_be_read_is_empty_not_fatal(tmp_path: Path) -> None:
    library = _library(tmp_path)
    _add(library)
    (tmp_path / "pets" / "index.json").write_text("{not json", encoding="utf-8")

    assert library.figures() == []
    assert library.generate_ready() == "", "the store has to stay usable"


def test_a_row_that_makes_no_sense_is_skipped_and_the_others_survive(tmp_path: Path) -> None:
    library = _library(tmp_path)
    good = _add(library)
    entries = [
        {"id": "pet:zzzzzzzzzzzz", "name": "手改坏的"},
        {"id": "not-even-a-pet-id", "name": "别的表串进来的"},
        json.loads(
            json.dumps({"id": good.pet_id, "name": good.name, "points": good.to_model()["points"]})
        ),
    ]
    (tmp_path / "pets" / "index.json").write_text(
        json.dumps({"version": 1, "pets": [*entries[0:2], entries[2]]}, ensure_ascii=False),
        encoding="utf-8",
    )

    listed = library.figures()

    assert [row.pet_id for row in listed] == [good.pet_id]


def test_a_directory_that_cannot_be_written_says_so_without_an_exception(tmp_path: Path) -> None:
    blocker = tmp_path / "a-file"
    blocker.write_text("x", encoding="utf-8")

    assert "写不进去" in PetLibrary(blocker / "pets").generate_ready()


def test_no_temporary_index_is_left_next_to_the_real_one(tmp_path: Path) -> None:
    _add(_library(tmp_path))

    assert not list((tmp_path / "pets").glob("*.tmp"))
