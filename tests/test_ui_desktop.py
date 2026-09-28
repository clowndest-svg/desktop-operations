"""Tests for the desktop shell's Python surface: what the page can actually call.

``run()`` needs a real WebView2 window and so stays out of here. Everything the
page touches *through* it is testable: the bridge methods, the state snapshot the
page pulls on mount, and the script the pump pushes into the browser.
"""

from __future__ import annotations

import json
import threading
import time
from typing import Any

from jarvis.app.chat_service import ChatReply, ChatService
from jarvis.app.disk_service import DiskService
from jarvis.app.system_service import SystemService
from jarvis.app.voice_service import VoiceService
from jarvis.core.events import PipelineEvent, VoicePhase
from jarvis.tools.disk_cleaner import DiskCleaner
from jarvis.tools.monitor import SystemMonitor
from jarvis.ui.desktop import HudBridge, _voice_status_dict, push_state
from jarvis.ui.state_bridge import StateBridge, UiState, UiVoiceState
from tests._fakes import FakeLlmClient


class FakeLoop:
    def __init__(self) -> None:
        self.starts = 0
        self.stops = 0

    @property
    def listening(self) -> bool:
        return self.starts > self.stops

    def start(self) -> None:
        self.starts += 1

    def stop(self) -> None:
        self.stops += 1


class FakeWindow:
    """Records the scripts a push would have run in the browser."""

    def __init__(self) -> None:
        self.scripts: list[str] = []

    def evaluate_js(self, script: str) -> Any:
        self.scripts.append(script)
        return None


class _NoTelemetry:
    """A monitor factory that must never be called by these tests."""

    def __call__(self) -> SystemMonitor:
        raise AssertionError("telemetry is not part of the bridge surface under test")


def _bridge(
    tmp_path: Any,
    *,
    voice: VoiceService | None = None,
    chat: ChatService | None = None,
    state: StateBridge | None = None,
) -> HudBridge:
    system = SystemService(_NoTelemetry())
    disk = DiskService(lambda: DiskCleaner(audit_log=tmp_path / "a.jsonl", sources={}))
    return HudBridge(system, disk, voice=voice, chat=chat, state=state)


def _wait_running(voice: VoiceService, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if voice.status.phase is VoicePhase.RUNNING:
            return True
        time.sleep(0.005)
    return voice.status.phase is VoicePhase.RUNNING


class TestVoiceSurface:
    def test_a_process_without_voice_says_so_rather_than_lying(self, tmp_path: Any) -> None:
        bridge = _bridge(tmp_path)

        status = bridge.voice_status()

        assert status["phase"] == "off"
        assert status["detail"]

    def test_enable_returns_before_the_models_have_loaded(self, tmp_path: Any) -> None:
        """The bridge call runs on pywebview's JS-API thread; loading takes ~25s."""
        started = threading.Event()

        def builder(_on_event: Any) -> FakeLoop:
            started.set()
            time.sleep(0.4)
            return FakeLoop()

        voice = VoiceService(builder, permission=lambda: True, keywords=lambda: ())
        bridge = _bridge(tmp_path, voice=voice)
        clock = time.monotonic()

        status = bridge.voice_enable()
        elapsed = time.monotonic() - clock

        assert status["phase"] == "loading"
        assert elapsed < 0.2, f"the bridge blocked for {elapsed:.2f}s on the UI thread"
        assert started.wait(2.0), "the boot thread never started loading"
        voice.stop()

    def test_mute_releases_the_microphone_and_reports_it(self, tmp_path: Any) -> None:
        loop = FakeLoop()
        voice = VoiceService(lambda _on_event: loop, permission=lambda: True)
        bridge = _bridge(tmp_path, voice=voice)
        voice.enable()
        assert _wait_running(voice)

        status = bridge.voice_mute()

        assert status["phase"] == "muted"
        assert loop.stops == 1

    def test_the_keyword_reaches_the_page(self, tmp_path: Any) -> None:
        voice = VoiceService(
            lambda _on_event: FakeLoop(),
            permission=lambda: True,
            keywords=lambda: ("你好小夜",),
        )
        bridge = _bridge(tmp_path, voice=voice)

        # The word is configuration, not run-time state: the page can print "说
        # 「你好小夜」唤醒我" before a single model has loaded.
        assert bridge.voice_status()["keyword"] == "你好小夜"
        voice.enable()
        assert _wait_running(voice)
        assert bridge.voice_status()["keyword"] == "你好小夜"

    def test_voice_status_dict_survives_an_unexpected_object(self) -> None:
        assert _voice_status_dict(object()) == {"phase": "off", "detail": "", "keyword": ""}


class TestChatSurface:
    def test_a_typed_question_is_answered(self, tmp_path: Any) -> None:
        client = FakeLlmClient("今天是星期六")
        chat = ChatService(lambda: client)
        chat.start()
        bridge = _bridge(tmp_path, chat=chat)

        payload = bridge.chat_ask("今天星期几")

        assert payload["answer"] == "今天是星期六"
        assert payload["error"] == ""

    def test_a_process_without_chat_reports_the_gap(self, tmp_path: Any) -> None:
        bridge = _bridge(tmp_path)

        payload = bridge.chat_ask("你好")

        assert payload["answer"] == ""
        assert payload["error"] == "对话服务未启用"

    def test_a_model_failure_surfaces_as_text_not_an_exception(self, tmp_path: Any) -> None:
        def boom() -> FakeLlmClient:
            raise RuntimeError("没有配置 API Key")

        chat = ChatService(boom)
        chat.start()
        bridge = _bridge(tmp_path, chat=chat)

        assert "没有配置 API Key" in str(bridge.chat_ask("你好")["error"])

    def test_reply_shape_is_exactly_what_the_page_renders(self) -> None:
        assert ChatReply("q", "a").to_dict() == {"question": "q", "answer": "a", "error": ""}


class TestStatePull:
    def test_a_page_that_joined_late_can_read_the_current_state(self, tmp_path: Any) -> None:
        """Push alone cannot work: the page mounts after events have already flown."""
        state = StateBridge()
        bridge = _bridge(tmp_path, state=state)
        state.push_event(PipelineEvent(kind="wake", text="你好小夜"))
        state.push_event(PipelineEvent(kind="state", text="processing"))

        snapshot = bridge.state_snapshot()

        assert snapshot["voice_state"] == "processing"
        assert snapshot["last_event"] == "state"

    def test_the_snapshot_is_json_serialisable(self, tmp_path: Any) -> None:
        state = StateBridge()
        state.push_event(PipelineEvent(kind="reply", text="你好"))
        bridge = _bridge(tmp_path, state=state)

        encoded = json.dumps(bridge.state_snapshot(), ensure_ascii=False)

        assert "你好" in encoded

    def test_no_state_bridge_still_answers_with_a_shape(self, tmp_path: Any) -> None:
        bridge = _bridge(tmp_path)

        snapshot = bridge.state_snapshot()

        assert snapshot["voice"] == "off"
        assert snapshot["history"] == []


class TestPushScript:
    def test_a_snapshot_becomes_one_call_with_json_inside(self) -> None:
        window = FakeWindow()
        state = UiState(
            voice_state=UiVoiceState.LISTENING,
            history=(),
            last_event="wake",
            interrupted=False,
        )

        push_state(window, state)

        assert len(window.scripts) == 1
        script = window.scripts[0]
        assert script.startswith("window.__jarvisState && window.__jarvisState(")
        inner = script[len("window.__jarvisState && window.__jarvisState(") : -len(");")]
        payload = json.loads(inner)
        assert payload["voice_state"] == "listening"

    def test_chinese_survives_the_trip(self) -> None:
        window = FakeWindow()
        state = UiState(
            voice_state=UiVoiceState.PROCESSING,
            history=(),
            last_event="你好小夜",
            interrupted=False,
        )

        push_state(window, state)

        assert "你好小夜" in window.scripts[0]
