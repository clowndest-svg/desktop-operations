"""生图这一层的测试：假 ComfyUI 和假云端都是**真开一个监听端口**。

不在这里 mock 掉 HTTP：要验的东西是"工作流发出去长什么样、回包怎么解析、连不上时那句话
怎么说"，这三样全都活在 HTTP 那一层。手搓一个假字典等于把被测代码替掉一半（记忆「测真接线」：
读屏那回假的 ``devices()`` 让测试在"去重"逻辑改了之后静静变成一条永真断言）。

云端那一半也一样：请求头里有没有钥匙、401 的文案点不点名变量、只给 url 时会不会去取图，
只有真收到请求才知道。
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import socket
import threading
from collections.abc import Iterator, Sequence
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlparse
from urllib.request import urlopen

import pytest

from jarvis.app import image_gen
from jarvis.app.chat_service import ChatService
from jarvis.app.image_gen import (
    IMAGE_KEY_ENV,
    ImageService,
    build_workflow,
    parse_cloud_answer,
    parse_size,
)
from jarvis.app.preferences import (
    IMAGE_CLOUD_BASE,
    IMAGE_COMFY_URL,
    IMAGE_ENGINE,
    IMAGE_KEEP_OUTPUT,
    Preferences,
)
from jarvis.app.tool_routing import PACKS, classify_tool_intent
from jarvis.config.paths import AppPaths
from jarvis.config.schema import ToolsSection
from jarvis.llm.types import ChatMessage, ChatResponse, GenerationOptions, ToolCall
from jarvis.tools.builtins import image_tools
from jarvis.tools.disk_cleaner import DiskCleaner
from jarvis.tools.monitor import SystemMonitor
from jarvis.tools.service import ToolService

#: 96x96 的一张真 PNG，不是 1x1 占位：预览那条路要真的缩一次图才算走过。
_PROXY_KEYS = ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy")

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAGAAAABgCAIAAABt+uBvAAABRElEQVR4nO3ZMUpEURBE0dvDLMHYdbp8"
    "0c4UXyCILziFaMfFFOfPd15e3x71+TOOx7cSnh+/PjL7x/GljS1IO/1UwrOHdjp8REys84BMbA7tmNic"
    "2zGxjYn1e4gotjGxfvkhoticazKxjYllYv3JdyYT25hYJpaJ9Q9vZrzuyINiFMvE+q8X576LzbkmD4ob"
    "imVieVDMg2LX/ReTYnOuiWIbimViUSyKdd1BsTnXRLENxTKxKBbFuu6g2JxrotiGYplYFItiXXdQbM41"
    "UWxDsUwsikWxrjsoNueaKLahWCYWxaJY1x0Um3NNFNtQLBOLYlGs6w6Kzbkmim0ololFsSjWdQfF5lwT"
    "xTYUy8SiWBTruoNic66JYhuKZWJRLIp13UGxOddEsQ3FMrEoFsW67qDYnGui2IZimVgUi2Jdd1BszjW9"
    "A3WGD6M/xSE4AAAAAElFTkSuQmCC"
)


class FakeEngine:
    """一个假装自己是 ComfyUI（顺带也假装是云端画图端点）的本机服务。"""

    def __init__(self) -> None:
        self.version = "0.39.0"
        self.checkpoints = ["anythingV3_fp16.safetensors"]
        self.history_polls = 1
        self.history_outputs = True
        self.prompt_status = 200
        self.view_status = 200
        self.cloud_mode = "b64"
        self.cloud_status = 200
        self.submitted: dict[str, Any] = {}
        self.cloud_bodies: list[dict[str, Any]] = []
        self.cloud_headers: list[dict[str, str]] = []
        self._polls = 0

    def handle(
        self, method: str, path: str, body: bytes, headers: dict[str, str]
    ) -> tuple[int, bytes, str]:
        route = urlparse(path).path
        if route == "/system_stats":
            return (
                200,
                json.dumps(
                    {"system": {"comfyui_version": self.version}, "devices": [{"type": "cuda"}]}
                ).encode(),
                "application/json",
            )
        if route == "/object_info/CheckpointLoaderSimple":
            return (
                200,
                json.dumps(
                    {
                        "CheckpointLoaderSimple": {
                            "input": {"required": {"ckpt_name": [list(self.checkpoints)]}}
                        }
                    }
                ).encode(),
                "application/json",
            )
        if route == "/prompt" and method == "POST":
            payload = json.loads(body.decode())
            graph = payload.get("prompt", {})
            self.submitted = {
                "client_id": payload.get("client_id"),
                "checkpoint": graph.get("1", {}).get("inputs", {}).get("ckpt_name"),
                "positive": graph.get("2", {}).get("inputs", {}).get("text"),
                "negative": graph.get("3", {}).get("inputs", {}).get("text"),
                "latent": graph.get("4", {}).get("inputs", {}),
                "seed": graph.get("5", {}).get("inputs", {}).get("seed"),
                "steps": graph.get("5", {}).get("inputs", {}).get("steps"),
                "node_count": len(graph),
            }
            self._polls = 0
            if self.prompt_status != 200:
                return self.prompt_status, b'{"error": "loader failed"}', "application/json"
            return 200, json.dumps({"prompt_id": "p1", "number": 1}).encode(), "application/json"
        if route.startswith("/history/"):
            self._polls += 1
            if self._polls <= self.history_polls:
                return 200, b"{}", "application/json"
            if not self.history_outputs:
                return (
                    200,
                    json.dumps({"p1": {"outputs": {}, "status": {"status_str": "error"}}}).encode(),
                    "application/json",
                )
            image = {"filename": "xiaoye_1_00001_.png", "subfolder": "", "type": "output"}
            return (
                200,
                json.dumps(
                    {"p1": {"outputs": {"7": {"images": [image]}}, "status": {"completed": True}}}
                ).encode(),
                "application/json",
            )
        if route == "/view":
            if self.view_status != 200:
                return self.view_status, b"", "image/png"
            return 200, PNG, "image/png"
        if route == "/v1/images/generations" and method == "POST":
            self.cloud_bodies.append(json.loads(body.decode()))
            self.cloud_headers.append({key.lower(): value for key, value in headers.items()})
            if self.cloud_status != 200:
                return (
                    self.cloud_status,
                    json.dumps(
                        {"error": {"message": "余额不足", "code": "insufficient_quota"}}
                    ).encode(),
                    "application/json",
                )
            if self.cloud_mode == "b64":
                return (
                    200,
                    json.dumps({"data": [{"b64_json": base64.b64encode(PNG).decode()}]}).encode(),
                    "application/json",
                )
            if self.cloud_mode == "url":
                host = headers.get("Host", "")
                return (
                    200,
                    json.dumps({"data": [{"url": f"http://{host}/download.png"}]}).encode(),
                    "application/json",
                )
            if self.cloud_mode == "empty":
                return 200, b'{"data": []}', "application/json"
            return (
                200,
                json.dumps({"error": {"message": "内容被过滤"}}).encode(),
                "application/json",
            )
        if route == "/download.png":
            return 200, PNG, "image/png"
        return 404, b"{}", "application/json"


class _Handler(BaseHTTPRequestHandler):
    engine: FakeEngine

    def _go(self, method: str) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        headers = dict(self.headers.items())
        code, payload, content_type = self.engine.handle(method, self.path, body, headers)
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:
        self._go("GET")

    def do_POST(self) -> None:
        self._go("POST")

    def log_message(self, *args: object) -> None:
        del args  # 测试里不往 stdout 刷一行行请求日志


class CountingOpener:
    """真的 urlopen，只是数着自己被调了几次 —— 「轮询不许发网络请求」那条就靠它。"""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, request: Any, timeout: float) -> Any:
        self.calls += 1
        return urlopen(request, timeout=timeout)


class FakeProc:
    """假引擎子进程。``exit_code`` 决定它是不是一直活着。"""

    def __init__(self, exit_code: int | None = None) -> None:
        self.exit_code = exit_code
        self.terminated = False
        self.stdout = _Pipe()

    def poll(self) -> int | None:
        return self.exit_code

    def wait(self, timeout: float | None = None) -> int:
        del timeout
        return self.exit_code if self.exit_code is not None else 0

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.terminated = True


class _Pipe:
    def __init__(self) -> None:
        self.closed = False

    def readline(self) -> bytes:
        if self.closed:
            return b""
        self.closed = True
        return b"Started server\n"


class _Server(ThreadingHTTPServer):
    """带一个假引擎的服务器：属性写在类上，mypy 才知道它真存在。"""

    engine: FakeEngine


class Harness:
    def __init__(self, tmp_path: Path, server: _Server) -> None:
        self.server = server
        self.url = f"http://127.0.0.1:{server.server_address[1]}"
        self.engine = server.engine
        self.prefs = Preferences(tmp_path / "preferences.json")
        self.requests = CountingOpener()
        self.spawned: list[tuple[list[str], Path]] = []
        self.proc = FakeProc()
        self.paths = AppPaths.resolve({"JARVIS_HOME": str(tmp_path / "数据")})
        self.service = ImageService(
            self.prefs,
            self.paths,
            environ={IMAGE_KEY_ENV: "sk-test-key"},
            opener=self.requests,
            spawn=cast("Any", self._spawn),
        )
        self.service.start()

    def _spawn(self, argv: list[str], cwd: Path) -> FakeProc:
        """记下发出去的那一行命令，然后把假进程交回去（服务的接缝要的就是这两件事）。"""
        self.spawned.append((list(argv), cwd))
        return self.proc

    def use_local(self) -> None:
        """把档位钉在本地，地址指到这个假引擎。"""
        self.prefs.set(IMAGE_ENGINE, "local")
        self.prefs.set(IMAGE_COMFY_URL, self.url)

    def use_cloud(self) -> None:
        self.prefs.set(IMAGE_ENGINE, "cloud")
        self.prefs.set(IMAGE_CLOUD_BASE, self.url + "/v1")

    def fake_comfyui_on_disk(self, tmp_path: Path) -> Path:
        """盘上摆一个"看起来像 ComfyUI"的目录：main.py + 旁边的 venv python。"""
        root = tmp_path / "ComfyUI"
        (root / "venv" / "Scripts").mkdir(parents=True, exist_ok=True)
        (root / "main.py").write_text("x", encoding="utf-8")
        (root / "venv" / "Scripts" / "python.exe").write_text("x", encoding="utf-8")
        return root

    def dead_address(self) -> None:
        """一个真没人听的端口。

        不能用端口 1：``_engine_port()`` 认不出个位数的端口，会退回 ``COMFY_PORTS[0]``，
        而作者这台机器上 8188 **真有一个 ComfyUI 在跑** —— 第一次就这么撞上去，
        测试画的其实是那台真引擎。这里现拿一个空闲端口，拿完就还回去。
        """
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            free = int(probe.getsockname()[1])
        self.prefs.set(IMAGE_ENGINE, "local")
        self.prefs.set(IMAGE_COMFY_URL, f"http://127.0.0.1:{free}")

    def close(self) -> None:
        self.service.stop()


@pytest.fixture
def hub(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Harness]:
    server = _Server(("127.0.0.1", 0), _Handler)
    engine = FakeEngine()
    server.engine = engine
    _Handler.engine = engine
    # 「自动找」要扫的那串端口必须钉死在这台假引擎上：作者这台机器上真的有一个
    # ComfyUI 在 8188 跑着，不钉住的话测试会连到它 —— 记住的地址会变成 8188，
    # 断言当场对不上，而且测的根本是别人的引擎。
    monkeypatch.setattr(image_gen, "COMFY_PORTS", (server.server_address[1],))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    harness = Harness(tmp_path, server)
    try:
        yield harness
    finally:
        harness.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


class TestProbe:
    """「本机有没有引擎」这句话必须问出来，不能靠猜。"""

    def test_probe_reports_version_device_and_weights(self, hub: Harness) -> None:
        hub.prefs.set(IMAGE_COMFY_URL, hub.url)
        probe = hub.service.probe()
        assert probe["ok"] is True
        assert "0.39.0" in probe["detail"]
        assert probe["checkpoints"] == ["anythingV3_fp16.safetensors"]

    def test_an_empty_slot_remembers_the_port_it_found(self, hub: Harness) -> None:
        """没填地址时扫到就记下来 —— 不然每次都要重扫八个端口。"""
        hub.service.probe()
        assert hub.prefs.text(IMAGE_COMFY_URL) == hub.url

    def test_status_never_touches_the_network(self, hub: Harness) -> None:
        """面板每 1.5 秒读一次状态 —— 那里一个请求都不许发。"""
        hub.use_local()
        hub.service.probe()
        before = hub.requests.calls
        for _ in range(5):
            hub.service.status()
        assert hub.requests.calls == before, f"轮询里多发 {hub.requests.calls - before} 个请求"

    def test_a_dead_port_says_what_to_do_next(self, hub: Harness) -> None:
        hub.dead_address()
        probe = hub.service.probe()
        assert probe["ok"] is False
        assert "127.0.0.1:" in probe["detail"], "那句连不上要点名是哪个地址"
        assert "要么" in probe["detail"], "光说连不上不算话，得给出下一步"

    def test_probing_does_not_start_a_process_on_its_own(self, hub: Harness) -> None:
        """没指目录 = 绝不替你起引擎。一张 4 GB 的卡上多起一个会崩。"""
        hub.dead_address()
        hub.service.probe()
        assert hub.spawned == []

    def test_a_server_that_answers_but_has_no_weights_is_not_ok(self, hub: Harness) -> None:
        hub.engine.checkpoints = []
        hub.prefs.set(IMAGE_COMFY_URL, hub.url)
        answer = hub.service.generate("权重表是空的")
        assert answer["ok"] is False
        assert "权重" in answer["error"] or "没报出" in answer["error"]


class TestChineseBecomesTags:
    """中文那句"画什么"要变成引擎听得懂的英文标签 —— 这一层是纯函数，不碰引擎。

    钉的不是"翻得漂亮"，是三条会 regress 的形状：**没有中文就一个字都不许动**
    （对话里模型已经扩写过的英文再被这层改一次就是灾难）、颜色要贴着主体、
    认不出的词要**说出来**而不是静默丢掉。
    """

    def test_an_english_prompt_is_not_touched(self) -> None:
        from jarvis.app.image_prompt import expand

        said = "green goblin, green skin, dark forest, moonlight"
        assert expand(said).prompt == said, "模型扩写过的英文再被改一次，就是外行指导内行"

    def test_the_color_sits_in_front_of_the_subject(self) -> None:
        from jarvis.app.image_prompt import expand

        answer = expand("画一个绿色瘦弱的哥布林")
        # 最后那三个词是量出来的：属性带着主体再说一遍，中心绿 0.52 / 外圈 0.17；
        # 只写 "thin" 的那一组是 0.05~0.16，和不带主体的没有区别。
        assert answer.prompt == "green goblin, green skin, thin goblin"
        assert answer.unknown == ()

    def test_words_the_table_does_not_know_are_said_out_loud(self) -> None:
        from jarvis.app.image_prompt import expand

        answer = expand("画一只喷火的麒麟兽")
        assert "qilin" in answer.prompt
        assert answer.unknown, "认不出来却不说 = 用户以为引擎听见了"
        assert "没翻出来" in answer.note

    def test_a_sentence_nothing_matches_gets_no_prompt_at_all(self) -> None:
        """一个词都认不出来时回**空串**，不是回原话。

        r112 之前这里回的是原话，于是一串汉字原样进了 ``CLIPTextEncode``。
        那不是"原样退回"，是"把噪声当提示词送出去"——从 PNG 里读出来的现场是
        ``pink pig, …, 头 人 起 舞 头 饰 随 舞 动 …``，画出来一团粉色色块。
        空串让服务层能拦住它并说实话；回原话只会让它报成功。
        """
        from jarvis.app.image_prompt import expand

        assert expand("这张不画").prompt == "", "一个词都没认出来时不许把汉字当提示词送出去"

    def test_measure_words_do_not_become_part_of_the_picture(self) -> None:
        from jarvis.app.image_prompt import expand

        assert "画" not in expand("帮我画一张壁纸").prompt
        assert "wallpaper" in expand("帮我画一张壁纸").prompt


class TestLocalGenerate:
    """工作流发出去的那张图，和拿回来的那个文件。"""

    def test_the_workflow_that_leaves_is_the_one_we_built(self, hub: Harness) -> None:
        hub.use_local()
        answer = hub.service.generate("一只橘猫", size="512x512", steps=18)
        assert answer["ok"] is True, answer["error"]
        got = hub.engine.submitted
        assert got["checkpoint"] == "anythingV3_fp16.safetensors"
        # SD1.5 的编码器只认英文标签：中文原话送进去等于没说话（r105 实拍那个动漫美女）。
        assert got["positive"] == "orange cat, orange skin"
        assert answer["prompt_input"] == "一只橘猫", "原话必须留着，面板要说清引擎听见了什么"
        assert got["latent"] == {"width": 512, "height": 512, "batch_size": 1}
        assert got["steps"] == 18
        assert got["node_count"] == 7, "节点少了就是工作流拼错，不是引擎的锅"
        assert isinstance(got["seed"], int) and got["seed"] > 0

    def test_the_png_lands_in_the_named_directory(self, hub: Harness) -> None:
        hub.use_local()
        answer = hub.service.generate("一朵玫瑰")
        path = Path(answer["path"])
        assert path.parent == hub.paths.images_dir
        assert path.read_bytes() == PNG
        assert answer["preview"].startswith("data:image/")
        assert answer["engine"] == "local"

    def test_the_seed_and_seconds_come_back_for_a_reason(self, hub: Harness) -> None:
        """回话里的数必须是引擎真用过的 —— "就刚才那张再来一遍"要能对上。"""
        hub.use_local()
        answer = hub.service.generate("同一只猫再来一次")
        assert answer["seed"] == hub.engine.submitted["seed"]
        assert answer["seconds"] >= 0

    def test_a_rejected_prompt_says_so_with_the_address(self, hub: Harness) -> None:
        hub.use_local()
        hub.engine.prompt_status = 500
        answer = hub.service.generate("这只猫它不画")
        assert answer["ok"] is False
        assert hub.url in answer["error"]
        assert answer["path"] == ""

    def test_finished_without_an_image_is_not_called_success(self, hub: Harness) -> None:
        hub.use_local()
        hub.engine.history_outputs = False
        answer = hub.service.generate("没出图的猫")
        assert answer["ok"] is False
        assert "没出图" in answer["error"]

    def test_a_missing_image_file_is_not_called_success(self, hub: Harness) -> None:
        hub.use_local()
        hub.engine.view_status = 404
        answer = hub.service.generate("历史有、文件没了")
        assert answer["ok"] is False and "404" in answer["error"]

    def test_an_empty_prompt_never_reaches_the_engine(self, hub: Harness) -> None:
        hub.use_local()
        answer = hub.service.generate("   ")
        assert answer["ok"] is False and hub.engine.submitted == {}

    def test_keep_turned_off_removes_the_file_but_keeps_the_preview(self, hub: Harness) -> None:
        hub.use_local()
        hub.prefs.set(IMAGE_KEEP_OUTPUT, False)
        answer = hub.service.generate("不留文件的猫")
        assert answer["ok"] is True
        assert answer["preview"].startswith("data:image/")
        assert not Path(answer["path"]).exists()

    def test_remembered_size_and_steps_are_used(self, hub: Harness) -> None:
        hub.use_local()
        assert hub.service.set_size("768x768") == ""
        assert hub.service.set_steps(30) == ""
        answer = hub.service.generate("按记着的档位画一只猫")
        assert answer["size"] == "768x768"
        assert hub.engine.submitted["steps"] == 30

    def test_the_status_snapshot_carries_the_last_run(self, hub: Harness) -> None:
        hub.use_local()
        hub.service.generate("记一下这只猫")
        status = hub.service.status()
        assert status["last_path"] and status["last_seconds"] >= 0
        assert status["error"] == ""


class TestStartEngine:
    """只有用户明确指了目录，软件才替他起一个引擎。"""

    def test_an_address_that_answers_means_no_new_process(
        self, hub: Harness, tmp_path: Path
    ) -> None:
        """已经在跑的引擎要被**用**，不是再复制一个：那张卡的显存只有一份。"""
        hub.use_local()
        hub.service.set_comfy_dir(str(hub.fake_comfyui_on_disk(tmp_path)))
        hub.service.probe()
        assert hub.service.status()["can_start_local"] is True, "目录是有的"
        assert hub.spawned == [], "地址上有人答话，就不许再起第二个引擎抢显存"

    def test_a_named_directory_is_stored_only_when_it_looks_like_comfyui(
        self, hub: Harness, tmp_path: Path
    ) -> None:
        root = hub.fake_comfyui_on_disk(tmp_path)
        assert hub.service.set_comfy_dir(str(root)) == ""
        assert hub.service.status()["can_start_local"] is True
        bad = hub.service.set_comfy_dir(str(tmp_path / "不存在"))
        assert "main.py" in bad

    def test_a_dead_engine_process_is_reported_as_exited(
        self, hub: Harness, tmp_path: Path
    ) -> None:
        hub.dead_address()
        hub.service.set_comfy_dir(str(hub.fake_comfyui_on_disk(tmp_path)))
        hub.proc.exit_code = 3
        answer = hub.service.generate("起不来的那只猫")
        assert answer["ok"] is False
        assert hub.spawned, "指了目录就该真去起一次"
        argv = hub.spawned[0][0]
        assert argv[0].endswith("python.exe") and "main.py" in argv and "--port" in argv

    def test_no_directory_means_no_process_is_forked(self, hub: Harness) -> None:
        hub.dead_address()
        answer = hub.service.generate("没指目录的猫")
        assert answer["ok"] is False and hub.spawned == []
        assert "要么" in answer["error"], "被拒时必须给出下一步，不是只剩一句连不上"

    def test_stopping_takes_the_engine_we_started_when_it_is_still_alive(
        self, hub: Harness, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """起起来的进程在退出时必须被收走；已经自己退了的就不去 terminate（那是真 no-op）。"""
        monkeypatch.setattr(image_gen, "ENGINE_START_TIMEOUT_SECONDS", 0.5)
        hub.dead_address()
        hub.service.set_comfy_dir(str(hub.fake_comfyui_on_disk(tmp_path)))
        hub.proc.exit_code = None  # 活着，但永远不答话
        answer = hub.service.generate("起活了但不理人的猫")
        assert answer["ok"] is False
        assert hub.spawned, "指了目录就该真去起一次"
        hub.service.stop()
        assert hub.proc.terminated is True


class TestCloud:
    """云端那半：请求真发出去，回包真解析。"""

    def test_the_key_travels_in_a_header_and_nowhere_else(self, hub: Harness) -> None:
        hub.use_cloud()
        answer = hub.service.generate("云端的那只猫")
        assert answer["ok"] is True, answer["error"]
        assert hub.engine.cloud_headers[-1]["authorization"] == "Bearer sk-test-key"
        assert "sk-test" not in json.dumps(hub.engine.cloud_bodies[-1]), "钥匙不许混进请求体"
        assert Path(answer["path"]).read_bytes() == PNG
        assert answer["engine"] == "cloud"

    def test_a_url_instead_of_bytes_gets_fetched(self, hub: Harness) -> None:
        hub.use_cloud()
        hub.engine.cloud_mode = "url"
        answer = hub.service.generate("只给链接的那只猫")
        assert answer["ok"] is True, answer["error"]
        assert Path(answer["path"]).read_bytes() == PNG

    def test_a_401_names_the_variable_to_fill(self, hub: Harness) -> None:
        hub.use_cloud()
        hub.engine.cloud_status = 401
        answer = hub.service.generate("没钱的云端画只猫")
        assert IMAGE_KEY_ENV in answer["error"], "要说出该填哪个环境变量，不是丢一句「鉴权失败」"

    def test_an_empty_data_array_is_not_reported_as_a_picture(self, hub: Harness) -> None:
        hub.use_cloud()
        hub.engine.cloud_mode = "empty"
        answer = hub.service.generate("这只猫被过滤了")
        assert answer["ok"] is False and answer["path"] == ""

    def test_no_base_url_means_not_one_request_leaves(self, hub: Harness) -> None:
        hub.prefs.set(IMAGE_ENGINE, "cloud")
        answer = hub.service.generate("没配地址的猫")
        assert answer["ok"] is False
        assert hub.engine.cloud_bodies == []
        assert "服务地址" in answer["error"]

    def test_a_configured_base_without_a_key_names_the_variable_to_fill(self, hub: Harness) -> None:
        """地址填了、钥匙没填：那句话必须点名变量，不能说"差服务地址"。"""
        hub.prefs.set(IMAGE_ENGINE, "cloud")
        hub.prefs.set(IMAGE_CLOUD_BASE, hub.url + "/v1")
        hub.service._environ = {}
        answer = hub.service.generate("只有地址没有钥匙的猫")
        assert answer["ok"] is False
        assert IMAGE_KEY_ENV in answer["error"], answer["error"]
        assert hub.engine.cloud_bodies == []

    def test_a_pasted_full_endpoint_is_trimmed_not_doubled(self, hub: Harness) -> None:
        assert hub.service.set_cloud(hub.url + "/v1/images/generations", "model-x") == ""
        assert hub.prefs.text(IMAGE_CLOUD_BASE) == hub.url + "/v1"

    def test_the_size_sent_to_the_cloud_is_a_supported_one(self, hub: Harness) -> None:
        hub.use_cloud()
        hub.service.set_size("768x768")
        hub.service.generate("云端按档位画猫")
        assert hub.engine.cloud_bodies[-1]["size"] == "768x768"

    def test_a_key_with_chinese_in_it_is_refused_not_crashed(self, hub: Harness) -> None:
        """钥匙里粘进中文：要给一句能照着做的话，不是把 UnicodeEncodeError 抛到界面上。"""
        hub.use_cloud()
        hub.service._environ = cast(Any, {IMAGE_KEY_ENV: "sk-我的钥匙"})
        answer = hub.service.generate("中文钥匙配只猫")
        assert answer["ok"] is False
        assert "ASCII" in answer["error"]
        assert hub.engine.cloud_bodies == [], "这种钥匙根本不该发出去"


class TestAutoOrder:
    """「自动」那一档必须说清它为什么退到云端。"""

    def test_local_wins_when_both_are_there(self, hub: Harness) -> None:
        hub.prefs.set(IMAGE_ENGINE, "auto")
        hub.prefs.set(IMAGE_COMFY_URL, hub.url)
        hub.prefs.set(IMAGE_CLOUD_BASE, hub.url + "/v1")
        assert hub.service.generate("自动这一档的猫")["engine"] == "local"

    def test_a_broken_local_falls_back_to_cloud(self, hub: Harness) -> None:
        hub.prefs.set(IMAGE_ENGINE, "auto")
        hub.prefs.set(IMAGE_COMFY_URL, "http://127.0.0.1:1")
        hub.prefs.set(IMAGE_CLOUD_BASE, hub.url + "/v1")
        answer = hub.service.generate("本机没了云端顶上画猫")
        assert answer["ok"] is True and answer["engine"] == "cloud"

    def test_local_only_never_leaks_to_the_cloud(self, hub: Harness) -> None:
        """钉死 local 的时候，云端配得好也不许偷偷发出去 —— 那是一台机器的隐私边界。"""
        hub.prefs.set(IMAGE_ENGINE, "local")
        hub.prefs.set(IMAGE_COMFY_URL, "http://127.0.0.1:1")
        hub.prefs.set(IMAGE_CLOUD_BASE, hub.url + "/v1")
        answer = hub.service.generate("只许本地的猫")
        assert answer["ok"] is False
        assert hub.engine.cloud_bodies == []
        assert "本地没走通" not in answer["error"], "local 档不该去检查云端有没有配好"
        assert "只用本机" in answer["error"] or "ComfyUI" in answer["error"], answer["error"]

    def test_two_holes_are_explained_in_one_sentence(self, hub: Harness) -> None:
        hub.prefs.set(IMAGE_ENGINE, "auto")
        hub.prefs.set(IMAGE_COMFY_URL, "http://127.0.0.1:1")
        answer = hub.service.generate("两边都没有的猫")
        assert answer["ok"] is False
        assert "本地没走通" in answer["error"] and "云端" in answer["error"]


class TestTheUsersOwnWordsReachTheEngine:
    """用户原话要**顺着工具递到引擎前面那一层**，不然词典那张表白调。

    r110 给 `expand()` 写了一整个类的判据，全绿，图还是错的 —— 因为对话那条路
    从来不把用户原话交给 `expand()`：模型先翻成英文，进引擎的是模型那串。
    r112 从 PNG 里读出来的现场，模型把「猪头人」翻成了 `pink pig`（一头粉猪）。
    这一类判据钉的就是那条管子：**原话进得来，词典的主体压得住模型的猜测。**
    """

    def _handlers(self, service: ImageService) -> dict[str, Any]:
        return {spec.name: handler for spec, handler in image_tools.build(service)}

    PIG = "生成一张在月光下翩翩起舞的猪头人"
    MODEL_WROTE = "pink pig, pink skin, glowing pig, fiery pig, stars, moonlight, dreamlike"

    def test_the_table_beats_the_models_guess_at_the_subject(self, hub: Harness) -> None:
        hub.use_local()
        words = self._handlers(hub.service)["generate_image"](
            {"prompt": self.MODEL_WROTE, "zh": self.PIG}
        )
        assert "画好了" in words, words
        positive = hub.engine.submitted["positive"]
        assert "pig head" in positive and "humanoid" in positive, positive
        assert "pink pig" not in positive, "模型那四个「猪」的说法会把猪头人压成一头猪"
        assert "fiery pig" not in positive, positive
        assert "dancing" in positive, "模型把「翩翩起舞」整句丢了，词典要把它补回来"

    def test_the_scene_the_model_added_is_not_thrown_away(self, hub: Harness) -> None:
        """只换抢主体的那几条，剩下的（星空、梦幻）一条不动。"""
        hub.use_local()
        self._handlers(hub.service)["generate_image"]({"prompt": self.MODEL_WROTE, "zh": self.PIG})
        positive = hub.engine.submitted["positive"]
        assert "stars" in positive and "dreamlike" in positive, positive

    def test_the_panel_sees_what_the_user_said_not_what_the_model_wrote(self, hub: Harness) -> None:
        hub.use_local()
        answer = hub.service.generate(self.MODEL_WROTE, zh=self.PIG)
        assert answer["prompt_input"] == self.PIG, "屏上要显示用户自己那句，不是模型的译文"

    def test_a_model_that_forgets_zh_still_gets_its_picture(self, hub: Harness) -> None:
        """`zh` 是选填的：模型漏了只是用不上词典，不许整个生成失败。

        把它设成必填试过一次 —— 工具框架当场回「缺少必填参数 zh」，
        模型漏一次用户就一张图都拿不到。那是拿可用性换纯洁。
        """
        hub.use_local()
        words = self._handlers(hub.service)["generate_image"]({"prompt": self.MODEL_WROTE})
        assert "画好了" in words, words
        assert "pink pig" in hub.engine.submitted["positive"], "没有原话时就该照模型写的画"

    def test_an_unknown_sentence_is_said_out_loud_not_drawn_as_noise(self, hub: Harness) -> None:
        """一个词都没认出来时不许把汉字交给引擎 —— 那是用户看到的那团色块。"""
        hub.use_local()
        answer = hub.service.generate("这张不画")
        assert answer["ok"] is False
        assert hub.engine.submitted == {}, "汉字一个都不许到引擎那边去"
        assert "没翻成英文标签" in answer["error"], answer["error"]


class TestTheToolLayer:
    def _handlers(self, service: ImageService) -> dict[str, Any]:
        return {spec.name: handler for spec, handler in image_tools.build(service)}

    def test_the_answer_carries_the_path_and_the_numbers(self, hub: Harness) -> None:
        hub.use_local()
        words = self._handlers(hub.service)["generate_image"]({"prompt": "一只橘猫"})
        assert "画好了" in words
        assert hub.engine.submitted["positive"] == "orange cat, orange skin"
        assert hub.service.status()["last_path"] in words, "回话里没给路径，用户不知道图去哪了"
        for needle in ("512x512", "本地 ComfyUI", "秒", "种子"):
            assert needle in words, f"回话缺了 {needle}：用户下一句就要问它"

    def test_a_refusal_is_passed_through_in_the_engine_s_own_words(self, hub: Harness) -> None:
        hub.use_local()
        hub.engine.prompt_status = 500
        words = self._handlers(hub.service)["generate_image"]({"prompt": "这只猫它不画"})
        assert "没画成" in words and hub.url in words

    def test_an_empty_prompt_is_refused_before_the_engine(self, hub: Harness) -> None:
        hub.use_local()
        words = self._handlers(hub.service)["generate_image"]({"prompt": "  "})
        assert "不能空着" in words and hub.engine.submitted == {}

    def test_no_engine_means_no_tools_advertised(self, hub: Harness) -> None:
        hub.prefs.set(IMAGE_ENGINE, "cloud")
        hub.prefs.set(IMAGE_CLOUD_BASE, "")
        assert image_tools.build(hub.service) == []

    def test_the_status_tool_answers_in_sentences(self, hub: Harness) -> None:
        hub.use_local()
        text = self._handlers(hub.service)["image_status"]({})
        assert "本地引擎" in text and "云端" in text and "尺寸" in text


def _tools_section() -> ToolsSection:
    return ToolsSection.from_mapping(
        {
            "enabled": True,
            "confirm_dangerous": True,
            "allow_write": False,
            "allow_shell": False,
            "file_roots": [],
            "max_result_chars": 8000,
        }
    )


class TestThroughTheChatDoor:
    """手册里挂了几轮的那条：「没有真让某个模型调一次」。

    这里只假在模型那一下，其余全是真的 —— ``ChatService`` 走真分发、注册表真校验参数、
    ``ImageService`` 真对一个开在端口上的假 ComfyUI 提交工作流、PNG 真落到盘上。
    判据是 ``reply.tools_used`` 里有 ``generate_image``，以及回话里那个路径**确实在盘上**。
    """

    def _service(self, hub: Harness) -> ToolService:
        service = ToolService(
            _tools_section,
            monitor_factory=lambda: SystemMonitor(top_processes=0),
            cleaner_factory=lambda: DiskCleaner(hub.paths.audit_dir / "deletions.jsonl"),
            images=hub.service,
        )
        service.start()
        return service

    def test_one_sentence_becomes_a_file_on_disk(self, hub: Harness) -> None:
        hub.use_local()
        service = self._service(hub)
        try:
            assert "generate_image" in [
                spec.name for spec in service.specs()
            ], "引擎在跑，工具却没上注册表"
            chat = ChatService(
                lambda: _AsksForAPicture(), tool_provider=lambda: service, session_id="t"
            )
            chat.start()
            reply = chat.ask("给我画一只在月光下的橘猫")
            assert reply.error == "", reply.error
            assert "generate_image" in reply.tools_used
            path = Path(str(hub.service.status()["last_path"]))
            assert path.is_file(), f"回话说画好了，盘上却没有：{path}"
            assert path.read_bytes()[:4] == b"\x89PNG", "落盘的不是 PNG"
            assert str(path) in reply.answer, "回话里没带路径，用户不知道去哪看图"
        finally:
            service.stop()

    def test_no_engine_means_the_chat_door_has_nothing_to_offer(self, hub: Harness) -> None:
        """引擎不在的时候这一条不能出现在她递给模型的清单里（假能力比没能力坏）。"""
        hub.prefs.set(IMAGE_ENGINE, "cloud")
        hub.prefs.set(IMAGE_CLOUD_BASE, "")
        service = self._service(hub)
        try:
            assert "generate_image" not in [spec.name for spec in service.specs()]
        finally:
            service.stop()


class _AsksForAPicture:
    """第一轮调 ``generate_image``，第二轮把工具给的那句话原样说完。

    第二轮是从 ``messages[-1].content`` 里读结果的 —— 那条断言就是"真接线"的证据：
    参数校验、注册表、引擎任何一环没对上，这里都会拿到一句不像话的回话而红。
    """

    provider_name = "fake"
    model = "fake"

    def __init__(self) -> None:
        self._round = 0

    def complete(
        self,
        messages: Sequence[ChatMessage],
        *,
        options: GenerationOptions | None = None,
    ) -> ChatResponse:
        del options
        self._round += 1
        if self._round == 1:
            return ChatResponse(
                content="",
                model="fake",
                tool_calls=(
                    ToolCall(
                        id="c1",
                        name="generate_image",
                        arguments='{"prompt": "一只在月光下的橘猫"}',
                    ),
                ),
            )
        evidence = messages[-1].content
        if "画好了" not in evidence:
            raise AssertionError(f"工具没把结果交回来：{evidence!r}")
        return ChatResponse(content=evidence, model="fake")

    def stream(
        self,
        messages: Sequence[ChatMessage],
        *,
        options: GenerationOptions | None = None,
    ) -> Any:
        del messages, options
        raise AssertionError("这条测试走的是 complete")


class TestRoutingAndBridge:
    def test_the_image_pack_is_reachable_and_whole(self) -> None:
        assert set(PACKS["image"]) == {"generate_image", "image_status"}
        assert classify_tool_intent("给我画一张橘猫") == ("image",)
        assert classify_tool_intent("帮我生成一张壁纸") == ("image",)
        assert "image" not in classify_tool_intent("把屏幕截图发我"), "截图不归生图管"

    def test_a_missing_service_still_answers_with_a_whole_state(self) -> None:
        from jarvis.ui.desktop import HudBridge

        bridge = HudBridge.__new__(HudBridge)
        bridge._images = None
        empty = bridge.image_state()
        for key in (
            "engines",
            "sizes",
            "cloud_key_set",
            "local",
            "tail",
            "error",
            "steps",
            "vendored_engine",
        ):
            assert key in empty, f"没服务时那一格缺了 {key}：面板会留着上一次的显示"
        refused = bridge.image_generate("一句描述里的猫")
        assert refused["ok"] is False and refused["path"] == "" and refused["error"]
        assert "没有接生图服务" in bridge.image_probe()["detail"]
        assert "没有接生图服务" in bridge.image_tune("auto", "512x512", 20)["detail"]
        assert "没有接生图服务" in bridge.image_setup("a", "b", "c", "d")["detail"]

    def test_the_real_service_is_reachable_through_the_bridge(self, hub: Harness) -> None:
        from jarvis.ui.desktop import HudBridge

        bridge = HudBridge.__new__(HudBridge)
        bridge._images = hub.service
        hub.use_local()
        answer = bridge.image_generate("画一只蓝色的鸟", "512x512")
        assert answer["ok"] is True, answer["error"]
        assert hub.engine.submitted["positive"] == "blue bird, blue skin"
        assert answer["prompt_input"] == "画一只蓝色的鸟"
        state = bridge.image_tune("local", "768x768", 24)
        assert state["size"] == "768x768" and state["steps"] == 24 and state["error"] == ""
        bad = bridge.image_tune("nonsense", "512x512", 20)
        assert "没有「nonsense」" in bad["error"], "被拒的那一格必须在屏上说话"

    def test_tuning_rejects_values_that_are_not_on_the_list(self, hub: Harness) -> None:
        assert "只能是" in hub.service.set_size("3000x3000")
        assert "整数" in hub.service.set_steps("abc")
        assert "之间" in hub.service.set_steps(400)

    def test_the_service_reads_the_key_from_the_environment_only(self, hub: Harness) -> None:
        """引擎这边只认环境变量：偏好文件里不许出现钥匙，快照里也不许带值。"""
        hub.use_cloud()
        status = hub.service.status()
        assert status["cloud_key_set"] is True
        assert IMAGE_KEY_ENV not in json.dumps(hub.prefs.as_dict(), ensure_ascii=False)
        assert "sk-test-key" not in json.dumps(status, ensure_ascii=False)

    def test_a_local_address_does_not_go_through_the_system_proxy(self, hub: Harness) -> None:
        """挂着系统代理时，发往 127.0.0.1 的引擎请求必须照样直达。

        这台机器上真有过一次：urllib 走代理，代理回了一个 **502**，屏幕上就成了
        「ComfyUI 坏了」，而它活得好好的。这里把 `*_PROXY` 指到一个**只会回 502** 的假代理上，
        再走一次服务自己的那条路 —— 请求要是被它接走，测试立刻红。
        """
        hub.use_local()
        proxy = _LyingProxy()
        port = proxy.start()
        before = {key: os.environ.get(key) for key in _PROXY_KEYS}
        for key in _PROXY_KEYS:
            os.environ[key] = f"http://127.0.0.1:{port}"
        try:
            # **不注入 opener**：注入的那个是测试自己造的计数器，用它就等于绕过了要验的东西。
            service = ImageService(hub.prefs, hub.paths, environ={IMAGE_KEY_ENV: "sk-test-key"})
            answer = service.generate("代理挡路也要画的猫")
        finally:
            for key, value in before.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
            service.stop()
        assert answer["ok"] is True, answer["error"]
        assert proxy.hits == 0, f"请求被假代理接走了 {proxy.hits} 次 —— 本机地址必须直达"
        assert hub.engine.submitted["positive"] == "cat"


class _LyingProxy:
    """一个只会回 502 的假代理：请求到了它那里，图就画不出来。"""

    def __init__(self) -> None:
        self.server: ThreadingHTTPServer | None = None
        self.hits = 0

    def start(self) -> int:
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                outer.hits += 1
                self.send_error(502, "lying proxy")

            def do_POST(self) -> None:
                outer.hits += 1
                self.send_error(502, "lying proxy")

            def log_message(self, *args: object) -> None:
                del args

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return int(self.server.server_address[1])


class TestPathsAndWords:
    """落盘的那条路径和引擎说的那句话，都是能被外面改掉的东西。"""

    def test_a_deleted_output_directory_is_made_again(self, hub: Harness) -> None:
        """用户在软件外面把 数据/生图 删了 —— 下一次该把图画进去，不是抛"存不下"。"""
        hub.use_local()
        hub.service.generate("先把目录建出来画猫")
        shutil.rmtree(hub.paths.images_dir)
        answer = hub.service.generate("目录没了也要画猫")
        assert answer["ok"] is True, answer["error"]
        assert Path(answer["path"]).parent == hub.paths.images_dir

    def test_the_rejection_reason_in_the_body_reaches_the_screen(self, hub: Harness) -> None:
        """ComfyUI 拒绝时 body 里写的是人话。扔掉它就只剩 "HTTP 400" 四个字。"""
        hub.use_local()

        class Exploding(FakeEngine):
            def handle(
                self, method: str, path: str, body: bytes, headers: dict[str, str]
            ) -> tuple[int, bytes, str]:
                from urllib.parse import urlparse

                if urlparse(path).path == "/prompt":
                    payload = {
                        "error": {
                            "type": "bad_weights",
                            "message": "这个包没带运行时的图片，去不了这个版本",
                        }
                    }
                    return 400, json.dumps(payload).encode(), "application/json"
                return super().handle(method, path, body, headers)

        hub.server.engine = cast(Any, Exploding())
        _Handler.engine = hub.server.engine
        answer = hub.service.generate("引擎会骂回来的那只猫")
        assert answer["ok"] is False
        assert "这个包没带运行时的图片" in answer["error"], answer["error"]
        assert "{" not in answer["error"], "整包 JSON 被当成话甩给用户了"

    def test_a_filename_from_the_engine_cannot_escape_the_directory(self, hub: Harness) -> None:
        """文件名是引擎回的。它要是回 ../../Windows/x.png，图只能落在 生图/ 里面。"""

        class Sneaky(FakeEngine):
            def handle(
                self, method: str, path: str, body: bytes, headers: dict[str, str]
            ) -> tuple[int, bytes, str]:
                from urllib.parse import urlparse

                code, payload, kind = super().handle(method, path, body, headers)
                if urlparse(path).path.startswith("/history/"):
                    bad = {"filename": "../../outside.png", "subfolder": "", "type": "output"}
                    return (
                        200,
                        json.dumps(
                            {"p1": {"outputs": {"7": {"images": [bad]}}, "status": {}}}
                        ).encode(),
                        "application/json",
                    )
                return code, payload, kind

        hub.use_local()
        hub.server.engine = cast(Any, Sneaky())
        _Handler.engine = hub.server.engine
        answer = hub.service.generate("想跑出去的那只猫")
        assert answer["ok"] is True, answer["error"]
        landed = Path(answer["path"])
        assert landed.parent == hub.paths.images_dir
        assert not (hub.paths.images_dir.parent / "outside.png").exists()


class TestPureHelpers:
    def test_sizes_align_to_eight_and_get_capped(self) -> None:
        assert parse_size("768x768") == (768, 768)
        assert parse_size("500x500") == (496, 496)
        assert parse_size("2000x2000") == (1024, 1024), "4 GB 的卡不许直接画 2000"
        assert parse_size("乱七八糟") == (512, 512)
        assert parse_size("1024×512") == (1024, 512)
        assert parse_size(None) == (512, 512)

    def test_cloud_answer_parses_both_shapes(self) -> None:
        blob, note = parse_cloud_answer({"data": [{"b64_json": base64.b64encode(PNG).decode()}]})
        assert blob == PNG and note == ""
        blob, note = parse_cloud_answer({"data": [{"url": "http://x/y.png"}]})
        assert blob == b"" and note.startswith("NEED_FETCH:")
        blob, note = parse_cloud_answer({"error": {"message": "quota"}})
        assert blob == b"" and "quota" in note
        blob, note = parse_cloud_answer("不是对象")
        assert blob == b"" and "不是 JSON 对象" in note

    def test_the_workflow_is_a_graph_not_a_guess(self) -> None:
        graph = build_workflow("m.safetensors", "p", "n", 512, 512, 20, 5, "pre")

        def inputs(node: str) -> dict[str, Any]:
            raw = graph[node]
            assert isinstance(raw, dict)
            got = raw["inputs"]
            assert isinstance(got, dict)
            return cast(dict[str, Any], got)

        assert inputs("1")["ckpt_name"] == "m.safetensors"
        assert inputs("2")["text"] == "p"
        assert inputs("3")["text"] == "n"
        assert inputs("5")["positive"] == ["2", 0]
        assert inputs("5")["latent_image"] == ["4", 0]
        assert inputs("6")["vae"] == ["1", 2]
        assert inputs("7")["filename_prefix"] == "pre"
