import json
from pathlib import Path

import pytest
from scripts.derive_runtime_edition import derive


def test_edition_copy_changes_metadata_without_sharing_mutable_files(tmp_path: Path) -> None:
    source = tmp_path / "author"
    metadata = source / "_internal/jarvis/buildinfo.json"
    metadata.parent.mkdir(parents=True)
    metadata.write_text(
        json.dumps({"edition": "author", "source_sha256": "fixture"}), encoding="utf-8"
    )
    (source / "小夜.exe").write_bytes(b"fixture")
    mutable = source / "_internal/runtime.py"
    mutable.write_text("original", encoding="utf-8")
    destination = tmp_path / "buyer"
    derive(source, destination, "buyer")
    copied = json.loads(
        (destination / "_internal/jarvis/buildinfo.json").read_text(encoding="utf-8")
    )
    assert copied["edition"] == "buyer"
    assert json.loads(metadata.read_text(encoding="utf-8"))["edition"] == "author"
    (destination / "_internal/runtime.py").write_text("changed", encoding="utf-8")
    assert mutable.read_text(encoding="utf-8") == "original"
    with pytest.raises(ValueError):
        derive(source, destination, "buyer")
    (source / "用户数据").mkdir()
    with pytest.raises(ValueError, match="user data"):
        derive(source, tmp_path / "unsafe", "buyer")
