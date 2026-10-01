"""Tests for the desktop shell's Python surface: what the page can actually call.

``run()`` needs a real WebView2 window and so stays out of here. Everything the
page touches *through* it is testable: the bridge methods, the state snapshot the
page pulls on mount, and the script the pump pushes into the browser.
"""

from __future__ import annotations

import datetime
import json
import os
import threading
import time
from typing import Any, cast

from jarvis.app.chat_service import ChatReply, ChatService
from jarvis.app.disk_service import DiskService
from jarvis.app.preferences import WINDOW_RECT, Preferences
from jarvis.app.system_service import SystemService
from jarvis.app.voice_picker import VoicePicker
from jarvis.app.voice_service import VoiceService
from jarvis.config.schema import ToolsSection, TtsSection
from jarvis.core.events import PipelineEvent, VoicePhase
from jarvis.tools.disk_cleaner import DiskCleaner
from jarvis.tools.monitor import SystemMonitor
from jarvis.tools.service import ToolService
from jarvis.ui.audio_bridge import AudioPusher
from jarvis.ui.desktop import (
    JUNK_BRIEFING,
    HudBridge,
    WindowGeometry,
    _watch_geometry,
    allow_autoplay,
    push_state,
    stored_geometry,
    voice_status_dict,
)
from jarvis.ui.state_bridge import StateBridge, UiState, UiVoiceState
from tests._fakes import FakeLlmClient


class FakeLoop:
    def __init__(self) -> None:
        self.starts = 0
        self.stops = 0
        self.talks = 0
        self.read_aloud: list[str] = []
        self.reading_stops = 0

    @property
    def listening(self) -> bool:
        return self.starts > self.stops

    def start(self) -> None:
        self.starts += 1

    def stop(self) -> None:
        self.stops += 1

    def speak_now(self) -> bool:
        self.talks += 1
        return True

    def speak_text(self, text: str) -> bool:
        self.read_aloud.append(text)
        return True

    def stop_speaking(self) -> bool:
        self.reading_stops += 1
        return bool(self.read_aloud)


class FakeWindow:
    """Records the scripts a push would have run in the browser."""

    def __init__(self) -> None:
        self.scripts: list[str] = []

    def evaluate_js(self, script: str) -> Any:
        self.scripts.append(script)
        return None


class FakeSettings:
    """The one question the bridge asks the settings store about a typed answer."""

    def __init__(self, *, speaks_typed: bool = True) -> None:
        self._speaks_typed = speaks_typed

    def speaks_typed(self) -> bool:
        return self._speaks_typed


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
    settings: Any = None,
    audio: AudioPusher | None = None,
    disk: DiskService | None = None,
    tools: ToolService | None = None,
    voice_picker: VoicePicker | None = None,
    lifecycle: Any = None,
    reminders: Any = None,
    announcer: Any = None,
    memory: Any = None,
    knowledge: Any = None,
) -> HudBridge:
    system = SystemService(_NoTelemetry())
    return HudBridge(
        system,
        disk or DiskService(lambda: DiskCleaner(audit_log=tmp_path / "a.jsonl", sources={})),
        voice=voice,
        chat=chat,
        state=state,
        settings=settings,
        audio=audio,
        tools=tools,
        voice_picker=voice_picker,
        lifecycle=lifecycle,
        reminders=reminders,
        announcer=announcer,
        memory=memory,
        knowledge=knowledge,
    )


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

    def test_talk_opens_one_turn_on_the_live_loop(self, tmp_path: Any) -> None:
        loop = FakeLoop()
        voice = VoiceService(lambda _on_event: loop, permission=lambda: True)
        bridge = _bridge(tmp_path, voice=voice)
        voice.enable()
        assert _wait_running(voice)

        status = bridge.voice_talk()

        assert loop.talks == 1
        assert status["phase"] == "running"
        assert status["detail"], "the press needs something to show either way"

    def test_talk_without_a_voice_stack_says_so(self, tmp_path: Any) -> None:
        bridge = _bridge(tmp_path)

        status = bridge.voice_talk()

        assert status["phase"] == "off"
        assert status["detail"]

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
        assert voice_status_dict(object()) == {"phase": "off", "detail": "", "keyword": ""}


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

    def test_a_typed_turn_lands_in_the_same_transcript_as_a_spoken_one(self, tmp_path: Any) -> None:
        """One log, not two: the page renders ``state.history`` for both paths."""
        chat = ChatService(lambda: FakeLlmClient("今天是星期六"))
        chat.start()
        state = StateBridge()
        bridge = _bridge(tmp_path, chat=chat, state=state)

        bridge.chat_ask("今天星期几")

        assert [(t.role, t.text) for t in state.snapshot().history] == [
            ("user", "今天星期几"),
            ("assistant", "今天是星期六"),
        ]

    def test_a_failed_turn_is_not_written_into_the_transcript(self, tmp_path: Any) -> None:
        def boom() -> FakeLlmClient:
            raise RuntimeError("没有配置 API Key")

        state = StateBridge()
        chat = ChatService(boom)
        chat.start()
        bridge = _bridge(tmp_path, chat=chat, state=state)

        bridge.chat_ask("你好")

        assert state.snapshot().history == ()

    def test_clearing_empties_the_transcript_without_touching_the_microphone(
        self, tmp_path: Any
    ) -> None:
        loop = FakeLoop()
        voice = VoiceService(lambda _on_event: loop, permission=lambda: True)
        chat = ChatService(lambda: FakeLlmClient("好的"))
        chat.start()
        state = StateBridge()
        bridge = _bridge(tmp_path, voice=voice, chat=chat, state=state)
        voice.enable()
        assert _wait_running(voice)
        bridge.chat_ask("现在几点")
        assert state.snapshot().history

        bridge.chat_clear()

        assert state.snapshot().history == ()
        assert chat.history_length == 0
        assert loop.stops == 0, "「清空」 is about the log, not the microphone"
        assert bridge.voice_status()["phase"] == "running"


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


def _audio_payload(script: str) -> Any:
    start = script.index("({") + 1
    end = script.rindex("})") + 1
    return json.loads(script[start:end])


class TestAudioSurface:
    """The page plays the assistant, so it has to be able to say whether it can."""

    def test_a_bridge_without_the_audio_channel_says_speaker(self, tmp_path: Any) -> None:
        bridge = _bridge(tmp_path)

        assert bridge.audio_output()["output"] == "speaker"

    def test_the_page_can_decline_audio_and_is_told_what_happened_instead(
        self, tmp_path: Any
    ) -> None:
        pusher = AudioPusher()
        pusher.attach_window(FakeWindow())
        bridge = _bridge(tmp_path, audio=pusher)

        answer = bridge.audio_ready(False, "浏览器拒绝了音频上下文")

        assert answer["output"] == "speaker"
        assert answer["reason"] == "浏览器拒绝了音频上下文"
        assert pusher.ready is False

    def test_a_page_that_claims_it_can_play_becomes_the_output_device(self, tmp_path: Any) -> None:
        pusher = AudioPusher()
        pusher.attach_window(FakeWindow())
        bridge = _bridge(tmp_path, audio=pusher)

        assert bridge.audio_ready(True)["output"] == "browser"
        assert bridge.audio_output()["output"] == "browser"

    def test_a_process_with_no_channel_refuses_the_claim(self, tmp_path: Any) -> None:
        """Answering the page's question with the truth, not with a fake success."""
        bridge = _bridge(tmp_path)

        assert bridge.audio_ready(True)["output"] == "speaker"

    def test_stopping_speech_cuts_both_copies_of_the_answer(self, tmp_path: Any) -> None:
        """Half a stop is the bug this pins: cancelling synthesis in Python while the
        page keeps playing what it already received reads as a button that does not
        work, in the one place the operator expected it to."""
        loop = FakeLoop()
        voice = VoiceService(lambda _on_event: loop, permission=lambda: True)
        pusher = AudioPusher()
        window = FakeWindow()
        pusher.attach_window(window)
        pusher.mark_ready(True)
        bridge = _bridge(tmp_path, voice=voice, audio=pusher)
        voice.enable()
        assert _wait_running(voice)

        result = bridge.speech_stop()

        assert result == {"stopped": True}
        assert loop.reading_stops == 1
        assert _audio_payload(window.scripts[-1])["flush"] is True


class TestTypedAnswerIsReadAloud:
    def test_a_settings_blessing_is_what_makes_a_typed_answer_speakable(
        self, tmp_path: Any
    ) -> None:
        loop = FakeLoop()
        voice = VoiceService(lambda _on_event: loop, permission=lambda: True)
        chat = ChatService(lambda: FakeLlmClient("今天是星期六"))
        chat.start()
        bridge = _bridge(tmp_path, voice=voice, chat=chat, settings=FakeSettings())
        voice.enable()
        assert _wait_running(voice)

        bridge.chat_ask("今天星期几")

        assert loop.read_aloud == ["今天是星期六"]

    def test_turning_the_toggle_off_silences_it_on_the_very_next_question(
        self, tmp_path: Any
    ) -> None:
        """Read live rather than cached: a switch that only takes effect after a
        restart is a switch nobody believes."""
        loop = FakeLoop()
        voice = VoiceService(lambda _on_event: loop, permission=lambda: True)
        chat = ChatService(lambda: FakeLlmClient("好的"))
        chat.start()
        bridge = _bridge(
            tmp_path,
            voice=voice,
            chat=chat,
            settings=FakeSettings(speaks_typed=False),
        )
        voice.enable()
        assert _wait_running(voice)

        bridge.chat_ask("你好")

        assert loop.read_aloud == []

    def test_a_failed_answer_is_not_read_out(self, tmp_path: Any) -> None:
        """Reading "没有配置 API Key" aloud would be the assistant announcing its own
        outage in a calm voice."""

        def boom() -> FakeLlmClient:
            raise RuntimeError("没有配置 API Key")

        loop = FakeLoop()
        voice = VoiceService(lambda _on_event: loop, permission=lambda: True)
        chat = ChatService(boom)
        chat.start()
        bridge = _bridge(tmp_path, voice=voice, chat=chat, settings=FakeSettings())
        voice.enable()
        assert _wait_running(voice)

        bridge.chat_ask("你好")

        assert loop.read_aloud == []

    def test_a_text_only_process_still_answers(self, tmp_path: Any) -> None:
        """The whole point of the typed path: no microphone, no models, an answer."""
        chat = ChatService(lambda: FakeLlmClient("今天是星期六"))
        chat.start()
        bridge = _bridge(tmp_path, chat=chat, settings=FakeSettings())

        assert bridge.chat_ask("今天星期几")["answer"] == "今天是星期六"


class TestAutoplayFlag:
    def test_the_window_is_launched_allowed_to_speak_without_a_click(self) -> None:
        """A fresh desktop window has never been clicked, and Chromium's autoplay
        policy would leave the audio context suspended -- an assistant that cannot
        talk until the operator clicks something has not solved what it was built for."""
        environ: dict[str, str] = {}

        assert allow_autoplay(environ) == "--autoplay-policy=no-user-gesture-required"
        assert environ["WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS"] == (
            "--autoplay-policy=no-user-gesture-required"
        )

    def test_an_operators_own_arguments_are_extended_not_replaced(self) -> None:
        """Whoever set this variable by hand (to debug the sandbox) knows more about
        that window than this function does."""
        environ = {"WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS": "--no-sandbox"}

        merged = allow_autoplay(environ)

        assert merged == "--no-sandbox --autoplay-policy=no-user-gesture-required"
        assert environ["WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS"] == merged

    def test_running_it_twice_does_not_stack_the_flag(self) -> None:
        environ: dict[str, str] = {}

        allow_autoplay(environ)
        again = allow_autoplay(environ)

        assert again.count("no-user-gesture-required") == 1


class TestJunkAdvice:
    """The 协助分析 button: an opinion about a scan, never an action on it.

    What these pin is the shape of the *ask*: the model gets the briefing and the
    digest, the page gets the answer, and the turn is kept out of the conversation.
    That the deletion path is untouched is already covered by the disk service's own
    tests -- what is new here is a model getting to look.
    """

    @staticmethod
    def _chat(client: FakeLlmClient) -> ChatService:
        service = ChatService(lambda: client)
        service.start()
        return service

    @staticmethod
    def _scanned(tmp_path: Any) -> DiskService:
        directory = tmp_path / "temp"
        directory.mkdir()
        leftover = directory / "old.tmp"
        leftover.write_bytes(b"x" * 512)
        stale = time.time() - 60.0
        os.utime(leftover, (stale, stale))
        service = DiskService(
            lambda: DiskCleaner(audit_log=tmp_path / "a.jsonl", sources={"临时文件": (directory,)})
        )
        service.start()
        service.plan()
        return service

    def test_without_a_chat_service_it_says_which_part_is_missing(self, tmp_path: Any) -> None:
        bridge = _bridge(tmp_path)

        result = bridge.disk_advice()

        assert result["text"] == ""
        assert "对话服务" in str(result["error"])

    def test_without_a_scan_there_is_nothing_to_analyse(self, tmp_path: Any) -> None:
        bridge = _bridge(tmp_path, chat=self._chat(FakeLlmClient("不该被调用")))

        result = bridge.disk_advice()

        assert "扫描" in str(result["error"])

    def test_the_briefing_and_the_digest_both_reach_the_model(self, tmp_path: Any) -> None:
        client = FakeLlmClient("这一项是构建缓存")
        bridge = _bridge(tmp_path, chat=self._chat(client), disk=self._scanned(tmp_path))

        result = bridge.disk_advice()

        assert result["text"] == "这一项是构建缓存"
        prompt = client.calls[-1][-1].content or ""
        assert JUNK_BRIEFING in prompt
        assert "old.tmp" in prompt

    def test_the_advice_turn_stays_out_of_the_conversation(self, tmp_path: Any) -> None:
        chat = self._chat(FakeLlmClient("ok"))
        bridge = _bridge(tmp_path, chat=chat, disk=self._scanned(tmp_path))

        bridge.disk_advice()

        assert chat.history_length == 0


class TestActivitySurface:
    """「动作」: the tool registry's own memory, reachable from the page."""

    def test_without_a_tool_service_the_panel_says_which_part_is_missing(
        self, tmp_path: Any
    ) -> None:
        bridge = _bridge(tmp_path)

        assert bridge.activity_log() == {"entries": [], "error": "工具服务不可用"}

    def test_a_refusal_shows_up_because_that_is_the_entry_worth_reading(
        self, tmp_path: Any
    ) -> None:
        section = ToolsSection(
            enabled=True,
            confirm_dangerous=True,
            allow_write=False,
            allow_shell=False,
            file_roots=(),
            max_result_chars=2000,
        )
        service = ToolService(
            lambda: section,
            monitor_factory=SystemMonitor,
            cleaner_factory=lambda: DiskCleaner(audit_log=tmp_path / "a.jsonl", sources={}),
        )
        service.start()
        bridge = _bridge(tmp_path, tools=service)

        service.invoke("no_such_tool")

        payload = bridge.activity_log()
        assert payload["error"] == ""
        entries = payload["entries"]
        assert isinstance(entries, list) and entries
        first = entries[0]
        assert isinstance(first, dict)
        assert first["tool"] == "no_such_tool"
        assert first["ok"] is False


class TestVoicePickerSurface:
    """The 音色 popup's three bridge calls, including the two refusals."""

    @staticmethod
    def _picker(tmp_path: Any, *, emit: Any = None) -> VoicePicker:
        prefs = Preferences(tmp_path / "prefs.json")
        return VoicePicker(
            lambda: TtsSection(
                enabled=True,
                engine="edge_tts",
                voice="zh-CN-XiaoxiaoNeural",
                speed=1.0,
                volume=1.0,
                device="cpu",
                model="",
            ),
            prefs,
            emit=emit,
        )

    def test_without_a_picker_the_popup_says_which_part_is_missing(self, tmp_path: Any) -> None:
        bridge = _bridge(tmp_path)

        assert "不可用" in str(bridge.tts_voices()["error"])
        assert bridge.tts_preview("zh-CN-XiaoxiaoNeural")["ok"] is False

    def test_the_list_and_the_pick_both_come_back_in_one_round_trip(self, tmp_path: Any) -> None:
        bridge = _bridge(tmp_path, voice_picker=self._picker(tmp_path))

        listing = bridge.tts_pick("zh-CN-YunxiNeural")

        assert listing["error"] == ""
        assert listing["current"] == "zh-CN-YunxiNeural"
        assert bridge.tts_voices()["current"] == "zh-CN-YunxiNeural"

    def test_a_preview_with_no_page_audio_channel_is_a_refusal_not_a_crash(
        self, tmp_path: Any
    ) -> None:
        bridge = _bridge(tmp_path, voice_picker=self._picker(tmp_path))

        result = bridge.tts_preview("zh-CN-XiaoxiaoNeural")

        assert result["ok"] is False
        assert "音频通道" in str(result["error"])


class _FakeEvent:
    def __init__(self) -> None:
        self.handlers: list[Any] = []

    def __iadd__(self, handler: Any) -> _FakeEvent:
        self.handlers.append(handler)
        return self


class _FakeEvents:
    def __init__(self) -> None:
        self.closed = _FakeEvent()
        self.resized = _FakeEvent()
        self.moved = _FakeEvent()
        self.maximized = _FakeEvent()
        self.restored = _FakeEvent()


class _FakeWindow:
    """A window whose geometry the test moves, and whose events it fires."""

    def __init__(self) -> None:
        self.x, self.y, self.width, self.height = 100, 50, 1280, 800
        self.events = _FakeEvents()
        self.calls: list[str] = []

    def maximize(self) -> None:
        self.calls.append("maximize")
        self.events.maximized.handlers[0]()

    def restore(self) -> None:
        self.calls.append("restore")
        self.events.restored.handlers[0]()


def _as(value: object) -> dict[str, Any]:
    """Narrow a bridge payload so the assertions below can index into it."""
    assert isinstance(value, dict)
    return cast("dict[str, Any]", value)


class TestWindowGeometry:
    def test_nothing_stored_means_no_rect_rather_than_a_zero_one(self, tmp_path: Any) -> None:
        preferences = Preferences(tmp_path / "preferences.json")
        assert stored_geometry(preferences, width=1280, height=800) is None
        assert stored_geometry(None, width=1280, height=800) is None

    def test_a_stored_rect_round_trips_and_respects_the_minimum(self, tmp_path: Any) -> None:
        preferences = Preferences(tmp_path / "preferences.json")
        preferences.set(
            WINDOW_RECT,
            {"x": 40, "y": 30, "width": 1500, "height": 900, "maximized": True},
        )
        geometry = stored_geometry(preferences, width=1280, height=800)
        assert geometry == WindowGeometry(x=40, y=30, width=1500, height=900, maximized=True)
        preferences.set(WINDOW_RECT, {"x": 0, "y": 0, "width": 10, "height": 10})
        small = stored_geometry(preferences, width=1280, height=800)
        assert small is not None and (small.width, small.height) == (900, 600)

    def test_garbage_in_the_stored_rect_falls_back_instead_of_raising(self, tmp_path: Any) -> None:
        preferences = Preferences(tmp_path / "preferences.json")
        preferences.set(WINDOW_RECT, {"x": "left", "width": True, "maximized": "yes"})
        geometry = stored_geometry(preferences, width=1280, height=800)
        # A bool is an int in Python; "left" is not a coordinate. Both fall back.
        assert geometry is not None
        assert geometry.x == 80 and geometry.width == 1280 and geometry.maximized is True

    def test_a_maximised_rect_is_never_saved_as_the_restore_size(self, tmp_path: Any) -> None:
        preferences = Preferences(tmp_path / "preferences.json")
        window = _FakeWindow()
        keeper = _watch_geometry(window, preferences, WindowGeometry(100, 50, 1280, 800))
        keeper.set_maximized(True)
        window.x, window.y, window.width, window.height = 0, 0, 1920, 1080
        keeper.capture()  # maximised: this must not overwrite the normal rect
        keeper.save()
        stored = preferences.get(WINDOW_RECT)
        assert stored == {"x": 100, "y": 50, "width": 1280, "height": 800, "maximized": True}

    def test_restoring_then_resizing_updates_the_remembered_rect(self, tmp_path: Any) -> None:
        preferences = Preferences(tmp_path / "preferences.json")
        window = _FakeWindow()
        keeper = _watch_geometry(window, preferences, WindowGeometry(100, 50, 1280, 800))
        window.width, window.height = 1400, 900
        window.events.resized.handlers[0]()
        keeper.save()
        stored = preferences.get(WINDOW_RECT)
        assert stored is not None and stored["width"] == 1400 and stored["maximized"] is False

    def test_the_bridge_toggles_and_reports_the_state_it_actually_set(self, tmp_path: Any) -> None:
        window = _FakeWindow()
        keeper = _watch_geometry(window, None, WindowGeometry(0, 0, 1280, 800))
        bridge = _bridge(tmp_path)
        bridge.attach_window(window, keeper)
        assert bridge.window_state() == {"maximized": False, "error": ""}
        assert bridge.window_toggle_max() == {"maximized": True, "error": ""}
        assert window.calls == ["maximize"]
        assert bridge.window_toggle_max() == {"maximized": False, "error": ""}
        assert window.calls == ["maximize", "restore"]

    def test_a_bridge_without_a_window_says_so_instead_of_lieing(self, tmp_path: Any) -> None:
        bridge = _bridge(tmp_path)
        assert bridge.window_state()["maximized"] is False
        answer = bridge.window_toggle_max()
        assert answer["maximized"] is False and answer["error"]


class TestCommandLevelSurface:
    """The second lock: the command line, on the same dialog as the first."""

    def test_no_command_service_still_answers_with_a_shape(self, tmp_path: Any) -> None:
        bridge = _bridge(tmp_path)
        listing = bridge.shell_levels()
        assert listing["error"] and listing["levels"] == [] and listing["commands"] == []

    def test_the_level_the_page_sets_is_the_level_the_tool_reads(self, tmp_path: Any) -> None:
        from jarvis.app.command_access import CommandAccess
        from jarvis.app.preferences import Preferences

        access = CommandAccess(Preferences(tmp_path / "p.json"), tmp_path / "commands.jsonl")
        bridge = _bridge(tmp_path)
        bridge._command_access = access  # the wiring under test, not the wiring of run()

        assert _as(bridge.shell_levels()["current"])["mode"] == "off"
        answer = bridge.shell_set_tier(1)
        assert answer["error"] == "" and _as(answer["current"])["label"] == "演练"
        # The tool asks the same object, so the dialog and the policy cannot drift.
        assert access.mode() == "rehearsal"

    def test_a_refused_tier_keeps_its_error_message(self, tmp_path: Any) -> None:
        from jarvis.app.command_access import CommandAccess
        from jarvis.app.preferences import Preferences

        access = CommandAccess(Preferences(tmp_path / "p.json"), tmp_path / "commands.jsonl")
        bridge = _bridge(tmp_path)
        bridge._command_access = access
        answer = bridge.shell_set_tier(9)
        assert answer["error"] and _as(answer["current"])["mode"] == "off"


class TestProcessKillSurface:
    """The bridge's share of "tick a row, then press 确认结束"."""

    def test_no_process_service_answers_with_a_shape(self, tmp_path: Any) -> None:
        bridge = _bridge(tmp_path)
        answer = bridge.process_kill([{"pid": 1, "name": "x.exe"}], True)
        assert answer["results"] == [] and answer["error"]

    def test_confirmation_is_passed_through_and_rechecked_below(self, tmp_path: Any) -> None:
        from jarvis.app.process_service import ProcessService
        from jarvis.tools.process_control import ProcessController

        controller = ProcessController(
            psutil_module=_NoProcesses(),
            audit_log=tmp_path / "kills.jsonl",
            own_pids=[],
        )
        bridge = _bridge(tmp_path)
        bridge._process = ProcessService(lambda: controller)

        unconfirmed = bridge.process_kill([{"pid": 42, "name": "app.exe"}], False)
        assert "二次确认" in str(unconfirmed["error"])

        confirmed = bridge.process_kill([{"pid": 42, "name": "app.exe"}], True)
        assert confirmed["ended"] == 0
        # The pid is not in the table, so the honest answer is "已经不在了" --
        # not a crash, and not a silent success.
        rows = confirmed["results"]
        assert isinstance(rows, list) and isinstance(rows[0], dict)
        assert rows[0]["note"] == "进程已经不在了"


class _NoProcesses:
    AccessDenied = type("AccessDenied", (Exception,), {})
    NoSuchProcess = type("NoSuchProcess", (Exception,), {})

    def Process(self, pid: int) -> Any:  # noqa: N802 - psutil's spelling
        raise self.NoSuchProcess(pid)


class TestTraySurface:
    """What the page is told about the X, and what it can ask the shell to do."""

    def test_shell_state_without_a_lifecycle_claims_a_normal_window(self, tmp_path: Any) -> None:
        bridge = _bridge(tmp_path)
        assert bridge.shell_state() == {
            "visible": True,
            "tray": False,
            "status": "off",
            "pet": False,
        }

    def test_window_hide_refuses_when_there_is_no_tray(self, tmp_path: Any) -> None:
        """The button must not offer to hide the app where it cannot be found again."""
        from jarvis.ui.lifecycle import ShellLifecycle

        lifecycle = ShellLifecycle(tray=None)
        bridge = _bridge(tmp_path, lifecycle=lifecycle)
        answer = bridge.window_hide()
        assert answer["visible"] is True
        assert "托盘不可用" in str(answer["error"])

    def test_window_hide_hides_and_reports(self, tmp_path: Any) -> None:
        from jarvis.ui.lifecycle import ShellLifecycle
        from tests.test_ui_lifecycle import FakeTray, FakeWindow

        window = FakeWindow()
        lifecycle = ShellLifecycle(window, tray=FakeTray())
        bridge = _bridge(tmp_path, lifecycle=lifecycle)
        assert bridge.window_hide() == {"visible": False, "error": ""}
        assert window.calls == ["hide"]
        assert bridge.shell_state()["visible"] is False

    def test_app_info_says_the_tray_exists(self, tmp_path: Any) -> None:
        """The title line is where an operator learns whether the X is reversible."""
        bridge = _bridge(tmp_path)
        assert "tray" in bridge.app_info()


class _FakePet:
    """The pet window, as far as the bridge's four calls are concerned."""

    def __init__(self) -> None:
        self.grips: list[object] = []
        self.dragging: list[bool] = []

    def report_grip(self, rect: object) -> None:
        self.grips.append(rect)

    def set_dragging(self, dragging: bool) -> None:
        self.dragging.append(dragging)


class TestPetSurface:
    def test_pet_state_without_a_window_says_it_is_not_there(self, tmp_path: Any) -> None:
        assert _bridge(tmp_path).pet_state() == {"shown": False, "error": ""}

    def test_toggle_without_a_lifecycle_refuses_with_a_reason(self, tmp_path: Any) -> None:
        answer = _bridge(tmp_path).pet_toggle()
        assert answer["shown"] is False
        assert "桌面宠物" in str(answer["error"])

    def test_toggling_remembers_the_operators_choice(self, tmp_path: Any) -> None:
        from jarvis.ui.lifecycle import ShellLifecycle
        from tests.test_ui_lifecycle import FakePet, FakeTray, FakeWindow

        pet = FakePet()
        lifecycle = ShellLifecycle(FakeWindow(), tray=FakeTray(), pet=pet)
        stored: list[bool] = []
        bridge = _bridge(tmp_path, lifecycle=lifecycle)
        bridge.attach_pet(None, remember=stored.append)
        assert bridge.pet_toggle() == {"shown": True, "error": ""}
        assert stored == [True]
        assert bridge.pet_toggle() == {"shown": False, "error": ""}
        assert stored == [True, False]

    def test_the_grip_and_the_drag_flag_reach_the_window(self, tmp_path: Any) -> None:
        from jarvis.ui.lifecycle import ShellLifecycle
        from tests.test_ui_lifecycle import FakeTray, FakeWindow

        pet = _FakePet()
        bridge = _bridge(tmp_path, lifecycle=ShellLifecycle(FakeWindow(), tray=FakeTray()))
        bridge.attach_pet(cast(Any, pet))
        rect = {"x": 0.6, "y": 0.02, "width": 0.3, "height": 0.06}
        assert bridge.pet_grip(rect) == {"ok": True}
        assert bridge.pet_drag(True) == {"ok": True}
        assert pet.grips == [rect]
        assert pet.dragging == [True]

    def test_grip_calls_without_a_pet_window_are_inert(self, tmp_path: Any) -> None:
        bridge = _bridge(tmp_path)
        assert bridge.pet_grip({"x": 0, "y": 0, "width": 1, "height": 1}) == {"ok": False}
        assert bridge.pet_drag(True) == {"ok": False}


class _FakeMemoryRow:
    def __init__(self, memory_id: int, content: str) -> None:
        self.memory_id = memory_id
        self.kind = "fact"
        self.scope = "user"
        self.content = content
        self.source = "chat"

    def to_dict(self) -> dict[str, object]:
        return {
            "memory_id": self.memory_id,
            "kind": self.kind,
            "scope": self.scope,
            "content": self.content,
            "source": self.source,
        }


class _FakeMemory:
    def __init__(self) -> None:
        self.rows = [_FakeMemoryRow(1, "喜欢深色主题"), _FakeMemoryRow(2, "常用浏览器是 Edge")]
        self.forgotten: list[int] = []
        self.wiped = 0

    def list_memories(self) -> list[Any]:
        return list(self.rows)

    def forget(self, memory_id: int) -> bool:
        self.forgotten.append(memory_id)
        before = len(self.rows)
        self.rows = [row for row in self.rows if row.memory_id != memory_id]
        return len(self.rows) < before

    def forget_scope(self, scope: Any = None) -> int:
        self.wiped += 1
        removed = len(self.rows)
        self.rows = []
        return removed


class _FakeKnowledge:
    def __init__(self) -> None:
        self.docs = [{"doc_id": "d1", "source": "E:/笔记.md", "title": "笔记", "chunks": 3}]
        self.ingested: list[str] = []
        self.forgot: list[str] = []

    def documents(self) -> list[Any]:
        return list(self.docs)

    def stats(self) -> dict[str, object]:
        return {"records": 3, "chunks": 3}

    def ingest(self, path: Any, *, force: bool = False) -> dict[str, object]:
        text = str(path)
        self.ingested.append(text)
        if "缺失" in text:
            return {"doc_id": "", "error": "路径不存在"}
        return {
            "doc_id": f"d{len(self.ingested)}",
            "source": text,
            "chunks": 1,
            "skipped_reason": "",
        }

    def forget(self, doc_id: str) -> bool:
        self.forgot.append(doc_id)
        return doc_id == "d1"

    def retrieve(self, query: str, *, top_k: int = 0, min_score: float = -1.0) -> list[Any]:
        return [{"doc_id": "d1", "text": f"关于「{query}」的一段", "score": 0.82}]


class _JobStore:
    """A scheduler double that actually keeps what it was given.

    ``add_job`` returning the spec without storing it would let a test assert that a
    reminder exists right after creating one -- and then the panel, which reads the
    list, would show nothing. That mismatch is the bug this class exists to catch.
    """

    def __init__(self) -> None:
        self.jobs: list[Any] = []

    def add_job(self, spec: Any) -> Any:
        self.jobs.append(spec)
        return spec

    def remove_job(self, job_id: str) -> bool:
        before = len(self.jobs)
        self.jobs = [job for job in self.jobs if job.job_id != job_id]
        return len(self.jobs) < before

    def set_enabled(self, job_id: str, enabled: bool) -> bool:
        return any(job.job_id == job_id for job in self.jobs)

    def list_jobs(self) -> list[Any]:
        return list(self.jobs)

    def next_run_time(self, job_id: str) -> str:
        return "2026-10-02 12:10:00"


class TestAssistantSurface:
    """The three panels' bridge calls: what they answer, and what they never do."""

    def test_reminders_without_a_service_says_why(self, tmp_path: Any) -> None:
        board = _bridge(tmp_path).reminders()
        assert board["rows"] == [] and "没有接提醒" in str(board["error"])

    def test_reminders_reports_rows_and_channels(self, tmp_path: Any) -> None:
        from jarvis.app.announcer import Announcer
        from jarvis.app.reminder_service import ReminderService

        clock = datetime.datetime(2026, 10, 2, 12, 0)
        store = _JobStore()
        service = ReminderService(lambda: store, clock=lambda: clock)
        bridge = _bridge(
            tmp_path,
            reminders=service,
            announcer=Announcer(speak=lambda text: True),
        )
        added = bridge.reminder_add("喝水", "十分钟后")
        assert added["ok"] is True
        assert added["row"]["text"] == "喝水"  # type: ignore[index]
        board = bridge.reminders()
        assert len(board["rows"]) == 1  # type: ignore[arg-type]
        assert board["channels"] == ["voice"]

    def test_a_refused_time_comes_back_as_an_answer_not_a_crash(self, tmp_path: Any) -> None:
        import datetime

        from jarvis.app.reminder_service import ReminderService

        class Scheduler:
            def add_job(self, spec: Any) -> Any:
                return spec

            def remove_job(self, job_id: str) -> bool:
                return True

            def set_enabled(self, job_id: str, enabled: bool) -> bool:
                return True

            def list_jobs(self) -> list[Any]:
                return []

            def next_run_time(self, job_id: str) -> str:
                return ""

        clock = datetime.datetime(2026, 10, 2, 12, 0)
        bridge = _bridge(
            tmp_path, reminders=ReminderService(lambda: _JobStore(), clock=lambda: clock)
        )
        answer = bridge.reminder_add("喝水", "回头")
        assert answer["ok"] is False and "听不懂" in str(answer["error"])

    def test_memory_rows_reach_the_page_as_plain_json(self, tmp_path: Any) -> None:
        memory = _FakeMemory()
        bridge = _bridge(tmp_path, memory=memory)
        rows = bridge.memory_list()["rows"]
        assert isinstance(rows, list) and isinstance(rows[0], dict)
        assert rows[0]["content"] == "喜欢深色主题"
        assert bridge.memory_forget(1)["ok"] is True
        assert memory.forgotten == [1]

    def test_wiping_memory_needs_confirmation_every_time(self, tmp_path: Any) -> None:
        memory = _FakeMemory()
        bridge = _bridge(tmp_path, memory=memory)
        refused = bridge.memory_forget_all(False)
        assert refused["removed"] == 0 and "二次确认" in str(refused["error"])
        assert memory.wiped == 0
        assert bridge.memory_forget_all(True)["removed"] == 2

    def test_a_bad_memory_id_is_refused_without_calling_the_service(self, tmp_path: Any) -> None:
        memory = _FakeMemory()
        bridge = _bridge(tmp_path, memory=memory)
        assert bridge.memory_forget("三")["ok"] is False
        assert memory.forgotten == []

    def test_knowledge_state_and_probe(self, tmp_path: Any) -> None:
        knowledge = _FakeKnowledge()
        bridge = _bridge(tmp_path, knowledge=knowledge)
        state = bridge.knowledge_state()
        documents = state["documents"]
        stats = state["stats"]
        assert isinstance(documents, list) and isinstance(documents[0], dict)
        assert documents[0]["title"] == "笔记"
        assert isinstance(stats, dict) and stats["chunks"] == 3
        hits = bridge.knowledge_probe("深色主题")["hits"]
        assert isinstance(hits, list) and "深色主题" in str(hits[0]["text"])

    def test_ingest_reports_the_service_error_rather_than_inventing_one(
        self, tmp_path: Any
    ) -> None:
        knowledge = _FakeKnowledge()
        bridge = _bridge(tmp_path, knowledge=knowledge)
        assert bridge.knowledge_ingest("")["ok"] is False
        bad = bridge.knowledge_ingest("E:/缺失.md")
        assert bad["ok"] is False and "不存在" in str(bad["error"])
        good = bridge.knowledge_ingest("E:/另一篇.md")
        assert good["ok"] is True
        assert knowledge.ingested == ["E:/缺失.md", "E:/另一篇.md"]

    def test_forgetting_a_document_names_the_disk_rule(self, tmp_path: Any) -> None:
        knowledge = _FakeKnowledge()
        bridge = _bridge(tmp_path, knowledge=knowledge)
        assert bridge.knowledge_forget("d1") == {"ok": True, "error": ""}
        assert knowledge.forgot == ["d1"]
