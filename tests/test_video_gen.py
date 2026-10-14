"""Video generation boundaries, chat artifacts and shared-GPU exclusion."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from jarvis.app.chat_service import _artifacts_of
from jarvis.app.image_gen import ImageService
from jarvis.app.media_prompt import Translation, TranslationError
from jarvis.app.preferences import Preferences
from jarvis.app.video_gen import VideoService
from jarvis.config.paths import AppPaths
from jarvis.tools.builtins import video_tools
from jarvis.ui.desktop import HudBridge


class _Translator:
    def __init__(self, failure: bool = False) -> None:
        self.inputs: list[str] = []
        self.failure = failure

    def translate(self, text: str) -> Translation:
        self.inputs.append(text)
        if self.failure:
            raise TranslationError("翻译失败，尚未提交给模型")
        return Translation(text, "Two red birds fly left of a blue cat, no hats", "fr")


def _service(tmp_path: Path, monkeypatch: Any) -> tuple[VideoService, ImageService, dict[str, Any]]:
    paths = AppPaths.resolve({"JARVIS_HOME": str(tmp_path)})
    images = ImageService(
        Preferences(tmp_path / "preferences.json"), paths, translator=_Translator()
    )
    video = VideoService(images, paths)
    probe = {"ok": True, "url": "http://127.0.0.1:1234", "checkpoints": ["sd15.safetensors"]}
    monkeypatch.setattr(images, "_find_engine", lambda: probe)
    nodes: dict[str, Any] = {
        name: {}
        for name in (
            "ADE_AnimateDiffLoaderGen1",
            "ADE_StandardUniformContextOptions",
            "VHS_VideoCombine",
        )
    }
    monkeypatch.setattr(images, "_get_json", lambda *args: nodes)
    sent: dict[str, Any] = {}

    def submit(base: str, graph: dict[str, Any]) -> str:
        sent.update(graph)
        return "job"

    monkeypatch.setattr(images, "_submit", submit)
    monkeypatch.setattr(
        video, "_wait", lambda *args: ("clip.mp4", b"\x00\x00\x00\x20ftyp" + b"0" * 40)
    )
    video.start()
    return video, images, sent


def test_bridge_and_chat_use_the_same_translation_and_mp4(tmp_path: Path, monkeypatch: Any) -> None:
    video, images, sent = _service(tmp_path, monkeypatch)
    bridge = HudBridge.__new__(HudBridge)
    bridge._videos = video
    bridge._paths = images._paths
    result = bridge.video_generate(
        "Deux oiseaux rouges à gauche d’un chat bleu, sans chapeau", 16, 8
    )
    assert result["ok"] and Path(result["path"]).is_file()
    assert sent["2"]["inputs"]["text"] == result["prompt"]
    assert sent["4"]["inputs"]["batch_size"] == 16
    assert str(bridge.artifact_video(result["path"])["data_url"]).startswith(
        "data:video/mp4;base64,"
    )
    assert bridge.artifact_video(str(tmp_path / "secret.mp4"))["ok"] is False
    handlers = {spec.name: handler for spec, handler in video_tools.build(video)}
    words = handlers["generate_video"]({"prompt": "原始视频描述"})
    assert _artifacts_of("generate_video", words) == (result["path"],)


def test_translation_failure_never_submits_or_leaks_lock(tmp_path: Path, monkeypatch: Any) -> None:
    video, images, sent = _service(tmp_path, monkeypatch)
    images.translator = _Translator(failure=True)
    assert video.generate("猫")["ok"] is False
    assert sent == {} and not images.generation_lock.locked()


def test_image_and_video_cannot_overlap(tmp_path: Path, monkeypatch: Any) -> None:
    video, images, sent = _service(tmp_path, monkeypatch)
    images.generation_lock.acquire()
    try:
        assert video.generate("猫")["ok"] is False
        assert images.generate("cat")["ok"] is False
        assert sent == {}
    finally:
        images.generation_lock.release()


def test_missing_video_nodes_stop_before_submission(tmp_path: Path, monkeypatch: Any) -> None:
    video, images, sent = _service(tmp_path, monkeypatch)
    monkeypatch.setattr(images, "_get_json", lambda *args: {})
    answer = video.generate("猫")
    assert not answer["ok"] and "视频组件" in answer["error"] and not sent


def test_poll_reports_execution_error(tmp_path: Path, monkeypatch: Any) -> None:
    video, images, _ = _service(tmp_path, monkeypatch)
    monkeypatch.setattr(
        images,
        "_get_json",
        lambda *args: {
            "job": {
                "status": {
                    "status_str": "error",
                    "messages": [
                        ["execution_error", {"exception_message": "GPU memory exhausted"}]
                    ],
                }
            }
        },
    )
    import pytest

    from jarvis.app.image_gen import _EngineError

    with pytest.raises(_EngineError, match="GPU memory exhausted"):
        VideoService._wait(video, "http://local", "job")
