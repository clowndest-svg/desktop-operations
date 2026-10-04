"""Tests for the desktop shell's Python surface: what the page can actually call.

``run()`` needs a real WebView2 window and so stays out of here. Everything the
page touches *through* it is testable: the bridge methods, the state snapshot the
page pulls on mount, and the script the pump pushes into the browser.
"""

from __future__ import annotations

import base64
import datetime
import json
import os
import threading
import time
from collections.abc import Mapping, Sequence
from types import SimpleNamespace
from typing import Any, cast

import pytest

from jarvis.app.alerts import AlertService
from jarvis.app.chat_service import ChatReply, ChatService
from jarvis.app.disk_service import DiskService
from jarvis.app.preferences import WINDOW_RECT, Preferences
from jarvis.app.system_service import SystemService
from jarvis.app.voice_picker import VoicePicker
from jarvis.app.voice_service import VoiceService
from jarvis.config.schema import ToolsSection, TtsSection
from jarvis.core.events import PipelineEvent, VoicePhase
from jarvis.core.exceptions import PlannerError
from jarvis.planner import Plan, PlanStep
from jarvis.scheduler import JobRun
from jarvis.scheduler import JobSpec as SchedulerJobSpec
from jarvis.scheduler import TriggerKind as SchedulerTriggerKind
from jarvis.tools.disk_cleaner import DiskCleaner
from jarvis.tools.monitor import SystemMonitor
from jarvis.tools.service import ToolService
from jarvis.ui.audio_bridge import AudioPusher
from jarvis.ui.compositor import (
    BUBBLE_ADD,
    BUBBLE_STOP,
    BubblePalette,
    bubble_palette,
)
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
from jarvis.workflow.types import TriggerKind as WorkflowTriggerKind
from jarvis.workflow.types import WorkflowDef, WorkflowRun
from tests._fakes import FakeLlmClient


class FakeLoop:
    def __init__(self) -> None:
        self.starts = 0
        self.stops = 0
        self.pauses = 0
        self.talks = 0
        self.read_aloud: list[str] = []
        self.reading_stops = 0

    @property
    def listening(self) -> bool:
        return self.starts > self.stops + self.pauses

    def start(self) -> None:
        self.starts += 1

    def stop(self) -> None:
        self.stops += 1

    def stop_listening(self) -> None:
        self.pauses += 1

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
    voice_library: Any = None,
    voice_call: Any = None,
    lifecycle: Any = None,
    reminders: Any = None,
    announcer: Any = None,
    memory: Any = None,
    knowledge: Any = None,
    process: Any = None,
    computer_access: Any = None,
    scheduler: Any = None,
    workflow: Any = None,
    planner: Any = None,
    turns: Any = None,
    model_probe: Any = None,
    usage: Any = None,
    alerts: Any = None,
    system: SystemService | None = None,
) -> HudBridge:
    if system is None:
        system = SystemService(_NoTelemetry(), alerts=alerts)
    bridge = HudBridge(
        system,
        disk or DiskService(lambda: DiskCleaner(audit_log=tmp_path / "a.jsonl", sources={})),
        voice=voice,
        chat=chat,
        state=state,
        settings=settings,
        audio=audio,
        tools=tools,
        voice_picker=voice_picker,
        voice_library=voice_library,
        voice_call=voice_call,
        lifecycle=lifecycle,
        reminders=reminders,
        announcer=announcer,
        memory=memory,
        knowledge=knowledge,
        process=process,
        computer_access=computer_access,
        scheduler=scheduler,
        workflow=workflow,
        planner=planner,
        model_probe=model_probe,
        usage=usage,
        alerts=alerts,
    )
    if turns is not None:
        bridge.attach_turns(turns)
    return bridge


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
        assert loop.pauses == 1 and loop.stops == 0, "关掉聆听不该把播放器一起拆了"

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


class TestTypedTurnIsATurn:
    """A keyboard asks questions too, and nothing in the voice pipeline knows about it.

    This is the whole reason 「思考中」 was unreachable from the chat panel: the turn
    axis was driven by pipeline events, a typed question produces none, and the figure
    could not show she was working on something the operator could already see.
    """

    def test_a_typed_question_shows_as_thinking_while_it_is_being_answered(
        self, tmp_path: Any
    ) -> None:
        state = StateBridge()
        seen: list[str] = []
        state.subscribe(lambda snapshot: seen.append(snapshot.voice_state.value))
        chat = ChatService(lambda: FakeLlmClient("今天是星期六"))
        chat.start()
        bridge = _bridge(tmp_path, chat=chat, state=state)

        assert bridge.chat_ask("今天星期几")["answer"] == "今天是星期六"
        # The first snapshot carries the *question*, already on screen, and only the next
        # one says she is working on it. That order is the feature: an operator who
        # pressed Enter and sees their own sentence knows the app heard them, which is
        # the difference between "thinking" and "hung".
        assert seen[:2] == ["idle", "processing"], seen
        assert seen[-1] == "idle", seen
        assert [snapshot.role for snapshot in state.snapshot().history] == ["user", "assistant"]

    def test_an_answer_that_dies_still_stops_the_thinking(self, tmp_path: Any) -> None:
        """``finally``, not a happy path: a failed ask must not leave her 思考中 forever."""

        def boom() -> FakeLlmClient:
            raise RuntimeError("没有配置 API Key")

        state = StateBridge()
        chat = ChatService(boom)
        chat.start()
        bridge = _bridge(tmp_path, chat=chat, state=state)

        assert bridge.chat_ask("你好")["error"]
        assert state.snapshot().voice_state is UiVoiceState.IDLE

    def test_the_typed_path_needs_no_microphone_to_be_a_turn(self, tmp_path: Any) -> None:
        """The phase axis says the mic is off; the turn axis still says what she is doing."""
        state = StateBridge()
        chat = ChatService(lambda: FakeLlmClient("好的"))
        chat.start()
        bridge = _bridge(tmp_path, chat=chat, state=state)

        bridge.chat_ask("你好")

        snapshot = state.snapshot()
        assert snapshot.voice is VoicePhase.OFF
        assert snapshot.voice_state is UiVoiceState.IDLE
        assert len(snapshot.history) == 2


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
        assert ChatReply("q", "a").to_dict() == {
            "question": "q",
            "answer": "a",
            "error": "",
            "reasoning": "",
            "model": "",
            "provider": "",
            "conversation": "",
            "task_id": "",
            "cancelled": False,
            "record": "",
        }

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

    def test_a_failed_turn_shows_the_question_and_no_answer(self, tmp_path: Any) -> None:
        """What the optimistic render promises, and what it refuses to invent.

        The question stays on screen -- pressing Enter and having the sentence vanish
        because the provider was unreachable is the one thing this must not do. What
        stays away is the *answer*: no empty assistant bubble, no apology written on the
        model's behalf.
        """

        def boom() -> FakeLlmClient:
            raise RuntimeError("没有配置 API Key")

        state = StateBridge()
        chat = ChatService(boom)
        chat.start()
        bridge = _bridge(tmp_path, chat=chat, state=state)

        reply = bridge.chat_ask("你好")

        assert reply["error"]
        assert [turn.role for turn in state.snapshot().history] == ["user"]
        assert state.snapshot().streaming is None, "a dead turn must not leave a bubble behind"

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

    def test_the_style_sliders_move_through_the_bridge(self, tmp_path: Any) -> None:
        """The panel's two new sliders go through the same door as everything else.

        A slider is a number from a page, so the check that it is one lives behind
        this call -- and the reply has to carry the new state, because the page
        re-renders from it rather than from its own optimistic copy.
        """
        bridge = _bridge(tmp_path, voice_picker=self._picker(tmp_path))

        moved = bridge.tts_set_style(1.25, 0.5)

        assert moved["error"] == ""
        assert moved["speed"] == pytest.approx(1.25)
        assert moved["volume"] == pytest.approx(0.5)
        assert bridge.tts_voices()["speed"] == pytest.approx(1.25)

    def test_a_slider_value_the_backend_will_not_take_comes_back_as_a_refusal(
        self, tmp_path: Any
    ) -> None:
        bridge = _bridge(tmp_path, voice_picker=self._picker(tmp_path))

        refused = bridge.tts_set_style(4.0, None)

        assert "超出范围" in str(refused["error"])
        assert bridge.tts_voices()["speed"] == pytest.approx(1.0)


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
    """The pet window, as far as the bridge's calls are concerned."""

    def __init__(self) -> None:
        self.grips: list[object] = []
        self.dragging: list[bool] = []
        self.palettes: list[BubblePalette] = []

    def report_grip(self, rect: object) -> None:
        self.grips.append(rect)

    def set_dragging(self, dragging: bool) -> None:
        self.dragging.append(dragging)

    def set_palette(self, palette: BubblePalette) -> None:
        self.palettes.append(palette)

    def set_actions(self, actions: object) -> None:
        return None


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


class TestTheSkinReachesHerCards:
    """她说的话和那句「思考中」是 Python 画的，所以换肤要有人把颜色报过来。

    ``theme.ts`` 里那张配色表只有一份：页面报的是自己身上那三个墨色，桥面把它换算成卡片
    的三色。这里守的是那条缝 —— 页面报了、桥面接了、但她没收到，就是"人换了衣服、字还是
    原来那身"。
    """

    AMBER = (0x3A2408, 0xFFB04A, 0xFFE0B0)

    def test_the_reported_inks_are_derived_and_pushed_to_the_window(self, tmp_path: Any) -> None:
        pet = _FakePet()
        bridge = _bridge(tmp_path)
        bridge.attach_pet(cast(Any, pet))

        answer = bridge.pet_palette(*self.AMBER)

        assert answer["ok"] is True and answer["pet"] is True
        assert pet.palettes[-1] == bubble_palette(*self.AMBER), "卡片没跟着皮肤走"

    def test_a_palette_reported_before_she_exists_is_still_hers_when_she_appears(
        self, tmp_path: Any
    ) -> None:
        """换了皮肤时她可能还没开 —— 挂上来的那一刻得把颜色补给她，不能等下一次换肤。"""
        bridge = _bridge(tmp_path)
        assert bridge.pet_palette(*self.AMBER)["pet"] is False, "没有宠物不是错误"

        pet = _FakePet()
        bridge.attach_pet(cast(Any, pet))

        assert pet.palettes == [bubble_palette(*self.AMBER)], "先报的颜色没在挂载时补送"

    def test_a_colour_that_is_not_a_number_is_refused_and_changes_nothing(
        self, tmp_path: Any
    ) -> None:
        """边界上是 JS 传来的数：不是数就得说清楚，并且保住已经在用的那套。"""
        pet = _FakePet()
        bridge = _bridge(tmp_path)
        bridge.attach_pet(cast(Any, pet))
        bridge.pet_palette(*self.AMBER)
        worn = pet.palettes[-1]

        for bad in ("0x3a2408", -1, 0x11223344, None):
            answer = bridge.pet_palette(cast(Any, bad), 0xFFB04A, 0xFFE0B0)
            assert answer["ok"] is False, bad
            assert str(answer["error"]), "拒绝了却不说是为什么，用户在页面上看不出来"

        assert pet.palettes[-1] == worn, "一次坏输入不许把她已穿的颜色改掉"


class TestTheIconsOnHerCard:
    """她桌上那两枚图钉的是**动作**：按下去要落到真能做事的地方。

    卡片是 Python 画的，按它不会经过页面 —— 所以这条路上每一段都得钉住：任务表决定该不
    该有「停止」，按下之后走的是面板那颗按钮同一个调用，而「回复」在桌面那一页没有输入框，
    只能把面板叫回来。
    """

    class _CardPet:
        def __init__(self) -> None:
            self.thinking: list[bool] = []
            self.actions: list[object] = []

        def set_thinking(self, active: bool, *, source: str = "turn") -> None:
            self.thinking.append(bool(active))

        def set_actions(self, actions: Sequence[str]) -> None:
            self.actions.append(tuple(actions))

        def set_palette(self, palette: BubblePalette) -> None:
            return None

    @staticmethod
    def _slow_chat(tmp_path: Any) -> tuple[Any, threading.Event, threading.Event]:
        from jarvis.app.chat_service import ChatService

        inside = threading.Event()
        release = threading.Event()

        class Slow:
            provider_name = "slow"
            model = "slow-model"

            def complete(self, messages: Any, *, options: Any = None) -> Any:
                from jarvis.llm.types import ChatResponse

                inside.set()
                assert release.wait(5.0), "测试没放行就答完了"
                return ChatResponse(content="答", model="slow-model")

            def stream(self, messages: Any, *, options: Any = None) -> Any:
                return iter(())

        chat = ChatService(lambda: Slow())
        chat.start()
        return chat, inside, release

    def test_the_icons_follow_what_the_task_table_is_doing(self, tmp_path: Any) -> None:
        """有活要停才有「停止」；「再问一句」一直在。"""
        from jarvis.app.turns import TurnRegistry
        from jarvis.ui.state_bridge import StateBridge

        chat, inside, release = self._slow_chat(tmp_path)
        pet = self._CardPet()
        bridge = _bridge(tmp_path, chat=chat, state=StateBridge(), turns=TurnRegistry())
        bridge.attach_pet(cast(Any, pet))
        try:
            bridge.chat_send("慢慢想")
            assert inside.wait(5.0)

            # Through a function, so the first comparison does not narrow the type of the
            # second one: "the set changed to just ＋" is the thing being asserted here.
            def last() -> object:
                return pet.actions[-1]

            assert last() == (BUBBLE_STOP, BUBBLE_ADD), pet.actions

            release.set()
            assert bridge._turns is not None and bridge._turns.wait(_last_task(bridge._turns), 5.0)
            assert last() == (BUBBLE_ADD,), pet.actions
        finally:
            chat.stop()

    def test_stop_ends_the_round_she_is_in(self, tmp_path: Any) -> None:
        """和面板那颗按钮同一个调用 —— 两种停法就会有两种"到底停了什么"。"""
        from jarvis.app.turns import STATE_CANCELLED, STATE_RUNNING, TurnRegistry
        from jarvis.ui.state_bridge import StateBridge

        chat, inside, release = self._slow_chat(tmp_path)
        registry = TurnRegistry()
        bridge = _bridge(tmp_path, chat=chat, state=StateBridge(), turns=registry)
        started = bridge.chat_send("慢慢想")
        assert inside.wait(5.0)

        answer = bridge.pet_action(BUBBLE_STOP)
        assert answer["ok"] is True, answer
        task = registry.get(str(started["task_id"]))
        assert task is not None and task.state == STATE_RUNNING, "取消是协作的，得等它自己回来"

        release.set()
        assert registry.wait(str(started["task_id"]), 5.0)
        finished = registry.get(str(started["task_id"]))
        assert finished is not None and finished.state == STATE_CANCELLED
        chat.stop()

    def test_summoning_her_mid_answer_still_gives_the_card_a_stop(self, tmp_path: Any) -> None:
        """她是被叫出来的：在她出现之前就开始的那一轮，也得能从她桌上停掉。"""
        from jarvis.app.turns import TurnRegistry
        from jarvis.ui.state_bridge import StateBridge

        chat, inside, release = self._slow_chat(tmp_path)
        bridge = _bridge(tmp_path, chat=chat, state=StateBridge(), turns=TurnRegistry())
        try:
            started = bridge.chat_send("慢慢想")
            assert inside.wait(5.0)

            pet = self._CardPet()
            bridge.attach_pet(cast(Any, pet))

            assert pet.actions[-1] == (BUBBLE_STOP, BUBBLE_ADD), pet.actions
            release.set()
            assert bridge._turns is not None and bridge._turns.wait(str(started["task_id"]), 5.0)
            assert bridge.pet_action(BUBBLE_STOP)["ok"] is False, "答完了再按就该说没得停"
        finally:
            chat.stop()

    def test_stop_with_nothing_running_says_so(self, tmp_path: Any) -> None:
        from jarvis.app.turns import TurnRegistry
        from jarvis.ui.state_bridge import StateBridge

        bridge = _bridge(tmp_path, state=StateBridge(), turns=TurnRegistry())
        answer = bridge.pet_action(BUBBLE_STOP)
        assert answer["ok"] is False
        assert str(answer["error"]), "按了没反应又不说为什么，用户只会再按一次"

    def test_add_brings_the_panel_forward_with_the_cursor_in_the_box(self, tmp_path: Any) -> None:
        """宠物那一页的渲染窗是停在屏幕外的：在那儿画输入框，是一个没人看得见的框。"""
        from jarvis.ui.lifecycle import ShellLifecycle
        from tests.test_ui_lifecycle import FakeTray, FakeWindow

        hud = FakeWindow()
        lifecycle = ShellLifecycle(FakeWindow(), tray=FakeTray())
        lifecycle.attach_window(hud)
        bridge = _bridge(tmp_path, lifecycle=lifecycle)
        bridge.attach_window(cast(Any, hud))
        hud.calls.clear()

        answer = bridge.pet_action(BUBBLE_ADD)

        assert answer["ok"] is True
        assert "show" in hud.calls, "面板还藏着，光标放进去也没人看得见"
        assert any("__jarvisFocusInput" in script for script in hud.scripts), hud.scripts

    def test_an_icon_nobody_recognises_is_refused_loudly(self, tmp_path: Any) -> None:
        bridge = _bridge(tmp_path)
        answer = bridge.pet_action("自爆")
        assert answer["ok"] is False and "自爆" in str(answer["error"])


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


class TestTheTypingSwitchOnTheBridge:
    """打字的开关要到得了页面，才叫"用户能打开它"。

    用户就是这么撞上的：档位开到最高，打字仍被拒，而界面上找不到任何开关 —— 因为旧规则
    要一个模型永远传不了的 `confirmed`。这条钉住新钥匙的桥面往返。
    """

    def _access(self, tmp_path: Any) -> Any:
        from jarvis.app.computer_access import ComputerAccess
        from jarvis.config.schema import ComputerSection

        base = ComputerSection(
            enabled=False,
            dry_run=True,
            allow_mouse=False,
            allow_keyboard=False,
            allow_typing=False,
            confirm_dangerous=True,
        )
        return ComputerAccess(Preferences(tmp_path / "prefs.json"), lambda: base)

    def test_the_switch_round_trips_and_shows_up_in_the_payload(self, tmp_path: Any) -> None:
        bridge = _bridge(tmp_path, computer_access=self._access(tmp_path))

        assert bridge.computer_levels()["current"]["allow_typing"] is False  # type: ignore[index]

        moved = bridge.computer_set_typing(True)

        assert moved["error"] == ""
        assert moved["current"]["allow_typing"] is True  # type: ignore[index]
        assert bridge.computer_levels()["current"]["allow_typing"] is True  # type: ignore[index]

    def test_a_non_boolean_is_refused_and_changes_nothing(self, tmp_path: Any) -> None:
        bridge = _bridge(tmp_path, computer_access=self._access(tmp_path))

        refused = bridge.computer_set_typing("yes")

        assert "只认真实/假" in str(refused["error"]) or "true/false" in str(refused["error"])
        assert bridge.computer_levels()["current"]["allow_typing"] is False  # type: ignore[index]

    def test_without_a_control_service_the_switch_says_so(self, tmp_path: Any) -> None:
        bridge = _bridge(tmp_path)

        assert "不可用" in str(bridge.computer_set_typing(True)["error"])


class TestTheSkinBridge:
    """皮肤要送到另一扇窗，靠的是这个口子。

    仪表盘自己换色是即时的；宠物是**另一页**，它有自己的一份图和材质。不推这一次，
    屏幕上就是"琥珀色面板 + 蓝色小人"，看起来像 bug 而不像主题。
    """

    def test_the_id_goes_through_to_a_live_pet(self, tmp_path: Any) -> None:
        class _Pet:
            def __init__(self) -> None:
                self.skins: list[str] = []

            def apply_skin(self, skin_id: str) -> bool:
                self.skins.append(skin_id)
                return True

        pet = _Pet()
        bridge = _bridge(tmp_path)
        bridge._pet = cast(Any, pet)

        answer = bridge.skin_apply("amber")

        assert answer == {"ok": True, "skin": "amber", "pet": True, "error": ""}
        assert pet.skins == ["amber"]

    def test_no_pet_is_not_an_error(self, tmp_path: Any) -> None:
        """宠物没开的时候她下次出场就是新颜色 —— 这不是失败，但要说清楚。"""
        answer = _bridge(tmp_path).skin_apply("green")

        assert answer["ok"] is True and answer["pet"] is False

    def test_an_id_that_is_not_shaped_like_one_is_refused_before_the_page(
        self, tmp_path: Any
    ) -> None:
        class _Pet:
            def apply_skin(self, skin_id: str) -> bool:
                raise AssertionError("非法 id 不该走到页面")

        bridge = _bridge(tmp_path)
        bridge._pet = _Pet()  # type: ignore[assignment]

        for bad in ("", "  ", "Blue", "a b", "x" * 40, 'x"; alert(1)//'):
            answer = bridge.skin_apply(bad)
            assert answer["ok"] is False and "不合法" in str(answer["error"]), bad


class FakeScheduler:
    """A scheduler that answers the five questions the automation tab asks."""

    def __init__(self, jobs: list[Any] | None = None) -> None:
        self.jobs = list(jobs or [])
        self.toggled: list[tuple[str, bool]] = []
        self.ran: list[str] = []
        self.removed: list[str] = []

    def stats(self) -> dict[str, object]:
        return {
            "name": "scheduler",
            "running": True,
            "jobs": len(self.jobs),
            "enabled_jobs": sum(1 for job in self.jobs if job.enabled),
            "runs": 3,
            "failures": 0,
        }

    def list_jobs(self) -> list[Any]:
        return list(self.jobs)

    def next_run_time(self, job_id: str) -> str:
        return "2026-10-03T10:00:00"

    def history(self, job_id: str | None = None, *, limit: int = 20) -> list[Any]:
        return []

    def set_enabled(self, job_id: str, enabled: bool) -> bool:
        self.toggled.append((job_id, enabled))
        return any(job.job_id == job_id for job in self.jobs)

    def remove_job(self, job_id: str) -> bool:
        self.removed.append(job_id)
        return True

    def run_now(self, job_id: str) -> Any:
        self.ran.append(job_id)
        return JobRun(
            run_id=1,
            job_id=job_id,
            started_at="2026-10-03T09:00:00",
            finished_at="2026-10-03T09:00:01",
            ok=True,
            detail="已开口，已弹托盘",
            error="",
        )


class FakeWorkflow:
    """A workflow engine with one definition and one recorded run."""

    def __init__(self) -> None:
        self.ran: list[str] = []
        self.reloads = 0

    def stats(self) -> dict[str, object]:
        return {
            "name": "workflow",
            "running": True,
            "definitions": 1,
            "scheduled": 1,
            "runs": 2,
            "failures": 1,
        }

    def definitions(self) -> list[Any]:
        return [
            WorkflowDef(
                name="每日早报",
                description="早上汇总磁盘与系统状态",
                trigger=WorkflowTriggerKind.CRON,
                schedule="0 9 * * *",
                steps=(),
                path="workflows/daily.yaml",
            )
        ]

    def history(self, name: str | None = None, *, limit: int = 20) -> list[Any]:
        return []

    def run(self, name: str) -> Any:
        self.ran.append(name)
        return WorkflowRun(
            run_id=1,
            workflow=name,
            started_at="2026-10-03T09:00:00",
            finished_at="2026-10-03T09:00:02",
            ok=True,
            steps=(),
            error="",
        )

    def reload(self) -> list[Any]:
        self.reloads += 1
        return self.definitions()


class FakePlanner:
    """A planner that either plans or refuses, the way the real one does."""

    def __init__(self, *, enabled: bool = True, refusal: str = "") -> None:
        self._enabled = enabled
        self._refusal = refusal
        self.goals: list[str] = []

    @property
    def enabled(self) -> bool:
        return self._enabled

    def stats(self) -> dict[str, object]:
        return {"name": "planner", "running": True}

    def plan(self, goal: str) -> Any:
        self.goals.append(goal)
        if self._refusal:
            raise PlannerError(self._refusal)
        return Plan(
            goal=goal,
            steps=(PlanStep(step_id="s1", title="先看一眼磁盘", action="disk_scan"),),
        )


def _job(job_id: str, *, trigger: SchedulerTriggerKind) -> SchedulerJobSpec:
    return SchedulerJobSpec(
        job_id=job_id,
        name=job_id.split(":")[-1],
        action="speak",
        arguments={},
        trigger=trigger,
        expression="0 9 * * *",
        enabled=True,
    )


def _block(payload: dict[str, object], key: str) -> dict[str, Any]:
    """One nested object out of a bridge answer, typed so it can be indexed.

    The bridge returns ``dict[str, object]`` by design -- every method is a JSON
    boundary -- so a nested read needs a cast. Doing it here keeps the assertions
    readable and ``mypy --strict`` honest at the same time.
    """
    return cast("dict[str, Any]", payload[key])


def _rows(payload: dict[str, object], key: str) -> list[dict[str, Any]]:
    """One nested list of rows out of a bridge answer."""
    return cast("list[dict[str, Any]]", payload[key])


class TestAutomationSurface:
    """The three engines whose ``stats()`` said "for the HUD's automation panel".

    That panel did not exist, so ``SchedulerService.stats`` and
    ``WorkflowService.stats`` have been running with counters nobody read. These
    tests are about the door, not about the engines -- none of the methods below may
    do anything the command line could not already do.
    """

    def test_the_overview_reports_all_three(self, tmp_path: Any) -> None:
        bridge = _bridge(
            tmp_path,
            scheduler=FakeScheduler(),
            workflow=FakeWorkflow(),
            planner=FakePlanner(),
        )
        answer = bridge.automation_overview()
        assert _block(answer, "scheduler")["jobs"] == 0
        assert _block(answer, "workflow")["definitions"] == 1
        assert answer["planner_enabled"] is True
        assert answer["error"] == ""

    def test_a_missing_service_reads_as_empty_not_as_a_failure(self, tmp_path: Any) -> None:
        """The tab opens onto three counters; one being absent is information.

        ``{}`` rather than an error, because "this process has no planner" is a fact
        about the build and not a malfunction -- and the panel still has to open.
        """
        answer = _bridge(tmp_path).automation_overview()
        assert answer["scheduler"] == {}
        assert answer["workflow"] == {}
        assert answer["planner"] == {}
        assert answer["planner_enabled"] is False
        assert answer["error"] == ""

    def test_scheduled_jobs_hide_the_reminder_namespace(self, tmp_path: Any) -> None:
        """Reminders have their own tab; showing them twice invites disagreement."""
        jobs = [
            _job("workflow:早报", trigger=SchedulerTriggerKind.CRON),
            _job("reminder:喝水", trigger=SchedulerTriggerKind.DATE),
        ]
        bridge = _bridge(tmp_path, scheduler=FakeScheduler(jobs))
        rows = _rows(bridge.scheduled_jobs(), "rows")
        assert [row["job_id"] for row in rows] == ["workflow:早报"]
        assert rows[0]["next_run"] == "2026-10-03T10:00:00"
        assert rows[0]["last"] is None

    def test_every_write_refuses_a_reminder(self, tmp_path: Any) -> None:
        """Two doors onto one row is two chances to disagree about whether it is on."""
        fake = FakeScheduler()
        bridge = _bridge(tmp_path, scheduler=fake)
        for answer in (
            bridge.job_toggle("reminder:喝水", False),
            bridge.job_run("reminder:喝水"),
            bridge.job_remove("reminder:喝水"),
        ):
            assert answer["ok"] is False
            assert "提醒" in str(answer["error"])
        assert fake.toggled == []
        assert fake.ran == []
        assert fake.removed == []

    def test_toggling_reaches_the_service(self, tmp_path: Any) -> None:
        fake = FakeScheduler([_job("workflow:早报", trigger=SchedulerTriggerKind.CRON)])
        bridge = _bridge(tmp_path, scheduler=fake)
        assert bridge.job_toggle("workflow:早报", False)["ok"] is True
        assert fake.toggled == [("workflow:早报", False)]

    def test_running_a_job_by_hand_reports_the_run(self, tmp_path: Any) -> None:
        """ "试一下" is how an operator finds out whether a job works at all."""
        fake = FakeScheduler([_job("workflow:早报", trigger=SchedulerTriggerKind.CRON)])
        bridge = _bridge(tmp_path, scheduler=fake)
        answer = bridge.job_run("workflow:早报")
        assert answer["ok"] is True
        assert _block(answer, "run")["detail"] == "已开口，已弹托盘"
        assert fake.ran == ["workflow:早报"]

    def test_removing_a_job_reports_whether_a_row_went(self, tmp_path: Any) -> None:
        fake = FakeScheduler()
        bridge = _bridge(tmp_path, scheduler=fake)
        answer = bridge.job_remove("workflow:早报")
        assert answer["ok"] is True
        assert answer["removed"] is True

    def test_toggling_an_unknown_job_says_so(self, tmp_path: Any) -> None:
        bridge = _bridge(tmp_path, scheduler=FakeScheduler())
        answer = bridge.job_toggle("workflow:没有这个", True)
        assert answer["ok"] is False
        assert answer["error"] == "没有这个任务"

    def test_a_scheduler_that_raises_becomes_an_error_string(self, tmp_path: Any) -> None:
        """A js_api call that raises reaches the page as an opaque pywebview error."""

        class Exploding(FakeScheduler):
            def set_enabled(self, job_id: str, enabled: bool) -> bool:
                raise RuntimeError("调度器炸了")

        bridge = _bridge(tmp_path, scheduler=Exploding())
        answer = bridge.job_toggle("workflow:早报", True)
        assert answer["ok"] is False
        assert "调度器炸了" in str(answer["error"])

    def test_workflow_definitions_carry_the_definition_and_the_runs(self, tmp_path: Any) -> None:
        bridge = _bridge(tmp_path, workflow=FakeWorkflow())
        answer = bridge.workflow_definitions()
        rows = _rows(answer, "rows")
        assert rows[0]["name"] == "每日早报"
        assert rows[0]["trigger"] == "cron"
        assert rows[0]["path"] == "workflows/daily.yaml"
        assert answer["runs"] == []
        assert answer["error"] == ""

    def test_running_a_workflow_reports_the_run(self, tmp_path: Any) -> None:
        fake = FakeWorkflow()
        bridge = _bridge(tmp_path, workflow=fake)
        answer = bridge.workflow_run("每日早报")
        assert answer["ok"] is True
        assert _block(answer, "run")["workflow"] == "每日早报"
        assert fake.ran == ["每日早报"]

    def test_reloading_counts_the_definitions(self, tmp_path: Any) -> None:
        """A new YAML must not need a restart, which is what reload is for."""
        fake = FakeWorkflow()
        bridge = _bridge(tmp_path, workflow=fake)
        answer = bridge.workflow_reload()
        assert answer["ok"] is True
        assert answer["count"] == 1
        assert fake.reloads == 1

    def test_planning_returns_the_plan_and_nothing_else(self, tmp_path: Any) -> None:
        """The plan is the whole answer: no step is executed and no tool is called."""
        fake = FakePlanner()
        bridge = _bridge(tmp_path, planner=fake)
        answer = bridge.plan_goal("把 C 盘清出 10G")
        assert answer["ok"] is True
        plan = _block(answer, "plan")
        assert plan["goal"] == "把 C 盘清出 10G"
        steps = cast("list[dict[str, Any]]", plan["steps"])
        assert steps[0]["title"] == "先看一眼磁盘"
        assert steps[0]["status"] == "pending"
        assert fake.goals == ["把 C 盘清出 10G"]

    def test_a_refused_plan_comes_back_as_an_answer(self, tmp_path: Any) -> None:
        """No key, blank goal, unparsable model output: all normal, none a crash."""
        bridge = _bridge(tmp_path, planner=FakePlanner(refusal="没有配置大模型"))
        answer = bridge.plan_goal("随便")
        assert answer["ok"] is False
        assert answer["plan"] is None
        assert "没有配置大模型" in str(answer["error"])

    def test_the_planner_says_when_it_is_disabled(self, tmp_path: Any) -> None:
        """``planner.enabled: false`` is a config decision, and the tab shows it."""
        bridge = _bridge(tmp_path, planner=FakePlanner(enabled=False))
        assert bridge.automation_overview()["planner_enabled"] is False


class TestTheModelPickersOnTheBridge:
    """The chat header's two dropdowns plus the two knobs, as the page calls them.

    Every one of these runs on pywebview's JS-API thread, so the contract is the
    same as everywhere else in this file: answer with a dict, never raise, and say
    *why* a pick was refused rather than silently keeping the old value.
    """

    def _real(self, tmp_path: Any) -> tuple[HudBridge, Preferences, dict[str, str]]:
        from jarvis.app.settings_service import SettingsService
        from jarvis.config.schema import (
            LlmSection,
            ModelSpec,
            ProviderSection,
        )

        def section() -> LlmSection:
            return LlmSection(
                default_provider="alpha",
                timeout_seconds=30.0,
                max_retries=1,
                retry_backoff_seconds=0.0,
                providers={
                    "alpha": ProviderSection(
                        name="alpha",
                        base_url="https://alpha.example/v1",
                        models=(ModelSpec(id="alpha-1"), ModelSpec(id="alpha-small")),
                        default_model="alpha-1",
                        api_key_env="ALPHA_API_KEY",
                        cost_input_per_1m=0.0,
                        cost_output_per_1m=0.0,
                    ),
                    "beta": ProviderSection(
                        name="beta",
                        base_url="https://beta.example/v1",
                        models=(ModelSpec(id="beta-1"),),
                        default_model="beta-1",
                        api_key_env="BETA_API_KEY",
                        cost_input_per_1m=0.0,
                        cost_output_per_1m=0.0,
                    ),
                },
            )

        prefs = Preferences(tmp_path / "preferences.json")
        env: dict[str, str] = {}
        settings = SettingsService(
            prefs,
            cast(Any, _RecordingLlm()),
            section,
            environ=env,
            persist_env=lambda *_: True,
        )
        settings.start()
        return _bridge(tmp_path, settings=settings), prefs, env

    def test_a_process_without_settings_says_so_instead_of_raising(self, tmp_path: Any) -> None:
        bridge = _bridge(tmp_path)

        assert bridge.chat_models()["error"]
        assert bridge.chat_pick("alpha")["error"]
        assert bridge.chat_tuning(thinking="high")["ok"] is False
        assert bridge.llm_add_model("alpha", "m-1")["ok"] is False
        assert bridge.llm_remove_model("alpha", "m-1")["ok"] is False

    def test_the_listing_is_shaped_as_two_levels(self, tmp_path: Any) -> None:
        bridge, _, _ = self._real(tmp_path)

        listing = bridge.chat_models()

        assert listing["error"] == ""
        rows = cast("list[dict[str, Any]]", listing["providers"])
        assert [entry["name"] for entry in rows] == ["alpha", "beta"]
        assert listing["provider"] == "alpha"
        assert listing["model"] == "alpha-1"

    def test_picking_a_provider_alone_lands_on_its_default(self, tmp_path: Any) -> None:
        bridge, _, _ = self._real(tmp_path)

        listing = bridge.chat_pick("beta")

        assert listing["provider"] == "beta"
        assert listing["model"] == "beta-1"

    def test_picking_a_model_inside_a_provider_works(self, tmp_path: Any) -> None:
        bridge, prefs, _ = self._real(tmp_path)

        listing = bridge.chat_pick("alpha", "alpha-small")

        assert listing["model"] == "alpha-small"
        assert prefs.text("llm.model") == "alpha-small"

    def test_an_unknown_provider_comes_back_as_an_error_not_a_crash(self, tmp_path: Any) -> None:
        bridge, _, _ = self._real(tmp_path)

        listing = bridge.chat_pick("ghost")

        assert listing["error"], "the page has to be able to show why it did not switch"

    def test_the_two_knobs_answer_with_the_pair_they_stored(self, tmp_path: Any) -> None:
        bridge, _, _ = self._real(tmp_path)

        assert bridge.chat_tuning(thinking="high", turns=20) == {
            "ok": True,
            "error": "",
            "thinking": "high",
            "turns": 20,
        }

    def test_a_bad_knob_value_is_refused_with_a_reason(self, tmp_path: Any) -> None:
        bridge, _, _ = self._real(tmp_path)

        answer = bridge.chat_tuning(thinking="galaxy-brain")

        assert answer["ok"] is False
        assert answer["error"]

    def test_adding_and_removing_a_model_round_trips_through_the_bridge(
        self, tmp_path: Any
    ) -> None:
        bridge, _, _ = self._real(tmp_path)

        added = bridge.llm_add_model("alpha", "alpha-turbo", "Alpha Turbo")
        assert added["ok"] is True
        assert [spec["id"] for spec in cast("list[dict[str, Any]]", added["models"])] == [
            "alpha-1",
            "alpha-small",
            "alpha-turbo",
        ]

        removed = bridge.llm_remove_model("alpha", "alpha-turbo")
        assert removed["ok"] is True
        listing = bridge.chat_pick("alpha", "alpha-small")
        rows = cast("list[dict[str, Any]]", listing["providers"])
        assert "alpha-turbo" not in [
            spec["id"] for entry in rows for spec in cast("list[dict[str, Any]]", entry["models"])
        ]

    def test_the_chat_service_is_actually_rebuilt_on_a_pick(self, tmp_path: Any) -> None:
        """The point of the dropdown: the next question goes to the new model with
        no restart. A pick that only wrote a preference would look identical here
        unless the client factory is checked too."""
        from jarvis.app.settings_service import SettingsService
        from jarvis.config.schema import LlmSection, ModelSpec, ProviderSection

        def section() -> LlmSection:
            return LlmSection(
                default_provider="alpha",
                timeout_seconds=30.0,
                max_retries=1,
                retry_backoff_seconds=0.0,
                providers={
                    "alpha": ProviderSection(
                        name="alpha",
                        base_url="https://alpha.example/v1",
                        models=(ModelSpec(id="alpha-1"),),
                        default_model="alpha-1",
                        api_key_env="ALPHA_API_KEY",
                        cost_input_per_1m=0.0,
                        cost_output_per_1m=0.0,
                    ),
                    "beta": ProviderSection(
                        name="beta",
                        base_url="https://beta.example/v1",
                        models=(ModelSpec(id="beta-1"),),
                        default_model="beta-1",
                        api_key_env="BETA_API_KEY",
                        cost_input_per_1m=0.0,
                        cost_output_per_1m=0.0,
                    ),
                },
            )

        llm = _RecordingLlm()
        settings = SettingsService(
            Preferences(tmp_path / "preferences.json"),
            cast(Any, llm),
            section,
            environ={},
            persist_env=lambda *_: True,
        )
        bridge = _bridge(tmp_path, settings=settings)

        bridge.chat_pick("beta", "beta-1")

        assert llm.section is not None, "the client cache must be invalidated on a pick"
        assert llm.section.default_provider == "beta"
        assert llm.section.providers["beta"].model == "beta-1"


class _RecordingLlm:
    """Minimal stand-in for ``LlmService``: remembers the override it was handed."""

    def __init__(self) -> None:
        self.section: Any = None

    def set_section_override(self, section: Any) -> None:
        self.section = section


def _recorder(seen: list[str]) -> Any:
    """A stand-in for ``cloud.delete`` that records and reports success.

    A named function rather than ``lambda voice: seen.append(voice) or True``:
    under ``mypy --strict`` the ``list.append`` in an expression is an error,
    and the fix is not a type ignore but an honest little function.
    """

    def fake(voice: str, **kwargs: Any) -> bool:
        seen.append(voice)
        return True

    return fake


def _enroller(seen: list[str]) -> Any:
    """A stand-in for ``cloud.enrol`` that records the call."""

    def fake(**kwargs: Any) -> Any:
        from jarvis.tts.cloud import EnrolledVoice

        seen.append("called")
        return EnrolledVoice(voice="v1", target_model="m")

    return fake


class TestCloudVoiceUpload:
    """``voice_clone_add`` with ``upload=true``: the录制-to-云端 path.

    What is checked here is the *policy*, not the HTTPS: that an upload happens
    only when asked for, that a recording survives a failed upload, and that
    deleting a voice also deletes its cloud half. The vendor call itself is
    stubbed -- it needs a key and a network, and neither belongs in a unit test.
    """

    @staticmethod
    def _library(tmp_path: Any) -> Any:
        from jarvis.app.voice_library import VoiceLibrary

        library = VoiceLibrary(tmp_path / "voices")
        library.start()
        return library

    @staticmethod
    def _picker(tmp_path: Any, library: Any) -> VoicePicker:
        """A real picker around the library.

        ``voice_clone_list`` reads the picker rather than the library, so a
        bridge built without one answers 「语音选择不可用」 -- which is a real
        state (a process with no page), just not the one under test.
        """
        from jarvis.config.loader import load_defaults

        raw = dict(cast("Mapping[str, object]", load_defaults()["tts"]))
        return VoicePicker(
            lambda: TtsSection.from_mapping(raw),
            Preferences(tmp_path / "preferences.json"),
            library=library,
        )

    @staticmethod
    def _pcm() -> bytes:
        return b"\x00\x00" * 16_000 * 4  # four seconds: inside the window

    def test_a_recording_without_upload_stays_local(self, tmp_path: Any) -> None:
        """The default must be private: uploading is a disclosure, so it takes
        an explicit ``true``."""
        library = self._library(tmp_path)
        bridge = _bridge(
            tmp_path,
            voice_library=library,
            voice_picker=self._picker(tmp_path, library),
        )
        reply = bridge.voice_clone_add(
            name="我的声音",
            pcm=base64.b64encode(self._pcm()).decode(),
            sample_rate=16_000,
            prompt_text="你好，我是小夜。",
        )
        assert reply["error"] == ""
        voices = cast("list[dict[str, Any]]", reply["voices"])
        assert len(voices) == 1
        assert voices[0]["cloud"] is False
        assert library.voices()[0].cloud_voice == ""

    def test_an_upload_links_the_voice_to_the_vendor_id(
        self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The whole point of uploading: the picker must be able to tell that
        this voice answers fast, which it learns from ``cloud``."""
        from jarvis.tts import cloud

        library = self._library(tmp_path)
        bridge = _bridge(
            tmp_path,
            voice_library=library,
            voice_picker=self._picker(tmp_path, library),
        )
        monkeypatch.setattr(
            cloud,
            "enrol",
            lambda **kwargs: cloud.EnrolledVoice(voice="xyvoiceSelf01", target_model="m"),
        )
        monkeypatch.setenv("DASHSCOPE_API_KEY", "k")
        reply = bridge.voice_clone_add(
            name="我的声音",
            pcm=base64.b64encode(self._pcm()).decode(),
            sample_rate=16_000,
            prompt_text="你好，我是小夜。",
            upload=True,
        )
        assert reply["error"] == ""
        voices = cast("list[dict[str, Any]]", reply["voices"])
        assert voices[0]["cloud"] is True
        assert voices[0]["cloud_model"] == "m"
        assert library.voices()[0].cloud_voice == "xyvoiceSelf01"

    def test_a_failed_upload_keeps_the_recording_and_says_why(
        self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A recording is the user's and is kept even when the vendor rejects
        it: the upload degrades to a working local voice plus a message, never
        to a lost recording."""
        from jarvis.tts import cloud

        library = self._library(tmp_path)
        bridge = _bridge(
            tmp_path,
            voice_library=library,
            voice_picker=self._picker(tmp_path, library),
        )

        def boom(**kwargs: Any) -> Any:
            raise cloud.CloudVoiceError("没有配置阿里云百炼的 API Key")

        monkeypatch.setattr(cloud, "enrol", boom)
        reply = bridge.voice_clone_add(
            name="我的声音",
            pcm=base64.b64encode(self._pcm()).decode(),
            sample_rate=16_000,
            prompt_text="你好，我是小夜。",
            upload=True,
        )
        assert reply["error"] == "", "the recording itself succeeded"
        assert "API Key" in str(reply["cloud_error"])
        assert len(library.voices()) == 1
        assert library.voices()[0].cloud_voice == ""

    def test_the_upload_string_form_is_accepted(
        self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A form field or query string sends ``"true"``; accepting only the
        bool would make the switch silently do nothing on whichever client sent
        a string."""
        from jarvis.tts import cloud

        library = self._library(tmp_path)
        bridge = _bridge(
            tmp_path,
            voice_library=library,
            voice_picker=self._picker(tmp_path, library),
        )
        calls: list[str] = []
        monkeypatch.setattr(cloud, "enrol", _enroller(calls))
        monkeypatch.setenv("DASHSCOPE_API_KEY", "k")
        bridge.voice_clone_add(
            name="我的声音",
            pcm=base64.b64encode(self._pcm()).decode(),
            sample_rate=16_000,
            prompt_text="你好。",
            upload="true",
        )
        assert calls == ["called"]

    def test_removing_an_uploaded_voice_deletes_its_cloud_half(
        self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Leaving a clone of somebody's voice alive after they asked for the
        recording to be removed is not a state this feature may end in."""
        from jarvis.tts import cloud

        library = self._library(tmp_path)
        stored = library.add(
            name="我的声音",
            pcm=self._pcm(),
            sample_rate=16_000,
            prompt_text="你好。",
            cloud_voice="xyvoiceSelf01",
            cloud_model="m",
        )
        deleted: list[str] = []
        monkeypatch.setattr(cloud, "delete", _recorder(deleted))
        bridge = _bridge(
            tmp_path,
            voice_library=library,
            voice_picker=self._picker(tmp_path, library),
        )

        reply = bridge.voice_clone_remove(voice_id=stored.voice_id)

        assert reply["error"] == ""
        assert deleted == ["xyvoiceSelf01"]
        assert library.voices() == []

    def test_removing_a_local_voice_touches_no_network(
        self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A local-only voice has no cloud half, so the vendor must not be
        called at all -- a delete request for a voice that was never uploaded
        is a request nobody can explain later."""
        from jarvis.tts import cloud

        library = self._library(tmp_path)
        stored = library.add(
            name="我的声音", pcm=self._pcm(), sample_rate=16_000, prompt_text="你好。"
        )
        calls: list[str] = []
        monkeypatch.setattr(cloud, "delete", _recorder(calls))
        bridge = _bridge(
            tmp_path,
            voice_library=library,
            voice_picker=self._picker(tmp_path, library),
        )

        bridge.voice_clone_remove(voice_id=stored.voice_id)

        assert calls == []


class TestUsageSurface:
    """The 用量 panel's whole data path, which had no bridge test before 2026-10-04.

    The service is real over a real SQLite file, so these also check the two things the
    screen cannot recover without them: that the per-model rows add up to the totals they
    sit under, and that the all-time reading says so instead of looking like a zero window.
    """

    @staticmethod
    def _usage(tmp_path: Any) -> Any:
        from datetime import timedelta

        from jarvis.app.usage_service import UsageService
        from jarvis.core.events import UsageEvent
        from jarvis.database import SqliteStore, format_timestamp, utc_now

        store = SqliteStore(tmp_path / "usage.db")
        store.start()
        usage = UsageService(store)
        usage.start()
        usage.record(
            UsageEvent(
                provider="deepseek",
                model="deepseek-chat",
                prompt_tokens=1000,
                completion_tokens=200,
                cached_tokens=400,
                latency_ms=700.0,
            )
        )
        usage.record(
            UsageEvent(
                provider="qwen",
                model="qwen-max",
                prompt_tokens=500,
                completion_tokens=90,
                cached_tokens=None,
                latency_ms=1400.0,
            )
        )
        # One row outside any week, so "the window is really filtering" is checked rather
        # than assumed -- an open-ended WHERE would pass a totals-only test.
        usage._repo.insert_event(
            format_timestamp(utc_now() - timedelta(days=90)),
            UsageEvent(
                provider="openai",
                model="gpt-4o",
                prompt_tokens=9_000,
                completion_tokens=900,
                cached_tokens=None,
                latency_ms=2100.0,
            ),
        )
        return usage

    def test_the_slices_add_up_to_the_total_it_sits_under(self, tmp_path: Any) -> None:
        bridge = _bridge(tmp_path, usage=self._usage(tmp_path))
        report = bridge.usage_summary(7)

        assert report["error"] == ""
        summary = report["summary"]
        rows = report["models"]
        assert isinstance(summary, dict) and isinstance(rows, list) and len(rows) == 2
        assert sum(int(row["total_tokens"]) for row in rows) == summary["total_tokens"]
        assert sum(int(row["calls"]) for row in rows) == summary["calls"]
        assert report["daily"], "一周的窗口该有每日柱子"

    def test_the_all_time_reading_replaces_the_window_without_losing_a_number(
        self, tmp_path: Any
    ) -> None:
        from jarvis.app.usage_service import ALL_TIME

        bridge = _bridge(tmp_path, usage=self._usage(tmp_path))
        report = bridge.usage_summary(ALL_TIME)
        summary = report["summary"]
        assert isinstance(summary, dict)

        assert summary["all_time"] is True and summary["days"] == ALL_TIME
        assert summary["calls"] == 3
        assert not report["daily"], "几百根柱子不是图：全部时间里这一段故意留空"
        rows = report["models"]
        assert isinstance(rows, list) and len(rows) == 3
        assert sum(int(row["total_tokens"]) for row in rows) == summary["total_tokens"]

        span = report["span"]
        assert isinstance(span, dict) and span["rows"] == 3
        assert str(span["first_at"]) <= str(summary["since"]), "标题的起点就是账本最早那条"

    def test_a_nonsense_window_falls_back_rather_than_failing_the_panel(
        self, tmp_path: Any
    ) -> None:
        bridge = _bridge(tmp_path, usage=self._usage(tmp_path))
        report = bridge.usage_summary("一周")  # type: ignore[arg-type]
        summary = report["summary"]
        assert isinstance(summary, dict) and summary["days"] == 7
        assert summary["all_time"] is False

    def test_a_build_without_the_ledger_says_so(self, tmp_path: Any) -> None:
        bridge = _bridge(tmp_path)
        report = bridge.usage_summary(7)
        assert "用量统计不可用" in str(report["error"])
        assert report["models"] == [] and report["daily"] == []


class TestTheThinkingIndicatorIsNeverInvisible:
    """「思考中」曾经一次都没出现过，这段就是钉住那个原因的。

    气泡的条件是当前标签的卡片 ``status === 'running'``。可卡片是 ``Conversation`` 的快照，
    而它是在**工作线程里**、`chat_send` 返回之后才把自己标成 running 的 —— 桥面那几次推送
    全都发生在那之前，于是整整二十秒里面板看到的都是 idle。修法是让任务表（它从请求被接受
    那一刻起就知道有一轮在跑）去盖卡片，所以这里的断言全部发生在答案回来**之前**。
    """

    def test_the_tab_says_running_while_the_model_is_still_thinking(self, tmp_path: Any) -> None:
        from jarvis.app.chat_service import ChatService
        from jarvis.app.turns import TurnRegistry
        from jarvis.ui.state_bridge import StateBridge

        inside = threading.Event()
        release = threading.Event()

        class Slow:
            provider_name = "slow"
            model = "slow-model"

            def complete(self, messages: Any, *, options: Any = None) -> Any:
                from jarvis.llm.types import ChatResponse

                inside.set()
                assert release.wait(5.0), "测试没放行就答完了，那这条测的是别的东西"
                return ChatResponse(content="答", model="slow-model")

            def stream(self, messages: Any, *, options: Any = None) -> Any:
                return iter(())

        state = StateBridge()
        chat = ChatService(lambda: Slow())
        chat.start()
        bridge = _bridge(tmp_path, chat=chat, state=state, turns=TurnRegistry())

        started = bridge.chat_send("慢慢想")
        assert started["ok"] is True
        assert inside.wait(5.0)

        card = next(
            row for row in state.snapshot().conversations if row.id == started["conversation"]
        )
        assert card.status == "running", "模型还在想，标签就已经该说在想了"
        assert card.phase == "思考中"
        assert card.task_id == started["task_id"]

        release.set()
        assert bridge._turns is not None and bridge._turns.wait(str(started["task_id"]), 5.0)
        done = next(
            row for row in state.snapshot().conversations if row.id == started["conversation"]
        )
        assert done.status != "running"

    def test_a_tool_phase_reaches_the_strip_instead_of_staying_internal(
        self, tmp_path: Any
    ) -> None:
        """服务里换了阶段，面板得跟着换 —— 只有推送时机对了才算"正在查东西"。"""
        from jarvis.app.chat_service import ChatService
        from jarvis.app.turns import TurnRegistry
        from jarvis.ui.state_bridge import StateBridge

        seen: list[str] = []

        class OneRound:
            provider_name = "tool"
            model = "m"

            def complete(self, messages: Any, *, options: Any = None) -> Any:
                from jarvis.llm.types import ChatResponse, ToolCall

                asked = bool(getattr(options, "tools", None)) and len(
                    [m for m in messages if str(m.role.value) == "tool"]
                )
                if not asked:
                    return ChatResponse(
                        content="",
                        model="m",
                        finish_reason="tool_calls",
                        tool_calls=(ToolCall(id="c1", name="lookup", arguments="{}"),),
                    )
                return ChatResponse(content="查完了", model="m")

            def stream(self, messages: Any, *, options: Any = None) -> Any:
                return iter(())

        class Tools:
            enabled = True

            def to_openai_tools(self) -> list[dict[str, object]]:
                return [{"type": "function", "function": {"name": "lookup"}}]

            def invoke(self, name: str, arguments: str) -> Any:
                from jarvis.tools.types import ToolResult

                return ToolResult(tool=name, ok=True, output="查到了")

        state = StateBridge()
        registry = TurnRegistry()
        chat = ChatService(lambda: OneRound(), tool_provider=lambda: cast(Any, Tools()))
        chat.start()
        bridge = _bridge(tmp_path, chat=chat, state=state, turns=registry)
        # Chained, not replaced: the bridge owns the one listener slot, and what is being
        # asserted here is exactly the announcement it makes.
        before = registry.on_update

        def watch(info: Any) -> None:
            seen.append(str(info.phase))
            if before is not None:
                before(info)

        registry.on_update = watch
        bridge.chat_send("查一下")
        assert registry.wait(_last_task(registry), 5.0)
        assert "思考中" in seen and "调用工具" in seen, seen

    def test_the_pet_is_told_she_is_thinking_and_when_that_stops(self, tmp_path: Any) -> None:
        """宠物那句「思考中」由任务表驱动，而不是由 ask 那条路驱动。

        区别在收尾：走 `_turn_changed` 的话，答案、取消、抛异常三种出口都会经过
        `TurnRegistry._finish` → announce，指示一定落回 False；挂在 ask 的返回上就会
        在异常那条路上留一个永远闪着的气泡。
        """
        from jarvis.app.chat_service import ChatService
        from jarvis.app.turns import TurnRegistry
        from jarvis.ui.state_bridge import StateBridge

        inside = threading.Event()
        release = threading.Event()

        class Slow:
            provider_name = "slow"
            model = "slow-model"

            def complete(self, messages: Any, *, options: Any = None) -> Any:
                from jarvis.llm.types import ChatResponse

                inside.set()
                assert release.wait(5.0), "测试没放行就答完了"
                return ChatResponse(content="答", model="slow-model")

            def stream(self, messages: Any, *, options: Any = None) -> Any:
                return iter(())

        class _RecordingPet:
            def __init__(self) -> None:
                self.calls: list[bool] = []

            def set_thinking(self, active: bool) -> None:
                self.calls.append(bool(active))

            def set_palette(self, palette: BubblePalette) -> None:
                return None

            def set_actions(self, actions: object) -> None:
                return None

        pet = _RecordingPet()
        chat = ChatService(lambda: Slow())
        chat.start()
        bridge = _bridge(tmp_path, chat=chat, state=StateBridge(), turns=TurnRegistry())
        bridge.attach_pet(cast(Any, pet))

        started = bridge.chat_send("慢慢想")
        assert inside.wait(5.0), "模型还在想的时候才轮得到断言"
        assert pet.calls and pet.calls[-1] is True, "任务一登记就该告诉她用户在等"

        release.set()
        assert bridge._turns is not None and bridge._turns.wait(str(started["task_id"]), 5.0)
        assert pet.calls[-1] is False, "答完了还亮着，用户看到的就叫「她卡住了」"

    def test_a_second_question_stacks_on_its_tab_and_never_blinks_the_card_off(
        self, tmp_path: Any
    ) -> None:
        """连着问两句：第二句要看得见地排在队里，交接那一下「思考中」不许灭。

        钉的是桥面而不是注册表。两句都在任务表里、都从 ``_turn_changed`` 出来，所以只有
        在这一层能看出问题：``live`` 要是按"在跑的"算而不是按"还欠着一个回答的"算，第一句
        答完、第二句刚起来的那个空档就会把宠物头上那张卡闪一下灭掉 —— 而面板上明明还列着
        第二句在等。
        """
        from jarvis.app.chat_service import ChatService
        from jarvis.app.turns import STATE_QUEUED, TurnRegistry
        from jarvis.ui.state_bridge import StateBridge

        inside = threading.Event()
        gates = [threading.Event(), threading.Event()]
        counter = iter(range(2))

        class Slow:
            provider_name = "slow"
            model = "slow-model"

            def complete(self, messages: Any, *, options: Any = None) -> Any:
                from jarvis.llm.types import ChatResponse

                index = next(counter)
                inside.set()
                assert gates[index].wait(5.0), "测试没放行就答完了"
                return ChatResponse(content="答", model="slow-model")

            def stream(self, messages: Any, *, options: Any = None) -> Any:
                return iter(())

        class _Pet:
            def __init__(self) -> None:
                self.calls: list[bool] = []

            def set_thinking(self, active: bool) -> None:
                self.calls.append(bool(active))

            def set_palette(self, palette: BubblePalette) -> None:
                return None

            def set_actions(self, actions: object) -> None:
                return None

        pet = _Pet()
        state = StateBridge()
        chat = ChatService(lambda: Slow())
        chat.start()
        registry = TurnRegistry()
        bridge = _bridge(tmp_path, chat=chat, state=state, turns=registry)
        bridge.attach_pet(cast(Any, pet))

        first = bridge.chat_send("第一问")
        assert inside.wait(5.0), "第一句还没进模型，断言就早了"
        second = bridge.chat_send("第二问")
        second_id = str(second["task_id"])
        waiting = registry.get(second_id)
        assert waiting is not None and waiting.state == STATE_QUEUED, "第二句没排队，是抢答"

        card = next(row for row in state.snapshot().conversations if row.active)
        assert [question.question for question in card.queued] == ["第二问"], "队没上到卡片上"
        assert card.status == "running"

        gates[0].set()
        assert registry.wait(str(first["task_id"]), 5.0), "第一句没答完"
        assert False not in pet.calls, f"交接那一下把「思考中」灭了：{pet.calls}"
        mid = next(row for row in state.snapshot().conversations if row.active)
        assert mid.queued == (), "第一句答完了，队里该只剩第二句在跑"

        gates[1].set()
        assert registry.wait(second_id, 5.0), "第二句没轮到"
        after = next(row for row in state.snapshot().conversations if row.active)
        assert after.queued == ()
        assert pet.calls[-1] is False, "两句都答完了，卡必须落回去"

    def test_a_turn_that_raises_still_puts_the_indicator_down(self, tmp_path: Any) -> None:
        """失败要看得见，但它不能把「思考中」留在桌上。"""
        from jarvis.app.chat_service import ChatService
        from jarvis.app.turns import TurnRegistry
        from jarvis.ui.state_bridge import StateBridge

        class Explodes:
            provider_name = "bad"
            model = "bad-model"

            def complete(self, messages: Any, *, options: Any = None) -> Any:
                raise RuntimeError("端点断了")

            def stream(self, messages: Any, *, options: Any = None) -> Any:
                return iter(())

        seen: list[bool] = []

        class _RecordingPet:
            def set_thinking(self, active: bool) -> None:
                seen.append(bool(active))

            def set_palette(self, palette: BubblePalette) -> None:
                return None

            def set_actions(self, actions: object) -> None:
                return None

        chat = ChatService(lambda: Explodes())
        chat.start()
        bridge = _bridge(tmp_path, chat=chat, state=StateBridge(), turns=TurnRegistry())
        bridge.attach_pet(cast(Any, _RecordingPet()))

        started = bridge.chat_send("问一句就炸")
        assert bridge._turns is not None and bridge._turns.wait(str(started["task_id"]), 5.0)
        assert seen and seen[-1] is False, seen


def _last_task(registry: Any) -> str:
    tasks = registry.recent(1)
    return str(tasks[0].task_id) if tasks else ""


class TestSurfaceSwitches:
    """设置一改，画它的那两处（宠物卡 / 语音核心的开关）要一起跟上。"""

    class _Settings:
        def __init__(self) -> None:
            self.loader = "dots"
            self.speaks = True

        def thinking_loader(self) -> str:
            return self.loader

        def speaks_typed(self) -> bool:
            return self.speaks

        def apply(self, patch: dict[str, object]) -> dict[str, object]:
            if "thinking_loader" in patch:
                self.loader = str(patch["thinking_loader"])
            if "auto_speak_typed" in patch:
                self.speaks = bool(patch["auto_speak_typed"])
            return {"problems": {}, "applied": dict(patch), "thinking_loader": self.loader}

    def test_applying_a_switch_pushes_it_to_both_surfaces(self, tmp_path: Any) -> None:
        from jarvis.ui.state_bridge import StateBridge

        class _Pet:
            def __init__(self) -> None:
                self.kinds: list[str] = []

            def set_thinking_loader(self, kind: str) -> None:
                self.kinds.append(kind)

            def set_palette(self, palette: BubblePalette) -> None:
                return None

            def set_actions(self, actions: object) -> None:
                return None

        settings = self._Settings()
        state = StateBridge()
        pet = _Pet()
        bridge = _bridge(tmp_path, settings=settings, state=state)
        bridge.attach_pet(cast(Any, pet))

        bridge.settings_apply({"thinking_loader": "matrix", "auto_speak_typed": False})

        assert pet.kinds == ["matrix"], "宠物那张卡没跟上类型"
        assert state.snapshot().thinking_loader == "matrix"
        assert state.snapshot().speaks_typed is False, "语音核心的开关没跟上朗读状态"

    def test_a_switch_that_was_never_applied_still_reaches_the_surfaces(
        self, tmp_path: Any
    ) -> None:
        """开机也得推一次：不然宠物卡用默认值、面板用文件里的值，一上来就不一致。"""
        from jarvis.ui.state_bridge import StateBridge

        settings = self._Settings()
        settings.loader = "ring"
        settings.speaks = False
        state = StateBridge()
        bridge = _bridge(tmp_path, settings=settings, state=state)

        bridge._sync_surface_switches()

        assert state.snapshot().thinking_loader == "ring"
        assert state.snapshot().speaks_typed is False


class TestTheStreamingBubbleIsAlwaysRetracted:
    """半截气泡不收，下一轮的「思考中」就永远出不来。

    面板只在"还没有任何字"的时候显示思考气泡；而 `_finish_turn` 只在答案落地那条路上
    清气泡 —— 抛异常的那一轮根本走不到它。于是上一轮留下的半截文本会一直挂在快照里，
    下一轮看起来就是"她没在想，也没在答"。
    """

    def test_a_turn_that_raises_retracts_the_partial_it_already_pushed(self, tmp_path: Any) -> None:
        from jarvis.app.chat_service import ChatService
        from jarvis.app.turns import TurnRegistry
        from jarvis.ui.state_bridge import StateBridge

        state = StateBridge()
        registry = TurnRegistry()
        chat = ChatService(lambda: cast(Any, None))
        chat.start()
        # The bridge is built for its wiring, not its return: attaching it is what
        # puts ``_turn_changed`` on the registry, and that handler is what is under test.
        _bridge(tmp_path, chat=chat, state=state, turns=registry)

        def half_then_boom(context: Any) -> dict[str, object]:
            state.set_stream("hud", str(context.task_id), "答到一半")
            assert state.snapshot().streaming is not None, "前提：气泡确实挂上了"
            raise RuntimeError("模型断了")

        info = registry.start(
            conversation_id="hud", kind="chat", question="问", work=half_then_boom
        )
        assert registry.wait(info.task_id, 5.0)

        assert state.snapshot().streaming is None, "异常出口必须自己把半截气泡收掉"


class _Machine:
    """One telemetry reading, small enough to hold in a hand, shaped like the real one.

    The alert engine reads it structurally -- the same way it reads
    ``jarvis.tools.monitor.SystemSnapshot`` -- so nothing here touches psutil.
    """

    def __init__(
        self,
        *,
        cpu: float = 10.0,
        memory: float = 20.0,
        swap: float = 0.0,
        free_gb: float = 500.0,
    ) -> None:
        self.set_reading(cpu=cpu, memory=memory, swap=swap, free_gb=free_gb)

    def set_reading(
        self,
        *,
        cpu: float = 10.0,
        memory: float = 20.0,
        swap: float = 0.0,
        free_gb: float = 500.0,
    ) -> None:
        """Move the whole machine at once. Untouched numbers return to their calm value,
        so a test cannot accidentally inherit last round's hot CPU."""
        self.cpu = SimpleNamespace(percent=cpu)
        self.memory = SimpleNamespace(percent=memory, swap_percent=swap)
        self.disks = [SimpleNamespace(mount="C:", free_bytes=int(free_gb * 1024**3))]
        self.warnings: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {"cpu": {"percent": self.cpu.percent}}

    def snapshot(self) -> _Machine:
        return self

    def close(self) -> None:
        pass


def _alert_harness(
    tmp_path: Any,
    *,
    sustain: float = 60.0,
    preferences: Preferences | None = None,
) -> tuple[HudBridge, AlertService, _Machine, list[float], list[Any]]:
    """A window with a real alert centre, a real SystemService and a clock we own.

    Everything in the chain is the shipped object. The only fakes are the machine being
    read and the clock -- a test that waits sixty seconds to prove a sixty-second window
    is a test that gets skipped.
    """
    machine = _Machine()

    def _monitor() -> Any:
        return machine

    now = [1_000.0]
    spoken: list[Any] = []
    alerts = AlertService(
        preferences,
        on_fire=spoken.append,
        clock=lambda: now[0],
        sustain_seconds=sustain,
    )
    system = SystemService(cast(Any, _monitor), alerts=alerts)
    system.start()
    bridge = _bridge(tmp_path, system=system, alerts=alerts)
    return bridge, alerts, machine, now, spoken


def _bake(
    bridge: HudBridge, machine: _Machine, now: list[float], **reading: float
) -> list[dict[str, Any]]:
    """Hold one reading steady across the sustained window; return what the HUD sees.

    Two polls, not one: a single sample only has to start the clock. The intermediate
    round is deliberately not asserted -- ``test_one_hot_sample_is_not_yet_an_incident``
    owns that claim, and here a stale rising edge from a previous round may legitimately
    open on the first poll.
    """
    machine.set_reading(**reading)
    bridge.snapshot()
    now[0] += 61.0
    rows: list[dict[str, Any]] = bridge.snapshot()["alerts"]
    return rows


class TestTheAlertCentreOnTheBridge:
    """The corner of the HUD that says 「这台机器不对劲」."""

    def test_a_window_without_an_alert_centre_says_so_instead_of_showing_nothing(
        self, tmp_path: Any
    ) -> None:
        bridge = _bridge(tmp_path)

        listed = bridge.alerts()
        assert listed["active"] == []
        assert listed["error"] == "告警中心未启用"
        assert bridge.alerts_ack("cpu") == {"ok": False, "error": "告警中心未启用"}
        assert bridge.alerts_ack_all()["ok"] is False

    def test_one_hot_sample_is_not_yet_an_incident(self, tmp_path: Any) -> None:
        bridge, _alerts, machine, now, _spoken = _alert_harness(tmp_path)
        machine.set_reading(cpu=97.0)

        assert bridge.snapshot()["alerts"] == [], "第一次超线还不该报警"
        now[0] += 30.0
        assert bridge.snapshot()["alerts"] == [], "三十秒的忙也许只是一次编译"
        now[0] += 31.0
        rows = bridge.snapshot()["alerts"]
        assert [row["code"] for row in rows] == ["cpu"]
        assert rows[0]["severity"] == "critical"

    def test_a_machine_that_stays_under_the_line_opens_nothing(self, tmp_path: Any) -> None:
        bridge, _alerts, machine, now, _spoken = _alert_harness(tmp_path)
        for _step in range(20):
            machine.set_reading(cpu=89.0)
            now[0] += 5.0
            assert bridge.snapshot()["alerts"] == []

    def test_the_line_is_named_by_the_thing_on_the_wrong_side_of_it(self, tmp_path: Any) -> None:
        bridge, _alerts, machine, now, _spoken = _alert_harness(tmp_path)

        rows = _bake(bridge, machine, now, free_gb=8.0)
        assert [row["code"] for row in rows] == ["disk:C:"]
        assert "C:" in rows[0]["message"]
        assert rows[0]["unit"] == "GB"

    def test_knowing_an_alert_empties_the_box_without_emptying_the_machine(
        self, tmp_path: Any
    ) -> None:
        bridge, alerts, machine, now, _spoken = _alert_harness(tmp_path)
        assert len(_bake(bridge, machine, now, cpu=97.0)) == 1

        assert bridge.alerts_ack("cpu")["ok"] is True
        assert bridge.snapshot()["alerts"] == [], "人已经看过了，别再挡在右下角"

        # The engine was never told the CPU got cooler, and it must not pretend it was.
        now[0] += 5.0
        machine.set_reading(cpu=10.0)
        assert bridge.snapshot()["alerts"] == []
        assert [row.code for row in alerts.history()] == ["cpu"], "恢复是被观察到的，不是被点掉的"

    def test_a_known_alert_stays_quiet_while_the_machine_stays_hot(self, tmp_path: Any) -> None:
        """认过了但没恢复：不再弹出来，也不算它好了。

        The distinction this pins down is that acknowledging a telemetry alert does not
        close it. If 「知道了」 closed it instead, the cooldown would start at that click
        and the same still-full disk would come back later as a brand new warning.
        """
        bridge, alerts, machine, now, _spoken = _alert_harness(tmp_path)
        assert len(_bake(bridge, machine, now, cpu=97.0)) == 1
        assert bridge.alerts_ack("cpu")["ok"] is True

        # Well past the ten-minute cooldown, with the reading never once coming back down.
        now[0] += 12 * 60.0
        assert bridge.snapshot()["alerts"] == []
        now[0] += 61.0
        assert bridge.snapshot()["alerts"] == [], "认过的告警不该在冷却结束后装作是新的"

        machine.set_reading(cpu=10.0)
        now[0] += 5.0
        bridge.snapshot()
        assert [row.code for row in alerts.history()] == ["cpu"], "恢复只记一次，不是两条"

    def test_everything_known_at_once_is_counted_not_assumed(self, tmp_path: Any) -> None:
        bridge, _alerts, machine, now, _spoken = _alert_harness(tmp_path)
        assert len(_bake(bridge, machine, now, cpu=97.0, memory=96.0, swap=90.0)) == 3

        assert bridge.alerts_ack_all() == {"ok": True, "count": 3, "error": ""}
        assert bridge.snapshot()["alerts"] == []

    def test_a_recovering_machine_moves_the_line_into_the_history(self, tmp_path: Any) -> None:
        bridge, alerts, machine, now, _spoken = _alert_harness(tmp_path)
        assert len(_bake(bridge, machine, now, cpu=97.0)) == 1

        machine.set_reading(cpu=20.0)
        now[0] += 5.0
        assert bridge.snapshot()["alerts"] == []
        assert [row.code for row in alerts.history()] == ["cpu"]

    def test_the_cooldown_keeps_a_flapping_machine_from_shouting_twice(self, tmp_path: Any) -> None:
        bridge, _alerts, machine, now, _spoken = _alert_harness(tmp_path)
        assert len(_bake(bridge, machine, now, cpu=97.0)) == 1

        machine.set_reading(cpu=10.0)
        now[0] += 10.0
        assert bridge.snapshot()["alerts"] == []

        assert _bake(bridge, machine, now, cpu=97.0) == [], "刚恢复就再报，是噪音不是告警"

        now[0] += 10 * 60.0
        assert [row["code"] for row in _bake(bridge, machine, now, cpu=97.0)] == ["cpu"]

    def test_a_switched_off_line_is_not_watched_at_all(self, tmp_path: Any) -> None:
        prefs = Preferences(tmp_path / "prefs.json")
        bridge, alerts, machine, now, _spoken = _alert_harness(tmp_path, preferences=prefs)
        assert alerts.apply_settings({"alerts_rules": {"cpu": {"enabled": False}}}) is None

        assert _bake(bridge, machine, now, cpu=99.0) == []

        # Switched back on: the same reading now counts. Without this half the test, an
        # engine that never alerts at all would pass it.
        assert alerts.apply_settings({"alerts_rules": {"cpu": {"enabled": True}}}) is None
        assert [row["code"] for row in _bake(bridge, machine, now, cpu=99.0)] == ["cpu"]

    def test_only_a_serious_one_is_allowed_to_interrupt_with_a_voice(self, tmp_path: Any) -> None:
        bridge, _alerts, machine, now, spoken = _alert_harness(tmp_path)
        warn = _bake(bridge, machine, now, memory=92.0)
        assert [row["severity"] for row in warn] == ["warn"]
        assert spoken == [], "普通告警亮在框里就够了，不该开口"

        assert len(_bake(bridge, machine, now, memory=92.0, cpu=97.0)) == 2
        assert [row.code for row in spoken] == ["cpu"]
        assert len(spoken) == 1, "同一条只说一次：以后每一轮只是把数字改新"

        _bake(bridge, machine, now, memory=92.0, cpu=99.0)
        assert len(spoken) == 1

    def test_muting_the_voice_leaves_the_box_alone(self, tmp_path: Any) -> None:
        prefs = Preferences(tmp_path / "prefs.json")
        bridge, alerts, machine, now, spoken = _alert_harness(tmp_path, preferences=prefs)
        assert alerts.apply_settings({"alerts_speak_critical": False}) is None

        assert len(_bake(bridge, machine, now, cpu=97.0)) == 1
        assert spoken == []

    def test_a_spoken_warning_that_crashes_still_reaches_the_screen(self, tmp_path: Any) -> None:
        bridge, alerts, machine, now, _spoken = _alert_harness(tmp_path)
        calls: list[Any] = []

        def explode(alert: Any) -> None:
            calls.append(alert)
            raise RuntimeError("TTS 断了")

        alerts.on_fire = explode

        assert len(_bake(bridge, machine, now, cpu=97.0)) == 1, "说不出话不等于没发生"
        assert len(calls) == 1

    def test_the_pull_surface_agrees_with_the_polled_report(self, tmp_path: Any) -> None:
        """``alerts()`` is the late joiner's door: it must show the same rows."""
        bridge, _alerts, machine, now, _spoken = _alert_harness(tmp_path)
        polled = _bake(bridge, machine, now, cpu=97.0)
        assert polled, "前提：框里确实有一行"

        pulled = bridge.alerts()
        assert pulled["error"] == ""
        active = cast("list[dict[str, Any]]", pulled["active"])
        assert [row["code"] for row in active] == [row["code"] for row in polled]

    def test_a_failed_scheduled_job_becomes_a_line_only_a_person_can_clear(
        self, tmp_path: Any
    ) -> None:
        from jarvis.__main__ import _job_failure_reporter

        bridge, alerts, _machine, _now, _spoken = _alert_harness(tmp_path)
        report = _job_failure_reporter(alerts)
        assert report is not None

        report("job-7", "每晚整理下载", "磁盘写入被拒绝")

        rows = alerts.active()
        assert [row.code for row in rows] == ["job:job-7"]
        assert "每晚整理下载" in rows[0].message
        assert "磁盘写入被拒绝" in rows[0].message
        assert [row["code"] for row in bridge.snapshot()["alerts"]] == ["job:job-7"]

        # A job failure has no reading to recover from, so the next poll must not
        # wash it away -- it leaves the screen only when a person says they have seen it.
        assert bridge.snapshot()["alerts"][0]["severity"] == "warn"
        assert bridge.alerts_ack("job:job-7")["ok"] is True
        assert alerts.active() == ()
        assert bridge.snapshot()["alerts"] == []

    def test_a_job_that_fails_every_minute_speaks_once(self, tmp_path: Any) -> None:
        from jarvis.__main__ import _job_failure_reporter

        _bridge, alerts, _machine, now, _spoken = _alert_harness(tmp_path)
        report = _job_failure_reporter(alerts)
        assert report is not None

        report("job-7", "每晚整理下载", "断了")
        assert len(alerts.active()) == 1
        now[0] += 60.0
        report("job-7", "每晚整理下载", "还是断了")
        assert len(alerts.active()) == 1, "重复失败只更新那一条，不再开一条"
        assert "还是断了" in alerts.active()[0].message

    def test_no_alert_centre_means_the_scheduler_stays_quiet(self) -> None:
        from jarvis.__main__ import _job_failure_reporter

        assert _job_failure_reporter(None) is None
