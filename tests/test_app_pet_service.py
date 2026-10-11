"""One photo in, four figures out -- with the segmenter and the PNG painter faked.

The service is the transaction, so that is what is tested here: how many rows a
photo produces, what happens to the photo, which figure the desktop ends up
showing, and what each failure says. The geometry behind it belongs to
「test_vision_silhouette」 and the disk layout to 「test_app_pet_library」, so the
fakes below are deliberately cheap: a fixed circle and four bytes of PNG.
"""

from __future__ import annotations

import base64
import io
import json
import math
from pathlib import Path
from typing import Any

import pytest

from jarvis.app.pet_library import MAX_FIGURES, PetLibrary
from jarvis.app.pet_service import CANDIDATES, MAX_ENCODED_BYTES, PetService
from jarvis.app.preferences import PET_FIGURE, Preferences
from jarvis.core.exceptions import VisionError
from jarvis.vision.silhouette import Cut
from tests._fakes import json_object

PREFIX = "pet:"


def _circle(points: int = 200, radius: int = 120) -> tuple[tuple[int, int], ...]:
    """A disc with ±1 px of wobble on it, because a *perfect* polygon simplifies
    identically at two of the four tolerances -- and the real trace always wobbles."""
    ring: list[tuple[int, int]] = []
    for step in range(points):
        wobble = ((step * 7) % 3) - 1
        angle = step / points * 2 * math.pi
        ring.append(
            (
                round(radius + (radius + wobble) * math.cos(angle)),
                round(radius + (radius + wobble) * math.sin(angle)),
            )
        )
    return tuple(ring)


def _fake_cut(photo: bytes) -> Cut:
    """A stand-in for 230 ms of ONNX: the boundary of a disc, in mask pixels."""
    assert photo, "the service must hand something to the segmenter"
    return Cut(raw=_circle(), coverage=0.42, height_px=2 * 121)


def _fake_shot(outline: Any, *, depth: float, colour: str = "") -> bytes:
    return f"png:{depth}:{colour}".encode()


@pytest.fixture()
def service(tmp_path: Path) -> PetService:
    library = PetLibrary(tmp_path / "pets")
    library.start()
    return PetService(
        library, Preferences(tmp_path / "preferences.json"), cut=_fake_cut, shot=_fake_shot
    )


def _photo_bytes() -> bytes:
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (40, 40), (9, 9, 9)).save(buffer, format="PNG")
    return buffer.getvalue()


def _data_url() -> str:
    return "data:image/png;base64," + base64.b64encode(_photo_bytes()).decode("ascii")


# -- generating --------------------------------------------------------------


def test_one_photo_makes_exactly_the_candidate_list(service: PetService) -> None:
    answer = json_object(service.generate(_data_url(), "白虎"))

    assert answer["ok"] is True, answer["error"]
    assert len(answer["made"]) == len(CANDIDATES) == 4
    assert [row["name"] for row in answer["pets"]] == [
        f"白虎·{candidate.label}" for candidate in CANDIDATES
    ]
    assert [row["depth"] for row in answer["pets"]] == [
        round(candidate.depth, 3) for candidate in CANDIDATES
    ]
    assert answer["cost_ms"] >= answer["segment_ms"] >= 0


def test_the_four_candidates_share_one_segmentation(service: PetService) -> None:
    """Four passes would be four slightly different animals, and four times the wait."""
    calls: list[bytes] = []

    def counting(photo: bytes) -> Cut:
        calls.append(photo)
        return _fake_cut(photo)

    service._cut = counting
    json_object(service.generate(_data_url(), "一只"))

    assert len(calls) == 1


def test_the_candidates_differ_in_line_count_not_in_shape(service: PetService) -> None:
    """The picker promises four thicknesses of one outline, so that had better vary."""
    answer = json_object(service.generate(_data_url(), "白虎"))
    lines = [row["lines"] for row in answer["pets"]]
    aspects = {row["aspect"] for row in answer["pets"]}

    assert lines == sorted(lines, reverse=True), "a looser tolerance cannot add lines"
    assert len(set(lines)) == len(lines), "four rows that differ by nothing are three rows"
    assert max(aspects) - min(aspects) < 0.02, "same boundary, so the same proportions"
    assert len({row["depth"] for row in answer["pets"]}) == len(CANDIDATES)


def test_the_photo_is_decoded_from_a_data_url_and_never_written(service: PetService) -> None:
    root = service._library._root
    before = set(root.iterdir())

    answer = json_object(service.generate(_data_url(), "白虎"))

    assert answer["ok"] is True
    added = sorted(path.name for path in set(root.iterdir()) - before)
    assert added == sorted(
        ["index.json"] + [f"{pet.removeprefix(PREFIX)}.png" for pet in answer["made"]]
    )
    assert not list(root.glob("*.jpg")) + list(root.glob("*.jpeg"))


def test_raw_bytes_are_accepted_because_that_is_what_a_test_has(tmp_path: Path) -> None:
    library = PetLibrary(tmp_path / "pets")
    library.start()
    made = PetService(library, Preferences(tmp_path / "p.json"), cut=_fake_cut, shot=_fake_shot)

    assert made.generate(_photo_bytes(), "直接给")["ok"] is True


@pytest.mark.parametrize(
    ("payload", "wanted"),
    [("", "没收到照片"), ("data:image/png;base64,###", "没传过来")],
)
def test_a_photo_that_never_arrives_says_so(service: PetService, payload: str, wanted: str) -> None:
    answer = json_object(service.generate(payload, "白虎"))

    assert answer["ok"] is False
    assert wanted in answer["error"]
    assert answer["pets"] == []


def test_an_oversized_upload_is_stopped_before_it_is_decoded(service: PetService) -> None:
    """The string is built here rather than in a parametrize id: a 20 MB test id is a
    20 MB line in the report, which is how this first presented itself."""
    answer = json_object(service.generate("0" * (MAX_ENCODED_BYTES + 4), "白虎"))

    assert answer["ok"] is False
    assert "太大" in answer["error"]


def test_a_segmenter_that_finds_nothing_keeps_its_words(service: PetService) -> None:
    def refusing(photo: bytes) -> Cut:
        raise VisionError("照片里找不出主体（背景太乱，或者整张就是一块颜色）")

    service._cut = refusing
    answer = json_object(service.generate(_data_url(), "白虎"))

    assert answer["ok"] is False
    assert "找不出主体" in answer["error"], "the reason must survive the transaction"
    assert answer["pets"] == []


def test_two_photos_at_once_are_refused_rather_than_queued(service: PetService) -> None:
    """Each pass loads a 4.5 MB model into memory; a queue would be two of them."""
    assert service._busy.acquire(blocking=False)
    try:
        answer = json_object(service.generate(_data_url(), "第二张"))
    finally:
        service._busy.release()

    assert answer["ok"] is False
    assert "上一张还在生成" in answer["error"]
    assert answer["pets"] == []


# -- choosing ----------------------------------------------------------------


def test_picking_a_figure_makes_it_the_one_the_desktop_draws(service: PetService) -> None:
    made = json_object(service.generate(_data_url(), "白虎"))["made"]

    answer = json_object(service.pick(made[2]))

    assert answer["ok"] is True
    assert answer["current"] == made[2]
    model = json_object(service.model())
    assert model["id"] == made[2]
    stored = service._library.resolve(made[2])
    assert stored is not None
    assert len(model["points"]) == stored.lines
    assert model["depth"] == round(CANDIDATES[2].depth, 3)


def test_picking_nothing_puts_the_builtin_figure_back(service: PetService) -> None:
    made = json_object(service.generate(_data_url(), "白虎"))["made"]
    json_object(service.pick(made[0]))

    assert json_object(service.pick(""))["ok"] is True
    assert service.current_id() == ""
    model = json_object(service.model())
    assert model["id"] == ""
    assert model["points"] == []
    assert model["style"] == ""
    assert model["texture"] == "", "没有宠物时不许带图片"


def test_a_choice_survives_a_restart(tmp_path: Path) -> None:
    library = PetLibrary(tmp_path / "pets")
    library.start()
    preferences = Preferences(tmp_path / "p.json")
    first = PetService(library, preferences, cut=_fake_cut, shot=_fake_shot)
    made = json_object(first.generate(_data_url(), "白虎"))["made"]
    first.pick(made[1])

    again = PetService(PetLibrary(tmp_path / "pets"), preferences, cut=_fake_cut, shot=_fake_shot)

    assert again.current_id() == made[1]
    assert json_object(again.model())["id"] == made[1]


def test_picking_a_figure_that_is_not_there_changes_nothing(service: PetService) -> None:
    json_object(service.generate(_data_url(), "白虎"))

    answer = json_object(service.pick(PREFIX + "ffffffffffff"))

    assert answer["ok"] is False
    assert "不在了" in answer["error"]
    assert service.current_id() == ""


def test_deleting_the_figure_on_screen_gives_her_the_builtin_one_back(service: PetService) -> None:
    made = json_object(service.generate(_data_url(), "白虎"))["made"]
    json_object(service.pick(made[0]))

    answer = json_object(service.remove(made[0]))

    assert answer["ok"] is True
    assert made[0] not in [row["id"] for row in answer["pets"]]
    assert service.current_id() == ""
    assert json_object(service.model())["points"] == []


def test_a_stored_choice_whose_figure_went_off_the_disk_is_forgotten(service: PetService) -> None:
    """The desktop and the picker must not disagree about who is standing there."""
    made = json_object(service.generate(_data_url(), "白虎"))["made"]
    json_object(service.pick(made[0]))
    library = service._library
    rows = [row for row in library._read_index() if row["id"] != made[0]]
    library._write_index(rows)

    assert service.current_id() == ""
    assert json_object(service.model())["id"] == ""
    # "Forgotten" has to mean the file too, not just the answer: a stored id nothing
    # can resolve is a row the picker would keep pointing at on the next launch.
    assert service._preferences.text(PET_FIGURE) == ""


def test_deleting_twice_is_a_no_not_a_crash(service: PetService) -> None:
    made = json_object(service.generate(_data_url(), "白虎"))["made"]

    assert json_object(service.remove(made[0]))["ok"] is True
    second = json_object(service.remove(made[0]))

    assert second["ok"] is False
    assert "已经删掉" in second["error"]


# -- the door in front of the button ----------------------------------------


def test_assets_reports_whether_generating_is_possible(service: PetService) -> None:
    answer = json_object(service.assets())

    assert answer["can_generate"] is True
    assert answer["reason"] == ""
    assert answer["pets"] == []


def test_a_missing_weights_file_is_named_before_anybody_picks_a_photo(
    service: PetService, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("jarvis.app.pet_service.weights_gap", lambda: "抠图模型不在 X")

    answer = json_object(service.generate(_data_url(), "白虎"))

    assert answer["ok"] is False
    assert "抠图模型不在 X" in answer["error"]
    assert answer["pets"] == [], "the list still has to come back so the panel can render"


def test_a_batch_that_would_not_fit_is_refused_whole(service: PetService) -> None:
    """Two of four on the disk is not a set anybody can compare side by side."""
    for _ in range(MAX_FIGURES // len(CANDIDATES)):
        assert json_object(service.generate(_data_url(), "白虎"))["ok"] is True
    for row in json_object(service.assets())["pets"][:3]:
        json_object(service.remove(row["id"]))
    assert len(json_object(service.assets())["pets"]) == 9

    answer = json_object(service.generate(_data_url(), "白虎"))

    assert answer["ok"] is False
    assert "只剩 3 个位置" in answer["error"]
    assert len(json_object(service.assets())["pets"]) == MAX_FIGURES - 3


def test_the_choice_is_stored_under_a_key_preferences_accepts(service: PetService) -> None:
    """The preference table refuses unknown keys in silence; this catches that drift."""
    made = json_object(service.generate(_data_url(), "白虎"))["made"]

    assert json_object(service.pick(made[0]))["ok"] is True
    assert service._preferences.get(PET_FIGURE) == made[0]


# -- the three styles --------------------------------------------------------


def _styles(tmp_path: Path) -> PetService:
    library = PetLibrary(tmp_path / "pets")
    library.start()
    return PetService(
        library,
        Preferences(tmp_path / "p.json"),
        cut=_fake_cut,
        shot=_fake_shot,
        paint=lambda cut: b"TEXTURE",
        tint=lambda cut: "#c8a04a",
    )


def test_the_cartoon_style_offers_four_animals_and_keeps_no_picture(tmp_path: Path) -> None:
    """The cartoon's four candidates are four *shapes*, and nothing but a colour is stored.

    The other two styles offer four thicknesses of one outline. If this style quietly
    fell back to that, the picker would show four identical blobs and the feature the
    operator asked for -- "make it look like the cute one" -- would be gone with no
    error anywhere.
    """
    service = _styles(tmp_path)

    answer = json_object(service.generate(_data_url(), "我家猫", "cartoon"))

    assert answer["ok"] is True, answer["error"]
    names = [row["name"] for row in answer["pets"]]
    assert names == ["我家猫·坐猫", "我家猫·团鸡", "我家猫·垂耳狗", "我家猫·团子"], names
    assert [row["template"] for row in answer["pets"]] == ["cat", "bird", "dog", "bun"]
    assert [row["style"] for row in answer["pets"]] == ["cartoon"] * 4
    assert [row["has_texture"] for row in answer["pets"]] == [False] * 4
    assert answer["colour"] == "#c8a04a"
    assert not list((tmp_path / "pets").glob("*.tex.png"))


def test_the_photo_style_stores_one_clipped_thumbnail_per_figure(tmp_path: Path) -> None:
    service = _styles(tmp_path)

    answer = json_object(service.generate(_data_url(), "我家猫", "photo"))

    assert answer["ok"] is True
    assert [row["style"] for row in answer["pets"]] == ["photo"] * 4, "行得说自己是什么档"
    assert [row["has_texture"] for row in answer["pets"]] == [True] * 4
    assert len(list((tmp_path / "pets").glob("*.tex.png"))) == 4
    model = json_object(service.model())

    assert model["id"] == "", "生成不等于换上"
    assert model["style"] == ""
    assert model["texture"] == "", "没选中的那一页不该拿到任何图片"


def test_the_outline_style_stays_the_one_that_promises_no_image(tmp_path: Path) -> None:
    service = _styles(tmp_path)

    answer = json_object(service.generate(_data_url(), "我家猫"))

    assert answer["ok"] is True
    assert [row["style"] for row in answer["pets"]] == ["outline"] * 4
    assert [row["has_texture"] for row in answer["pets"]] == [False] * 4
    assert not list((tmp_path / "pets").glob("*.tex.png"))
    assert [row["template"] for row in answer["pets"]] == [""] * 4


def test_an_unknown_style_is_refused_before_the_photo_is_read(tmp_path: Path) -> None:
    service = _styles(tmp_path)

    answer = json_object(service.generate(_data_url(), "我家猫", "anime"))

    assert answer["ok"] is False
    assert "没这种风格" in answer["error"]
    assert answer["pets"] == []


def test_picking_a_photo_figure_is_the_only_way_the_page_gets_pixels(tmp_path: Path) -> None:
    import base64

    service = _styles(tmp_path)
    made = json_object(service.generate(_data_url(), "我家猫", "photo"))["made"]

    json_object(service.pick(made[0]))
    model = json_object(service.model())

    assert base64.b64decode(model["texture"]) == b"TEXTURE"
    assert model["colour"] == "#c8a04a"
    assert model["style"] == "photo", "行里写成线框，页面拿着图也不会贴上去"


# -- the shipped models -------------------------------------------------------


def _bare(tmp_path: Path) -> PetService:
    """A service with no fake segmenter: these tests never generate from a photo."""
    return PetService(PetLibrary(tmp_path / "pets"), Preferences(tmp_path / "preferences.json"))


def _shelf(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *names: str) -> None:
    """Pretend the package ships ``names``: point the registry at an empty shelf and
    write one minimal GLB per name. The real ``.glb`` files are not in git, so a test
    that read them would fail on a fresh clone for the wrong reason."""
    import struct

    from jarvis.app import pet_gallery

    shelf = tmp_path / "shipped-models"
    shelf.mkdir(parents=True, exist_ok=True)
    for name in names:
        payload = json.dumps(
            {"asset": {"version": "2.0"}, "animations": [{"name": "Idle"}] * 7}
        ).encode()
        payload += b" " * (-len(payload) % 4)
        blob = b"\x00" * 8
        (shelf / (name + ".glb")).write_bytes(
            struct.pack("<III", 0x46546C67, 2, 12 + 8 + len(payload) + 8 + len(blob))
            + struct.pack("<II", len(payload), 0x4E4F534A)
            + payload
            + struct.pack("<II", len(blob), 0x004E4942)
            + blob
        )
    monkeypatch.setattr(pet_gallery, "MODELS_DIR", shelf)


def test_the_picker_is_offered_the_models_that_actually_shipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``pet_assets`` carries the shelf so the panel cannot show a button that 404s."""
    service = _bare(tmp_path)
    _shelf(tmp_path, monkeypatch, "Fox", "Wizard")

    answer = json_object(service.assets())

    assert [row["key"] for row in answer["models"]] == ["Fox", "Wizard"]
    assert answer["models"][0]["name"] == "狐狸"
    assert answer["models"][0]["clips"] == 7, "段数是读出来的，不是抄的"


def test_choosing_a_builtin_model_puts_it_on_the_desktop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _bare(tmp_path)
    _shelf(tmp_path, monkeypatch, "Fox")

    answer = json_object(service.pick("builtin:Fox"))

    assert answer["ok"] is True, answer["error"]
    model = json_object(service.model())
    assert model["kind"] == "builtin"
    assert model["id"] == "builtin:Fox"
    assert model["points"] == [], "内置模型不给点位，页面走的是另一条加载路"
    assert model["model"]["url"] == "models/Fox.glb"


def test_a_model_that_is_not_in_the_package_is_refused_on_screen(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _bare(tmp_path)
    _shelf(tmp_path, monkeypatch, "Fox")

    answer = json_object(service.pick("builtin:Ghost"))

    assert answer["ok"] is False
    assert "不在了" in answer["error"]
    assert json_object(service.model())["kind"] == "", "拒了就不能把她换成一个加载不出来的东西"


def test_a_builtin_choice_whose_file_went_missing_falls_back_and_forgets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same rule as a deleted figure: the desktop and the picker must not disagree."""
    service = _bare(tmp_path)
    _shelf(tmp_path, monkeypatch, "Fox")
    json_object(service.pick("builtin:Fox"))
    (tmp_path / "shipped-models" / "Fox.glb").unlink()

    assert service.current_id() == ""
    assert json_object(service.model())["kind"] == ""


def test_a_shipped_model_cannot_be_deleted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Deleting one would mean editing the installed files, so the answer is a sentence."""
    service = _bare(tmp_path)
    _shelf(tmp_path, monkeypatch, "Fox")
    json_object(service.pick("builtin:Fox"))

    answer = json_object(service.remove("builtin:Fox"))

    assert answer["ok"] is False
    assert "删不掉" in answer["error"]
    assert json_object(service.model())["kind"] == "builtin", "被拒之后她当然还是原来那只"
