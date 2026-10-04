"""#91 / #92: the thinking chain, from the request that asks for it to the row that shows it.

Measured on 2026-10-02 against the qwenai token-plan endpoint: with ``enable_thinking``
the reply carries ``reasoning_content`` and ``usage.completion_tokens_details
.reasoning_tokens``; ``thinking_budget`` truncates the reasoning while the answer still
finishes; without the flag, neither field appears at all. So "the window shows no
thinking" was a request-side absence, not a rendering gap -- and these tests follow that
wire all the way to the panel that moves it.

The budget is asserted as a number the provider echoes back, never as a label: a
高/中/低 picker would be a decoration, and this project has already shipped one of
those and been told, correctly, not to.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest

from jarvis.app.chat_service import ChatService, ThinkingRequest
from jarvis.app.preferences import CHAT_HISTORY_TURNS, Preferences
from jarvis.app.settings_service import (
    DEFAULT_HISTORY_TURNS,
    DEFAULT_THINKING_BUDGET,
    SettingsService,
)
from jarvis.config.schema import LlmSection
from jarvis.llm.types import ChatMessage, ChatResponse, GenerationOptions, ToolCall, Usage
from jarvis.tools.types import ToolResult
from jarvis.ui.state_bridge import ChatTurn, StateBridge
from tests.test_llm_client import FakeTransport, make_client, ok_response


def thinking_reply(reasoning: str = "先算今天", tokens: int = 32) -> dict[str, object]:
    """The shape the endpoint actually answers with when thinking is switched on."""
    return {
        "model": "test-model-2024",
        "choices": [
            {
                "message": {"content": "答案是星期六", "reasoning_content": reasoning},
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": 100,
            "completion_tokens": 60,
            "completion_tokens_details": {"reasoning_tokens": tokens},
        },
    }


class _ScriptedClient:
    """A model client whose replies are handed to it as a list of ``ChatResponse``."""

    provider_name = "fake"
    model = "fake"

    def __init__(self, *replies: ChatResponse) -> None:
        self._replies = list(replies)
        self.options: list[GenerationOptions | None] = []

    def complete(
        self,
        messages: Any,
        *,
        options: GenerationOptions | None = None,
    ) -> ChatResponse:
        del messages
        self.options.append(options)
        return self._replies.pop(0)

    def stream(self, messages: Any, *, options: Any = None) -> Any:
        del messages, options
        yield from ()


class _MemoryWithoutAnything:
    """Real tool, empty answer: the point is the round trip, not the retrieval."""

    running = True

    def recall(self, query: str, *, top_k: int = 0) -> list[Any]:
        del query, top_k
        return []

    def remember(self, content: str, *, kind: str = "fact", source: str = "") -> Any:
        raise AssertionError("this test never writes")

    def profile_block(self, *args: Any, **kwargs: Any) -> str:
        return ""


class _ToolsWithMemorySearch:
    """``ToolService`` narrowed to the three calls ``ChatService`` makes of it."""

    enabled = True

    def to_openai_tools(self) -> list[dict[str, object]]:
        return [{"type": "function", "function": {"name": "memory_search"}}]

    def invoke(self, name: str, arguments: Any, *, confirmed: bool = False) -> ToolResult:
        del arguments, confirmed
        return ToolResult(ok=True, output="记忆里没有", tool=name)


def _chat(client: _ScriptedClient, **kwargs: Any) -> ChatService:
    service = ChatService(lambda: client, session_id="t", **kwargs)
    service.start()
    return service


class TestWireFormat:
    def test_an_ordinary_request_sends_neither_field(self) -> None:
        transport = FakeTransport()
        transport.json_replies.append(ok_response("ok"))
        make_client(transport).complete([ChatMessage.user("hi")])
        payload = transport.json_calls[0]["payload"]
        assert isinstance(payload, dict)
        assert "enable_thinking" not in payload
        assert "thinking_budget" not in payload

    def test_switched_on_puts_the_flag_and_the_budget_on_the_wire(self) -> None:
        transport = FakeTransport()
        transport.json_replies.append(thinking_reply())
        reply = make_client(transport).complete(
            [ChatMessage.user("今天星期几")],
            options=GenerationOptions(thinking=True, thinking_budget=40),
        )
        payload = transport.json_calls[0]["payload"]
        assert isinstance(payload, dict)
        assert payload["enable_thinking"] is True
        assert payload["thinking_budget"] == 40
        assert reply.reasoning == "先算今天"
        assert reply.usage is not None and reply.usage.reasoning_tokens == 32

    def test_the_client_serialises_an_explicit_false_as_given(self) -> None:
        """Client-level contract only: if a caller says ``thinking=False``, say so rather
        than dropping the field. Who decides to say it is ``ChatService``'s business, and
        it does not -- see ``test_turning_it_back_off_sends_nothing_extra_at_all``."""
        transport = FakeTransport()
        transport.json_replies.append(ok_response("ok"))
        make_client(transport).complete(
            [ChatMessage.user("hi")], options=GenerationOptions(thinking=False)
        )
        payload = transport.json_calls[0]["payload"]
        assert isinstance(payload, dict) and payload["enable_thinking"] is False

    def test_a_provider_staying_silent_about_reasoning_reads_as_unknown_not_zero(self) -> None:
        """``None`` and ``0`` are different claims: one is "it did not say", the other is
        "it thought for nothing", and the panel can only mean something with those kept
        apart."""
        transport = FakeTransport()
        transport.json_replies.append(ok_response("ok"))
        reply = make_client(transport).complete([ChatMessage.user("hi")])
        assert reply.reasoning == ""
        assert reply.usage is not None and reply.usage.reasoning_tokens is None


class TestChatServiceAsks:
    def test_no_settings_means_the_request_is_untouched(self) -> None:
        client = _ScriptedClient(ChatResponse(content="答", model="fake"))
        _chat(client).ask("你好")
        assert client.options[0] is None or client.options[0].thinking is None

    def test_the_operators_switch_reaches_the_next_request(self) -> None:
        client = _ScriptedClient(ChatResponse(content="答", model="fake"))
        chat = _chat(client, thinking_provider=lambda: ThinkingRequest(True, 512))
        chat.ask("你好")
        options = client.options[0]
        assert options is not None
        assert options.thinking is True and options.thinking_budget == 512

    def test_turning_it_back_off_sends_nothing_extra_at_all(self) -> None:
        """Read per turn, so "save" means "now" -- and off means the request goes back to
        exactly the shape it had before this setting existed.

        Sending an explicit ``enable_thinking: false`` was the tempting version and it is
        the wrong one: it puts a field no provider was ever asked about onto the default
        path, and a strict OpenAI-compatible endpoint can answer that with a 400.
        """
        state = ThinkingRequest(True, 512)
        client = _ScriptedClient(
            ChatResponse(content="答", model="fake"),
            ChatResponse(content="答", model="fake"),
        )
        chat = _chat(client, thinking_provider=lambda: state)
        chat.ask("第一问")
        state = ThinkingRequest(False, 512)
        chat.ask("第二问")
        assert client.options[0] is not None and client.options[0].thinking is True
        assert client.options[1] is not None and client.options[1].thinking is None

    def test_a_budget_of_zero_does_not_ask_for_zero_tokens_of_thinking(self) -> None:
        client = _ScriptedClient(ChatResponse(content="答", model="fake"))
        _chat(client, thinking_provider=lambda: ThinkingRequest(True, 0)).ask("你好")
        assert client.options[0] is not None
        assert client.options[0].thinking is True and client.options[0].thinking_budget is None

    def test_the_reasoning_and_its_cost_come_back_on_the_reply(self) -> None:
        client = _ScriptedClient(
            ChatResponse(
                content="答",
                model="fake",
                reasoning="先想了想",
                usage=Usage(prompt_tokens=10, completion_tokens=5, reasoning_tokens=7),
            )
        )
        reply = _chat(client, thinking_provider=lambda: ThinkingRequest(True, 1000)).ask("你好")
        assert reply.reasoning == "先想了想"
        assert reply.reasoning_tokens == 7
        assert reply.to_dict()["reasoning"] == "先想了想"

    def test_a_turn_that_called_a_tool_keeps_every_thought_it_had(self) -> None:
        """Two rounds, two thoughts. Dropping the first hides the part where she decided
        which tool to reach for, and that is the part an operator auditing her wants."""
        client = _ScriptedClient(
            ChatResponse(
                content="",
                model="fake",
                reasoning="第一轮：该查记忆",
                tool_calls=(ToolCall(id="c1", name="memory_search", arguments='{"query":"JDK"}'),),
                usage=Usage(prompt_tokens=1, completion_tokens=1, reasoning_tokens=5),
            ),
            ChatResponse(
                content="Temurin 17",
                model="fake",
                reasoning="第二轮：照着结果答",
                usage=Usage(prompt_tokens=1, completion_tokens=1, reasoning_tokens=6),
            ),
        )
        reply = _chat(
            client,
            tool_provider=lambda: cast(Any, _ToolsWithMemorySearch()),
            memory_provider=lambda: cast(Any, _MemoryWithoutAnything()),
            thinking_provider=lambda: ThinkingRequest(True, 800),
        ).ask("我的 JDK 是啥版本")
        assert "第一轮" in reply.reasoning and "第二轮" in reply.reasoning
        assert reply.reasoning_tokens == 11
        assert reply.tools_used == ("memory_search",)

    def test_a_silent_provider_leaves_the_cost_unknown_even_after_a_tool_round(self) -> None:
        client = _ScriptedClient(
            ChatResponse(
                content="",
                model="fake",
                tool_calls=(ToolCall(id="c1", name="memory_search", arguments='{"query":"x"}'),),
            ),
            ChatResponse(content="答", model="fake"),
        )
        reply = _chat(
            client,
            tool_provider=lambda: cast(Any, _ToolsWithMemorySearch()),
            thinking_provider=lambda: ThinkingRequest(True, 100),
        ).ask("你好")
        assert reply.reasoning_tokens is None


class TestStateBridgeCarriesIt:
    def test_a_turn_keeps_its_reasoning_next_to_its_text(self) -> None:
        bridge = StateBridge()
        bridge.add_turn("assistant", "答案是星期六", "先翻了日历", "deepseek-chat")
        assert bridge.snapshot().history == (
            ChatTurn("assistant", "答案是星期六", "先翻了日历", "deepseek-chat"),
        )
        assert bridge.snapshot().to_dict()["history"] == [
            {
                "role": "assistant",
                "text": "答案是星期六",
                "reasoning": "先翻了日历",
                # Which model said this. Cheap to carry, and unrecoverable afterwards:
                # a round table writes three models into one transcript, and the panel
                # has nowhere else to read the difference from.
                "model": "deepseek-chat",
                # The round table's record rides along for the same reason: it is the
                # only place the page can read who argued what, and it is display-only
                # -- nothing stores it, so this is the whole of its life.
                "record": "",
            }
        ]

    def test_a_spoken_turn_arrives_with_an_empty_chain_the_page_can_skip(self) -> None:
        bridge = StateBridge()
        bridge.add_turn("user", "几点了")
        assert bridge.snapshot().history[0].reasoning == ""


class TestSettingsPanel:
    @pytest.fixture
    def settings(self, tmp_path: Path) -> SettingsService:
        section = LlmSection(
            default_provider="a",
            timeout_seconds=30.0,
            max_retries=1,
            retry_backoff_seconds=0.5,
            providers={},
        )
        service = SettingsService(
            Preferences(tmp_path / "prefs.json"),
            cast(Any, _NoopLlm()),
            lambda: section,
            environ={},
            persist_env=lambda _name, _value: True,
        )
        service.start()
        return service

    def test_the_knobs_start_where_the_defaults_are(self, settings: SettingsService) -> None:
        assert settings.thinking_enabled() is False
        assert settings.thinking_budget() == DEFAULT_THINKING_BUDGET
        assert settings.history_turns() == DEFAULT_HISTORY_TURNS

    def test_saving_thinking_takes_effect_on_the_next_read(self, settings: SettingsService) -> None:
        outcome = settings.apply({"thinking_enabled": True, "thinking_budget": 4096})
        assert outcome["problems"] == {}, outcome["problems"]
        assert settings.thinking_enabled() is True
        assert settings.thinking_budget() == 4096

    def test_a_budget_outside_the_range_is_refused_with_the_range_in_the_message(
        self, settings: SettingsService
    ) -> None:
        outcome = settings.apply({"thinking_budget": 999_999})
        assert "64" in outcome["problems"]["thinking_budget"]
        assert settings.thinking_budget() == DEFAULT_THINKING_BUDGET

    def test_a_non_number_is_answered_as_a_problem_not_a_crash(
        self, settings: SettingsService
    ) -> None:
        assert "history_turns" in settings.apply({"history_turns": "很多"})["problems"]

    def test_answering_from_this_turn_alone_is_a_number_the_panel_accepts(
        self, settings: SettingsService
    ) -> None:
        assert settings.apply({"history_turns": 0})["problems"] == {}
        assert settings.history_turns() == 0

    def test_the_snapshot_shows_the_same_bounds_the_writer_enforces(
        self, settings: SettingsService
    ) -> None:
        """One range, stated once, shown where it is set -- or the panel invites a
        number the save then refuses."""
        snapshot = settings.snapshot()
        assert snapshot["thinking_budget_bounds"] == [64, 16000]
        assert snapshot["history_turns_bounds"] == [0, 50]

    def test_the_chat_window_replays_exactly_the_turns_the_setting_asks_for(
        self, tmp_path: Path
    ) -> None:
        stored = Preferences(tmp_path / "p.json")
        stored.set(CHAT_HISTORY_TURNS, 2)
        client = _ScriptedClient(*[ChatResponse(content="答", model="fake")] * 8)
        chat = _chat(
            client, history_turns_provider=lambda: stored.number(CHAT_HISTORY_TURNS, default=10)
        )
        for index in range(5):
            chat.ask(f"第 {index} 问")
        assert chat.history_length == 2


class TestTheFigureMovesWhileSheThinks:
    """#93, pinned where it actually broke: the order of two checks.

    The pet already had a ``thinking`` pose. What it did not have was a chance to show
    it -- every consumer of the turn axis asked ``phase === 'running'`` first, and a
    typed question is answered with the microphone shut, so a six-second answer drew a
    motionless figure and a top bar reading 「语音未启用」. These assert the order, because
    a mood map that looks complete can still be unreachable.

    Read off the source rather than a browser: the same shape the rest of this project's
    frontend invariants are pinned by, since the page under test only exists inside a
    WebView2 window an agent cannot drive.
    """

    ROOT = Path(__file__).resolve().parent.parent
    FRONTEND = ROOT / "frontend" / "src"

    def _read(self, *parts: str) -> str:
        return (self.ROOT.joinpath(*parts)).read_text(encoding="utf-8")

    def test_the_bridge_says_she_is_thinking_before_the_model_is_called(self) -> None:
        """Set before the call, and released by something that cannot be skipped.

        The release used to be a ``finally`` inside ``chat_ask``. It no longer can be: a
        turn now runs on its own thread, so the thing that clears 思考中 is the registry
        filing the turn as finished -- which happens for a raise as well as for a
        return, and which recomputes the indicator from *all* running turns instead of
        trusting the one that just ended. Otherwise two tabs would fight over one light.
        """
        source = self._read("jarvis", "ui", "desktop.py")
        assert "state.set_turn(UiVoiceState.PROCESSING)" in source
        finish = source[source.index("def _finish_turn") : source.index("def _turn_changed")]
        assert "UiVoiceState.IDLE" in finish, "回答落地要把思考中放掉"
        assert "self._turns.waiting()" in finish, "还要看有没有别的回合在跑或在排队"

        registry = self._read("jarvis", "app", "turns.py")
        drive = registry[registry.index("def _drive(") :]
        assert (
            "except Exception" in drive[: drive.index("def cancel")]
        ), "worker 抛异常也要把回合登记成结束，否则指示灯永远停不下来"

    def test_the_pet_reaches_the_thinking_pose_without_a_microphone(self) -> None:
        stage = self._read("frontend", "src", "avatar", "PetStage.vue")
        thinking = stage.index("voice.turn === 'processing'")
        phase = stage.index("voice.phase === 'running'")
        assert thinking < phase, "思考中的判定必须排在麦克风状态之前"

    def test_the_hud_core_does_the_same(self) -> None:
        core = self._read("frontend", "src", "components", "VoiceCore.vue")
        assert core.index("voice.turn === 'processing'") < core.index("switch (voice.phase)")

    def test_the_top_bar_says_it_out_loud(self) -> None:
        store = self._read("frontend", "src", "stores", "voice.ts")
        label = store[store.index("const label") :]
        assert label.index("turn.value === 'processing'") < label.index("switch (phase.value)")


class TestThroughTheWindowBridge:
    """The last hop: the answer the page draws has to carry the chain with it.

    ``chat_ask`` returns a dict for the bubble, but the transcript the panel renders
    comes from the state bridge -- so if ``reasoning`` stopped at the reply object, the
    box would stay empty no matter what the model thought.
    """

    def test_the_reasoning_lands_in_the_history_the_page_reads(self, tmp_path: Path) -> None:
        from jarvis.ui.state_bridge import StateBridge
        from tests.test_ui_desktop import _bridge

        class Client:
            provider_name = "fake"
            model = "fake"

            def complete(self, messages: Any, *, options: Any = None) -> ChatResponse:
                return ChatResponse(content="星期六", model="fake", reasoning="翻了日历")

            def stream(self, messages: Any, *, options: Any = None) -> Any:
                del messages, options
                yield from ()

        state = StateBridge()
        chat = ChatService(lambda: Client(), session_id="t")
        chat.start()
        reply = _bridge(tmp_path, chat=chat, state=state).chat_ask("今天星期几")

        assert reply["reasoning"] == "翻了日历"
        assistant = [turn for turn in state.snapshot().history if turn.role == "assistant"][-1]
        assert assistant.text == "星期六"
        assert assistant.reasoning == "翻了日历"
        # The chain travels beside the text; it is never read aloud or replayed as it.
        assert assistant.text != assistant.reasoning


class _NoopLlm:
    """Stands in for ``LlmService``: neither new knob needs a client rebuilt."""

    def __init__(self) -> None:
        self.overrides: list[Any] = []

    def set_section_override(self, section: Any) -> None:
        self.overrides.append(section)
