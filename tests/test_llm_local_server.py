"""The bundled llama.cpp server: the decision table, the phase, and who may start it.

Nothing here waits for a real 4.36 GB model. The point of these tests is the *judgement*
-- can she run here, what does the screen say when she cannot, and does the port the
client is handed match the one the child actually bound -- and every one of those is
answerable with a fake exe, a fake ``.gguf`` and a monkeypatched memory reading.
"""

from __future__ import annotations

import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from jarvis.core.exceptions import ConfigurationError
from jarvis.llm import local_server
from jarvis.llm.local_server import LocalServer, ModelPhase
from jarvis.llm.openai_compat import OpenAiCompatClient


@pytest.fixture()
def shelf(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A package that carries both the runtime and the weights."""
    runtime = tmp_path / "vendor" / "llama"
    runtime.mkdir(parents=True)
    (runtime / local_server.SERVER_EXE).write_bytes(b"MZ")
    models = tmp_path / "模型"
    models.mkdir()
    (models / "small.gguf").write_bytes(b"x" * 10)
    (models / "big.gguf").write_bytes(b"x" * 40)
    monkeypatch.setattr(local_server, "runtime_dir", lambda: runtime)

    def select(wanted: str) -> Path | None:
        # 门槛的判定现在走 `selected_model`（`model_file()` 只是拿当前旋钮调它一次），
        # 所以要缝的是这一层 —— 缝 `model_file` 会让预览那条路漏回真仓库的 模型/ 目录。
        pick = models / (wanted or "big.gguf")
        return pick if pick.exists() else None

    monkeypatch.setattr(local_server, "selected_model", select)
    monkeypatch.setattr(local_server, "free_memory_mb", lambda: 9000)
    # 假文件只有 40 字节，按大小估门槛会得到一个没意义的数；给它一张和出厂 7B 一样的档案，
    # 这几个测试才继续在量"门槛不够就不加载"这件事本身。
    monkeypatch.setitem(
        local_server.MODEL_CARDS,
        "big.gguf",
        local_server.ModelCard(
            "big.gguf", "测试模型", "big", "?", 5200, 64, {4096: (5061, 4758)}, False
        ),
    )
    return tmp_path


def test_the_plan_says_yes_when_the_files_and_the_memory_are_there(shelf: Path) -> None:
    verdict = local_server.plan()

    assert verdict.can_run is True
    assert verdict.reason == ""
    assert verdict.model is not None and verdict.model.name == "big.gguf"


def test_a_missing_runtime_names_the_folder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(local_server, "runtime_dir", lambda: None)
    monkeypatch.setattr(local_server, "model_file", lambda: tmp_path / "a.gguf")
    monkeypatch.setattr(local_server, "free_memory_mb", lambda: 9000)

    verdict = local_server.plan()

    assert verdict.can_run is False
    assert "vendor/llama" in verdict.reason


def test_missing_weights_say_where_to_put_them(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(local_server, "runtime_dir", lambda: tmp_path / "vendor" / "llama")
    monkeypatch.setattr(local_server, "selected_model", lambda wanted: None)
    monkeypatch.setattr(local_server, "free_memory_mb", lambda: 9000)

    verdict = local_server.plan()

    assert verdict.can_run is False
    assert local_server.MODEL_DIRNAME in verdict.reason


def test_a_machine_without_room_is_told_the_number(
    shelf: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(local_server, "free_memory_mb", lambda: 2048)

    verdict = local_server.plan()

    assert verdict.can_run is False
    assert "2 GB" in verdict.reason and "5 GB" in verdict.reason


def test_the_endpoint_follows_the_port_the_child_actually_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Before startup the answer is the preferred port; after it, the real one."""
    monkeypatch.setattr(local_server, "shared_server", lambda: LocalServer())

    assert local_server.endpoint() == f"http://127.0.0.1:{local_server.DEFAULT_PORT}/v1"


def test_status_never_claims_running_without_a_server(shelf: Path) -> None:
    server = LocalServer()

    status = server.status()

    assert status["phase"] == ModelPhase.OFF.value
    assert status["port"] == 0
    assert status["can_run"] is True
    assert status["model"] == "big.gguf"


def test_a_refused_start_reports_the_reason_and_stays_off(
    shelf: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(local_server, "free_memory_mb", lambda: 100)
    server = LocalServer()

    ok, reason = server.ensure_running()

    assert ok is False
    assert "内存" in reason
    assert server.phase is ModelPhase.FAILED
    assert server.status()["phase"] == "failed"


def test_a_child_that_dies_becomes_a_sentence_with_its_output(
    shelf: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The stderr tail is the whole difference between "起不来" and a diagnosable fault."""

    class Dead:
        returncode = 1

        def poll(self) -> int:
            return 1

        def terminate(self) -> None:
            pass

        def wait(self, timeout: float | None = None) -> int:
            return 1

        def kill(self) -> None:
            pass

        stderr = iter([b"ggml_vulkan: no device found\n"])
        stdout = iter([b""])

    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: Dead())
    server = LocalServer()

    ok, reason = server.ensure_running(wait_seconds=2)

    assert ok is False
    assert "退出码" in reason or "退出" in reason
    assert "no device found" in reason
    assert server.phase is ModelPhase.FAILED


class _Alive:
    """A child that stays up: poll() says nothing has exited."""

    returncode = None

    def __init__(self) -> None:
        self.terminates = 0

    def poll(self) -> None:
        return None

    def terminate(self) -> None:
        self.terminates += 1

    def wait(self, timeout: float | None = None) -> int:
        return 0

    def kill(self) -> None:
        pass

    stdout = iter([b""])
    stderr = iter([b""])


def test_two_callers_wait_for_the_same_child(shelf: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """并发冷加载时第二个人必须等**同一个**子进程，而不是再 spawn 一个顶掉它。

    被顶掉的那一个从此没人管：几 GB 内存和一个端口一直占到注销，而看门狗只认
    ``self._proc`` 指向的那一个。真机形态是「她卡住了，任务管理器里两个 llama-server」。
    """
    spawned: list[_Alive] = []

    def spawn(*args: Any, **kwargs: Any) -> _Alive:
        child = _Alive()
        spawned.append(child)
        return child

    monkeypatch.setattr(subprocess, "Popen", spawn)

    gate = threading.Event()
    waiting: list[float] = []

    def fake_wait(self: LocalServer, seconds: float) -> tuple[bool, str]:
        waiting.append(seconds)
        gate.wait(5)
        return True, ""

    monkeypatch.setattr(LocalServer, "_wait_ready", fake_wait)

    server = LocalServer()
    first = threading.Thread(target=server.ensure_running)
    first.start()
    while not waiting:
        time.sleep(0.01)
    assert server.phase is ModelPhase.LOADING

    second = threading.Thread(target=server.ensure_running)
    second.start()
    while len(waiting) < 2:
        time.sleep(0.01)

    gate.set()
    first.join(5)
    second.join(5)

    assert len(waiting) == 2, "两个人都在等"
    assert len(spawned) == 1, f"只该起一个子进程，实际起了 {len(spawned)} 个"


def test_a_child_left_alive_by_a_failed_load_is_reaped(
    shelf: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """加载失败但进程没死透：换新的之前先把旧的收掉，否则它跟着这扇窗活一整天。"""
    old = _Alive()
    spawned: list[_Alive] = []

    def spawn(*args: Any, **kwargs: Any) -> _Alive:
        child = _Alive()
        spawned.append(child)
        return child

    monkeypatch.setattr(subprocess, "Popen", spawn)
    monkeypatch.setattr(LocalServer, "_wait_ready", lambda self, seconds: (True, ""))

    server = LocalServer()
    # 假子进程：只要求它"看着还活着"，类型对不上是故意的
    server._proc = old  # type: ignore[assignment]
    server._phase = ModelPhase.FAILED
    server._detail = "上一次没起来"

    ok, _ = server.ensure_running()

    assert ok is True
    assert len(spawned) == 1
    assert old.terminates == 1, "旧进程必须被收掉"


def test_stopping_twice_is_a_no_not_a_crash(shelf: Path) -> None:
    server = LocalServer()

    server.stop()
    server.stop(reason="再来一次")

    assert server.phase is ModelPhase.OFF


def _section() -> Any:
    from jarvis.config.schema import LlmSection

    return LlmSection.from_mapping(
        {
            "default_provider": "local",
            "timeout_seconds": 60,
            "max_retries": 0,
            "retry_backoff_seconds": 0.1,
            "providers": {
                "local": {
                    "base_url": "http://127.0.0.1:9/v1",
                    "key_optional": True,
                    "models": [{"id": "qwen2.5-7b"}],
                }
            },
        }
    )  # from_mapping is the hand-rolled parser the config layer actually uses


def test_the_chat_path_refuses_when_the_model_cannot_run(
    shelf: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``client_for('local')`` is the one join every request path uses.

    Asserted through the real service and the real section rather than a stand-in dict,
    because a fake that skipped ``ensure_running`` would stay green while she was broken.
    """
    from jarvis.llm.service import LlmService

    monkeypatch.setattr(local_server, "free_memory_mb", lambda: 100)
    service = LlmService(_section)
    service.start()

    with pytest.raises(ConfigurationError) as caught:
        service.client_for("local")

    assert "内存" in str(caught.value)


def test_a_running_server_replaces_the_yaml_port_with_the_live_one(
    shelf: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jarvis.llm.service import LlmService

    async_free = LocalServer()
    monkeypatch.setattr(local_server, "shared_server", lambda: async_free)
    monkeypatch.setattr(async_free, "ensure_running", lambda wait_seconds=0: (True, ""))
    monkeypatch.setattr(local_server, "endpoint", lambda: "http://127.0.0.1:29111/v1")
    service = LlmService(_section)
    service.start()

    client = service.client_for("local")

    # ``_settings`` is private, but this is the assertion that matters: a client built
    # for the yaml port after the child bound another one would look like the model died.
    assert isinstance(client, OpenAiCompatClient)
    assert client._settings.base_url == "http://127.0.0.1:29111/v1"


def test_weights_beside_the_exe_are_found_even_when_the_data_root_moved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    r"""The shipped ``模型`` folder sits next to the executable, not next to the data root.

    A machine that still has ``%LOCALAPPDATA%\Jarvis`` from an older build keeps its data
    there, and the data root's parent is then nowhere near the weights -- that combination
    once made the settings row say 没找到权重 beside a file that was in the package.
    """
    install = tmp_path / "小夜"
    weights = install / local_server.MODEL_DIRNAME
    weights.mkdir(parents=True)
    (weights / "Qwen.gguf").write_bytes(b"y" * 30)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(install / "小夜.exe"))
    monkeypatch.setattr(local_server, "_package_root", lambda: tmp_path / "没打包的源码树")
    monkeypatch.delenv("JARVIS_MODEL_DIR", raising=False)
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "别处" / "数据"))

    found = local_server.model_file()

    assert found is not None
    assert found.name == "Qwen.gguf"
    assert found.parent.parent.name == install.name


def test_from_source_the_repo_root_still_counts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Running from a checkout: ``<repo>/模型`` is the drop-off point, frozen or not."""
    monkeypatch.delattr(sys, "frozen", raising=False)
    monkeypatch.delenv("JARVIS_MODEL_DIR", raising=False)
    monkeypatch.setattr(local_server, "_package_root", lambda: tmp_path / "jarvis")
    weights = tmp_path / local_server.MODEL_DIRNAME
    weights.mkdir()
    (weights / "dev.gguf").write_bytes(b"z" * 12)

    found = local_server.model_file()

    assert found is not None and found.name == "dev.gguf"


def test_a_running_server_is_never_reported_as_unrunnable(
    shelf: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``plan()`` asks "would a cold start fit"; the child's own 5 GB is not a reason to say no.

    The settings row once said 在跑 with 内存只剩 2 GB under it, because the running model is
    exactly what made the free-memory reading drop.
    """
    server = LocalServer()
    server._phase = ModelPhase.RUNNING
    monkeypatch.setattr(local_server, "free_memory_mb", lambda: 2048)

    state = server.status()

    assert state["can_run"] is True
    assert state["reason"] == ""
    assert state["free_mb"] == 2048, "数字还在，只是不再拿它当「跑不了」"


def test_a_loading_server_is_not_called_unrunnable_mid_load(
    shelf: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    server = LocalServer()
    server._phase = ModelPhase.LOADING
    monkeypatch.setattr(local_server, "free_memory_mb", lambda: 2048)

    state = server.status()

    assert state["can_run"] is True and state["reason"] == ""


def test_an_idle_server_still_tells_the_truth_about_memory(
    shelf: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    server = LocalServer()
    monkeypatch.setattr(local_server, "free_memory_mb", lambda: 2048)

    state = server.status()

    assert state["phase"] == ModelPhase.OFF.value
    assert state["can_run"] is False
    assert "内存" in str(state["reason"])


@pytest.fixture()
def dial(monkeypatch: pytest.MonkeyPatch) -> Any:
    """A tunable dial that cannot leak into the next test."""
    from jarvis.llm.local_server import Tuning

    def set_tuning(**changes: Any) -> Any:
        current = local_server.tuning()
        merged = {**current.__dict__, **changes}
        value = Tuning(**merged)
        monkeypatch.setattr(local_server, "_TUNING", value)
        return value

    return set_tuning


def test_a_bigger_window_asks_for_more_free_memory(shelf: Path, dial: Any) -> None:
    """The gate is per tier: measured CPU-only, 2048 costs 4639 MB and 16384 costs 5476 MB."""
    small = dial(context=2048)
    big = dial(context=16384)

    assert big.needed_free_mb > small.needed_free_mb
    assert local_server.plan().needed_mb == big.needed_free_mb


def test_the_gate_scales_with_the_window_it_was_given(
    shelf: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """5.1 GB free passes the 2048 tier and refuses the 16384 one -- it is not one number."""
    from jarvis.llm.local_server import Tuning

    monkeypatch.setattr(local_server, "free_memory_mb", lambda: 5100)
    monkeypatch.setattr(local_server, "_TUNING", Tuning(context=2048))
    assert local_server.plan().can_run is True
    monkeypatch.setattr(local_server, "_TUNING", Tuning(context=16384))
    verdict = local_server.plan()
    assert verdict.can_run is False
    assert "16384" not in verdict.reason  # the sentence talks in GB, not raw token counts
    assert "5 GB" in verdict.reason


def test_eager_loads_anyway_and_says_so_on_the_screen(
    shelf: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The opt-in exists for machines with more RAM than the developer's; refusing there
    would be the gate overruling an operator who measured their own box."""
    from jarvis.llm.local_server import Tuning

    monkeypatch.setattr(local_server, "free_memory_mb", lambda: 2048)
    monkeypatch.setattr(local_server, "_TUNING", Tuning(context=8192, eager=True))

    verdict = local_server.plan()

    assert verdict.can_run is True
    assert local_server.shared_server().status()["eager"] is True


def test_the_window_reaches_the_command_line(shelf: Path, dial: Any) -> None:
    """A dial that only changes what the screen says would be the worst kind of setting."""
    dial(context=8192)
    verdict = local_server.plan()
    assert verdict.exe is not None and verdict.model is not None

    command = local_server.build_command(verdict, 28181)

    assert command[command.index("-c") + 1] == "8192"
    assert "-ngl" not in command, "写死 -ngl 会让 llama.cpp 的 --fit 直接退出"


def test_zero_idle_minutes_cancels_the_reaper(shelf: Path, dial: Any) -> None:
    server = LocalServer()
    server._proc = object()  # type: ignore[assignment] # only the None check matters here
    server._last_used = time.monotonic()
    dial(idle_seconds=0.0)

    server.rearm_idle()

    assert server._idle_timer is None


def test_a_shorter_idle_wait_counts_the_silence_that_already_passed(shelf: Path, dial: Any) -> None:
    """Rearming must not hand the model another full interval for a conversation that ended."""
    server = LocalServer()
    server._proc = object()  # type: ignore[assignment]
    server._last_used = time.monotonic() - 500.0
    dial(idle_seconds=600.0)

    server.rearm_idle()
    armed = server._idle_timer
    assert armed is not None
    assert armed.interval < 600.0 - 480.0

    dial(idle_seconds=300.0)
    server.rearm_idle()
    assert server._idle_timer is not None
    assert server._idle_timer.interval <= 1.5, "已经闲过新档位了：该马上收尾，而不是再等一轮"


def test_a_request_in_flight_is_never_idle(dial: Any) -> None:
    """答一句答得比空闲窗口久，不能被自己的看门狗掐死。

    用户机器上的真实顺序：19:47:12 加载完并当场发出请求 → 19:49:12「空闲 2 分钟，已释放内存」
    → 19:49:13 屏幕上只剩 WinError 10054。倒计时到点是"没人问话"的意思，不是"没人答话"。
    """
    server = LocalServer()
    dial(idle_seconds=2.0)
    stopped: list[str] = []
    server.stop = lambda *, reason="": stopped.append(reason)  # type: ignore[method-assign]

    try:
        server.begin_request()
        server._last_used = time.monotonic() - 999.0  # 单看时间早就"闲过头"了
        server._stop_if_idle()
        assert stopped == [], "这一发还在飞，看门狗不许收人"

        server.end_request()
        server._last_used = time.monotonic() - 999.0
        server._stop_if_idle()
        assert len(stopped) == 1, "答完了才轮到收尾"
        assert "空闲" in stopped[0]
    finally:
        timer = server._idle_timer
        if timer is not None:
            timer.cancel()
