from pathlib import Path

from jarvis.config.full_bundle import FULL_COMPONENTS, full_bundle_problems


def test_complete_delivery_cannot_silently_omit_media_or_voice(tmp_path: Path) -> None:
    assert full_bundle_problems(tmp_path)
    for relative in [*FULL_COMPONENTS.values(), "模型/local.gguf"]:
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"fixture")
    assert full_bundle_problems(tmp_path) == []
    (tmp_path / FULL_COMPONENTS["视频模型"]).unlink()
    assert any("视频模型" in error for error in full_bundle_problems(tmp_path))
