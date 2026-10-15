"""Video generation boundaries, chat artifacts and shared-GPU exclusion."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

from jarvis.app.chat_service import _artifacts_of
from jarvis.app.image_gen import ImageService
from jarvis.app.media_prompt import Translation, TranslationError
from jarvis.app.preferences import (
    VIDEO_SECONDS,
    VIDEO_SIZE,
    Preferences,
)
from jarvis.app.video_cloud import (
    DEFAULT_BODY,
    DEFAULT_FAILED,
    DEFAULT_HEADERS,
    DEFAULT_KEY_ENV,
    DEFAULT_MODEL,
    DEFAULT_POLL_URL,
    DEFAULT_RUNNING,
    DEFAULT_STATUS_PATH,
    DEFAULT_SUBMIT_URL,
    DEFAULT_SUCCEEDED,
    DEFAULT_TASK_PATH,
    DEFAULT_VIDEO_PATH,
    CloudVideoClient,
    CloudVideoConfig,
    dig,
    render_json_body,
    render_url,
)
from jarvis.app.video_gen import (
    VideoService,
    auto_profile,
    build_video_workflow,
    max_frames_for,
    resolve_cloud_plan,
    resolve_plan,
)
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
    prefs = Preferences(tmp_path / "preferences.json")
    images = ImageService(prefs, paths, translator=_Translator())
    video = VideoService(images, prefs, paths)
    probe = {
        "ok": True,
        "url": "http://127.0.0.1:1234",
        "checkpoints": ["sd15.safetensors"],
        "vram_total_mb": 4096.0,
        "vram_free_mb": 1979.0,
    }
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


# -- r114：分辨率/帧率/时长可调，默认按本机显存 --------------------------------


def test_auto_profile_steps_by_vram() -> None:
    assert auto_profile(4096) == ("256x256", 8, 2)
    assert auto_profile(8192) == ("384x384", 12, 4)
    assert auto_profile(12288) == ("512x512", 16, 8)


def test_frame_cap_anchored_on_the_measured_4gb_point() -> None:
    # r113 实测锚点：4 GB 卡 32 帧@256×256 装得下。公式必须原样复现这个数。
    assert max_frames_for(4096, 256, 256) == 32
    # 同尺寸显存翻倍 → 帧数翻倍；同显存像素翻倍 → 帧数减半（外推，界面上有声明）。
    assert max_frames_for(8192, 256, 256) == 64
    assert max_frames_for(4096, 512, 512) == 8
    assert max_frames_for(24576, 256, 256) == 196
    assert max_frames_for(49152, 256, 256) == 256  # 绝对上限 256 帧


def test_plan_prefers_explicit_then_prefs_then_auto(tmp_path: Path) -> None:
    prefs = Preferences(tmp_path / "preferences.json")
    auto = resolve_plan(prefs, 4096)
    assert (auto["size"], auto["fps"], auto["frames"], auto["steps"]) == ("256x256", 8, 16, 16)
    assert auto["explicit"] is False and auto["capped"] is False

    prefs.set(VIDEO_SIZE, "512x512")
    from_pref = resolve_plan(prefs, 4096)
    assert from_pref["size"] == "512x512" and from_pref["explicit"] is True
    assert from_pref["capped"] is True  # 4 GB 装不下 2 秒@512 的 16 帧，截到 8 帧
    assert from_pref["frames"] == 8 and "已截到" in from_pref["note"]

    explicit = resolve_plan(prefs, 4096, {"size": "256x256", "seconds": 4, "fps": 8})
    assert (explicit["size"], explicit["frames"]) == ("256x256", 32)


def test_workflow_carries_size_fps_and_frames() -> None:
    graph = build_video_workflow(
        "sd15.safetensors", "a cat", 7, 64, 16, width=384, height=384, fps=12
    )
    assert graph["4"]["inputs"]["width"] == 384
    assert graph["4"]["inputs"]["batch_size"] == 64
    assert graph["7"]["inputs"]["frame_rate"] == 12


def test_generate_uses_plan_and_reports_actual_shape(tmp_path: Path, monkeypatch: Any) -> None:
    video, images, sent = _service(tmp_path, monkeypatch)
    images._prefs.set(VIDEO_SIZE, "256x256")
    images._prefs.set(VIDEO_SECONDS, 4)
    answer = video.generate("一只橘猫散步")
    assert answer["ok"] is True
    assert answer["frames"] == 32 and answer["fps"] == 8 and answer["size"] == "256x256"
    assert answer["duration"] == 4.0 and answer["capped"] is False
    assert sent["4"]["inputs"]["batch_size"] == 32
    assert sent["7"]["inputs"]["frame_rate"] == 8


def test_legacy_frames_still_maps_to_a_plan(tmp_path: Path, monkeypatch: Any) -> None:
    video, _, sent = _service(tmp_path, monkeypatch)
    answer = video.generate("猫", frames=24, steps=16)
    assert answer["ok"] is True and answer["frames"] == 24
    assert sent["4"]["inputs"]["batch_size"] == 24


def test_setters_validate_and_roundtrip(tmp_path: Path, monkeypatch: Any) -> None:
    video, _, _ = _service(tmp_path, monkeypatch)
    assert video.set_size("640x640") != ""
    assert video.set_fps(9) != ""
    assert video.set_seconds(5) != ""
    assert video.set_steps(40) != ""
    assert video.set_size("384x384") == ""
    assert video.set_fps(16) == ""
    assert video.set_seconds(8) == ""
    assert video.set_steps(20) == ""
    status = video.status()
    assert status["choice"] == {"size": "384x384", "fps": 16, "seconds": 8, "steps": 20}
    assert status["plan"]["size"] == "384x384" and status["plan"]["steps"] == 20
    assert video.set_size("auto") == ""
    assert video.status()["choice"]["size"] == ""
    assert video.status()["options"]["sizes"] == ["256x256", "384x384", "512x512"]


def test_bridge_tune_and_probe_do_not_need_the_engine(tmp_path: Path, monkeypatch: Any) -> None:
    video, images, _ = _service(tmp_path, monkeypatch)
    bridge = HudBridge.__new__(HudBridge)
    bridge._videos = video
    bridge._paths = images._paths
    status = bridge.video_tune("512x512", 8, 2, 16)
    assert status["plan"]["size"] == "512x512"
    assert bridge.video_generate("猫", None, None, "256x256", None, 2)["ok"] is True
    probed = bridge.video_probe()
    assert probed["plan"]["vram_total_mb"] == 4096.0
    words = {spec.name: h for spec, h in video_tools.build(video)}["video_status"]({})
    assert "256/384/512" in words


def test_chat_tool_carry_size_and_seconds(tmp_path: Path, monkeypatch: Any) -> None:
    video, _, sent = _service(tmp_path, monkeypatch)
    handlers = {spec.name: handler for spec, handler in video_tools.build(video)}
    words = handlers["generate_video"](
        {"prompt": "一只猫", "size": "256x256", "fps": 8, "seconds": 4, "steps": 12}
    )
    assert "存在：" in words and "4.0 秒" in words
    assert sent["4"]["inputs"]["batch_size"] == 32
    assert sent["5"]["inputs"]["steps"] == 12


# -- r114：云端视频（多家可自填、异步提交→轮询→下载） --------------------------


class _Resp:
    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    def __enter__(self) -> _Resp:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self, _size: int = -1) -> bytes:
        return self._payload


def _fake_opener(responses: list[bytes]) -> tuple[Any, list[str]]:
    seen: list[str] = []

    def opener(request: Any, timeout: float) -> _Resp:
        seen.append(request.full_url)
        index = len(seen) - 1
        return _Resp(responses[min(index, len(responses) - 1)])

    return opener, seen


_MP4 = b"\x00\x00\x00\x20ftyp" + b"0" * 40


def _default_config() -> CloudVideoConfig:
    return CloudVideoConfig(
        submit_url=DEFAULT_SUBMIT_URL,
        key_env=DEFAULT_KEY_ENV,
        model=DEFAULT_MODEL,
        body=DEFAULT_BODY,
        headers=DEFAULT_HEADERS,
        task_path=DEFAULT_TASK_PATH,
        poll_url=DEFAULT_POLL_URL,
        video_path=DEFAULT_VIDEO_PATH,
        status_path=DEFAULT_STATUS_PATH,
        succeeded=DEFAULT_SUCCEEDED,
        running=DEFAULT_RUNNING,
        failed=DEFAULT_FAILED,
    )


def test_render_json_body_inserts_values_without_quotes() -> None:
    out = render_json_body(
        DEFAULT_BODY,
        {
            "prompt": 'a "cat" runs',
            "model": "wanx2.1-t2v-turbo",
            "width": 256,
            "height": 256,
            "seconds": 5,
            "fps": 8,
            "frames": 40,
        },
    )
    parsed = json.loads(out)
    assert parsed["input"]["prompt"] == 'a "cat" runs'  # 引号由 json 转义，模板不被撑坏
    assert parsed["parameters"]["duration"] == 5 and parsed["parameters"]["fps"] == 8
    assert parsed["parameters"]["size"] == "256*256"


def test_dig_walks_dotted_and_indexed_paths() -> None:
    payload = {"output": {"results": [{"url": "http://x/a.mp4"}]}}
    assert dig(payload, "output.results[0].url") == "http://x/a.mp4"
    assert dig(payload, "output.results[-1].url") == "http://x/a.mp4"
    assert dig(payload, "output.missing.url") is None
    assert dig(payload, "output.results[9].url") is None


def test_render_url_encodes_task_id() -> None:
    assert render_url("https://p/tasks/{{task_id}}", {"task_id": "a b/c"}) == (
        "https://p/tasks/a%20b%2Fc"
    )


def test_cloud_client_submits_polls_and_downloads() -> None:
    submit = json.dumps({"output": {"task_id": "t1"}}).encode()
    pending = json.dumps({"output": {"task_status": "PENDING"}}).encode()
    done = json.dumps(
        {"output": {"task_status": "SUCCEEDED", "video_url": "https://cdn/x.mp4"}}
    ).encode()
    opener, seen = _fake_opener([submit, pending, done, _MP4])
    client = CloudVideoClient(
        _default_config(),
        {DEFAULT_KEY_ENV: "sk-test"},
        opener=opener,
        sleep=lambda _s: None,
    )
    blob, trace = client.generate(
        "一只橘猫在雪地跑步", width=256, height=256, seconds=2, fps=8, frames=16
    )
    assert blob == _MP4 and trace["task_id"] == "t1"
    assert seen[0] == DEFAULT_SUBMIT_URL and seen[3] == "https://cdn/x.mp4"


def test_cloud_client_sync_without_poll_url() -> None:
    submit = json.dumps({"data": [{"url": "https://cdn/one.mp4"}]}).encode()
    config = CloudVideoConfig(
        **{**_default_config().__dict__, "poll_url": "", "video_path": "data[0].url"}
    )
    opener, seen = _fake_opener([submit, _MP4])
    client = CloudVideoClient(config, {DEFAULT_KEY_ENV: "k"}, opener=opener)
    blob, _trace = client.generate("cat", width=256, height=256, seconds=2, fps=8, frames=16)
    assert blob == _MP4 and len(seen) == 2  # 一次提交 + 一次下载，没有轮询


def test_cloud_client_missing_key_is_a_plain_sentence() -> None:
    import pytest

    from jarvis.app.image_gen import _EngineError

    client = CloudVideoClient(_default_config(), {}, opener=_fake_opener([b"{}"])[0])
    with pytest.raises(_EngineError, match=DEFAULT_KEY_ENV):
        client.generate("cat", width=256, height=256, seconds=2, fps=8, frames=16)


def test_cloud_client_reports_task_id_fetch_failure() -> None:
    import pytest

    from jarvis.app.image_gen import _EngineError

    submit = json.dumps({"request_id": "abc"}).encode()  # 没有 output.task_id
    opener, _seen = _fake_opener([submit])
    client = CloudVideoClient(
        _default_config(), {DEFAULT_KEY_ENV: "k"}, opener=opener, sleep=lambda _s: None
    )
    with pytest.raises(_EngineError, match="没拿到任务 id"):
        client.generate("cat", width=256, height=256, seconds=2, fps=8, frames=16)


def test_cloud_client_downloads_only_valid_mp4() -> None:
    import pytest

    from jarvis.app.image_gen import _EngineError

    submit = json.dumps({"output": {"video_url": "https://cdn/x.mp4"}}).encode()
    opener, _seen = _fake_opener([submit, b"not a video"])
    config = CloudVideoConfig(**{**_default_config().__dict__, "poll_url": ""})
    client = CloudVideoClient(config, {DEFAULT_KEY_ENV: "k"}, opener=opener)
    with pytest.raises(_EngineError, match="MP4"):
        client.generate("cat", width=256, height=256, seconds=2, fps=8, frames=16)


class _StubClient:
    """替掉真网络：服务只测派活与落盘，不测云端本身。"""

    def __init__(self, config: CloudVideoConfig, environ: Any, **_kw: Any) -> None:
        self.config = config

    def generate(
        self,
        prompt: str,
        *,
        width: int,
        height: int,
        seconds: int,
        fps: int,
        frames: int,
        should_stop: Any = None,
    ) -> tuple[bytes, dict[str, Any]]:
        assert callable(should_stop)
        return _MP4, {}


def _cloud_service(tmp_path: Path, monkeypatch: Any) -> tuple[VideoService, ImageService]:
    from jarvis.app import video_gen

    video, images, _sent = _service(tmp_path, monkeypatch)
    images._environ = {DEFAULT_KEY_ENV: "sk-test"}
    monkeypatch.setattr(video_gen, "CloudVideoClient", _StubClient)
    return video, images


def test_cloud_plan_does_not_cap_by_local_vram(tmp_path: Path) -> None:
    prefs = Preferences(tmp_path / "preferences.json")
    plan = resolve_cloud_plan(prefs, {"size": "512x512", "seconds": 32, "fps": 24})
    assert plan["size"] == "512x512" and plan["frames"] == 32 * 24  # 云端不按本机显存截


def test_engine_defaults_to_local_and_cloud_needs_a_key(tmp_path: Path, monkeypatch: Any) -> None:
    video, _images = _cloud_service(tmp_path, monkeypatch)
    assert video._engine_choice() == "local"
    assert video.set_engine("kling") != ""  # 只认 auto/local/cloud
    assert video.set_engine("cloud") == "" and video._engine_choice() == "cloud"
    assert video.cloud_ready() is True  # 有钥匙 + 出厂默认端点齐


def test_cloud_generation_writes_mp4_and_skips_translation(
    tmp_path: Path, monkeypatch: Any
) -> None:
    video, images = _cloud_service(tmp_path, monkeypatch)
    video.set_engine("cloud")
    answer = video.generate("一只橘猫在雪地跑步", size="256x256", fps=8, seconds=2)
    assert answer["ok"] is True and answer["engine"] == "cloud"
    assert Path(answer["path"]).is_file() and answer["path"].endswith(".mp4")
    assert answer["prompt"] == "一只橘猫在雪地跑步"  # 原话直发，没翻英文
    assert cast(_Translator, images.translator).inputs == []  # 云端这条路一次翻译都没发


def test_cloud_refuses_without_key_on_screen(tmp_path: Path, monkeypatch: Any) -> None:
    video, images = _cloud_service(tmp_path, monkeypatch)
    images._environ = {}  # 没设环境变量
    video.set_engine("cloud")
    answer = video.generate("猫")
    assert answer["ok"] is False and DEFAULT_KEY_ENV in answer["error"]


def test_bridge_engine_and_cloud_save(tmp_path: Path, monkeypatch: Any) -> None:
    video, images = _cloud_service(tmp_path, monkeypatch)
    bridge = HudBridge.__new__(HudBridge)
    bridge._videos = video
    bridge._paths = images._paths
    status = bridge.video_set_engine("cloud")
    assert status["engine"] == "cloud" and status["cloud"]["key_set"] is True
    saved = bridge.video_cloud_save({"model": "kling-v1", "poll_url": ""})
    assert saved["cloud"]["model"] == "kling-v1"
    bad = bridge.video_cloud_save({"headers": "{not json"})
    assert bad.get("error")  # 坏 JSON 模板当场拒，不存进去等下次崩
    reset = bridge.video_cloud_reset()
    assert reset["cloud"]["model"] == DEFAULT_MODEL  # 清空退回出厂默认


def test_status_exposes_engine_and_cloud_defaults(tmp_path: Path, monkeypatch: Any) -> None:
    video, _images = _cloud_service(tmp_path, monkeypatch)
    status = video.status()
    assert status["options"]["engines"] == ["auto", "local", "cloud"]
    assert status["cloud_defaults"]["submit_url"] == DEFAULT_SUBMIT_URL
    assert "多家可自填" in video.capabilities()


# -- r114：停止生成 + 重启引擎（共用生图那条取消位与引擎） ----------------------


def test_video_cancel_delegates_to_the_shared_engine(tmp_path: Path, monkeypatch: Any) -> None:
    video, images, _ = _service(tmp_path, monkeypatch)
    assert "没有正在生成" in video.cancel()  # 此刻没在跑，只置位
    assert images._cancel.is_set()


def test_video_generate_clears_a_stale_cancel(tmp_path: Path, monkeypatch: Any) -> None:
    video, images, _ = _service(tmp_path, monkeypatch)
    images._cancel.set()
    answer = video.generate("一只橘猫散步", size="256x256", fps=8, seconds=2)
    assert answer["ok"] is True
    assert images._cancel.is_set() is False


def test_video_restart_returns_status_without_spawning(tmp_path: Path, monkeypatch: Any) -> None:
    video, images, _ = _service(tmp_path, monkeypatch)
    status = video.restart_engine()
    assert "plan" in status  # 回的是整份视频状态
    assert images._proc is None  # 没指 comfy_dir：重启不该真去起进程
