"""The shipped-model registry: what gets offered, and what happens when a file vanishes.

These models are not in git (see ``.gitignore``), so the tests cannot depend on the real
``.glb`` files being on disk -- a fresh clone would fail for reasons that have nothing to
do with this code. Everything here points ``MODELS_DIR`` at a temporary directory and
writes a two-chunk GLB by hand, which is also the only way to prove the clip count is
*read* rather than remembered.
"""

from __future__ import annotations

import json
import struct
from pathlib import Path

import pytest

from jarvis.app import pet_gallery


def _glb(tmp_path: Path, name: str, clips: int) -> Path:
    """A GLB whose JSON chunk declares ``clips`` animations and nothing else."""
    ring = [{"name": f"Idle_{i}"} for i in range(clips)]
    model = {"asset": {"version": "2.0"}, "animations": ring}
    payload = json.dumps(model).encode("utf-8")
    payload += b" " * (-len(payload) % 4)
    blob = b"\x00" * 8
    path = tmp_path / (name + ".glb")
    path.write_bytes(
        struct.pack("<III", 0x46546C67, 2, 12 + 8 + len(payload) + 8 + len(blob))
        + struct.pack("<II", len(payload), 0x4E4F534A)
        + payload
        + struct.pack("<II", len(blob), 0x004E4942)
        + blob
    )
    return path


@pytest.fixture()
def shelf(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A package that ships two of the three known models."""
    monkeypatch.setattr(pet_gallery, "MODELS_DIR", tmp_path)
    _glb(tmp_path, "Fox", 12)
    _glb(tmp_path, "Wizard", 15)
    return tmp_path


def test_only_the_models_whose_files_shipped_are_offered(
    shelf: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A picker entry that 404s is worse than a smaller picker.

    The registry knows eleven names; this package carries two files. The third name has
    to be *absent* from the list and named in the log, not silently offered as a button
    that cannot load.
    """
    offered = [entry.model_id for entry in pet_gallery.models()]

    assert offered == ["Fox", "Wizard"], "顺序就是 KNOWN 里的顺序，缺的那个不列"
    assert "没有随包" in caplog.text


def test_the_clip_count_is_read_off_the_file_not_a_constant(shelf: Path) -> None:
    """「12 段动作」 is shown on screen, so it has to come from the model.

    The number in the file here is deliberately different from anything the registry
    could have remembered: if the count ever becomes a hard-coded field, this test is
    the one that notices.
    """
    fox = next(entry for entry in pet_gallery.models() if entry.model_id == "Fox")

    assert fox.clips == 12
    assert fox.bytes == (shelf / "Fox.glb").stat().st_size
    assert fox.to_public()["clips"] == 12


def test_a_corrupt_file_reports_zero_clips_instead_of_raising(
    shelf: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A half-written download should cost one card's worth of detail, not the panel."""
    (shelf / "Fox.glb").write_bytes(b"glTF\x02\x00\x00\x00\x1c\x00\x00\x00")

    fox = next(entry for entry in pet_gallery.models() if entry.model_id == "Fox")

    assert fox.clips == 0
    assert fox.bytes == 12
    assert "读不出动画数" in caplog.text


def test_a_choice_is_stored_under_a_prefix_that_cannot_collide_with_generated_ids() -> None:
    """``pet:<hex>`` is a generated figure; ``builtin:<Key>`` is a shipped model."""
    entry = pet_gallery.GalleryModel("Fox", "狐狸", "动物", "", 1, 2)

    assert entry.preference == "builtin:Fox"
    assert pet_gallery.resolve("pet:9f3a") is None
    assert pet_gallery.resolve("") is None


def test_a_builtin_that_is_no_longer_in_the_package_stops_resolving(shelf: Path) -> None:
    """Dropping a model from a release must not leave it claimed as the desktop figure."""
    assert pet_gallery.resolve("builtin:Fox") is not None
    (shelf / "Fox.glb").unlink()

    assert pet_gallery.resolve("builtin:Fox") is None


def test_every_name_in_the_registry_has_a_known_home() -> None:
    """The list is hand-written twice: here and in the fetch script. This keeps them one."""
    import importlib.util

    script = Path(__file__).resolve().parent.parent / "scripts" / "fetch_gallery_models.py"
    spec = importlib.util.spec_from_file_location("fetch_gallery_models", script)
    assert spec and spec.loader
    fetcher = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fetcher)

    assert {name for name, _ in fetcher.WANTED.items()} == {entry[0] for entry in pet_gallery.KNOWN}
    assert len({entry[0] for entry in pet_gallery.KNOWN}) == len(pet_gallery.KNOWN), "id 重复了"
    for model_id, label, kind, _note in pet_gallery.KNOWN:
        assert label and kind in {"动物", "幻想", "人物"}, model_id
