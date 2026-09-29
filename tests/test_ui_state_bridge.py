"""Deterministic tests for the headless state bridge.

No Qt, no browser, no microphone: the bridge is a pure event fold, which is the
only reason its behaviour can be pinned this cheaply.
"""

from __future__ import annotations

from jarvis.core.events import VOICE_STATUS_KIND, PipelineEvent, VoicePhase
from jarvis.orchestration.voice_pipeline import PipelineState
from jarvis.ui.state_bridge import (
    ChatTurn,
    StateBridge,
    UiState,
    UiVoiceState,
)


def _ev(kind: str, text: str = "") -> PipelineEvent:
    return PipelineEvent(kind=kind, text=text)


def test_initial_state() -> None:
    bridge = StateBridge()
    state = bridge.snapshot()
    assert state.voice_state == UiVoiceState.IDLE
    assert state.history == ()
    assert state.last_event == ""
    assert state.interrupted is False


def test_wake_transitions_to_listening() -> None:
    bridge = StateBridge()
    bridge.push_event(_ev("wake", "jarvis"))
    state = bridge.snapshot()
    assert state.voice_state == UiVoiceState.LISTENING
    assert state.last_event == "wake"


def test_full_turn_user_then_assistant() -> None:
    bridge = StateBridge()
    bridge.push_event(_ev("wake", "jarvis"))
    bridge.push_event(_ev("speech_start"))
    bridge.push_event(_ev("speech_end"))
    bridge.push_event(_ev("reply", "打开灯"))  # user transcript
    bridge.push_event(_ev("reply", "好的,已打开灯"))  # assistant answer
    state = bridge.snapshot()
    assert state.voice_state == UiVoiceState.PROCESSING
    assert state.history == (
        ChatTurn("user", "打开灯"),
        ChatTurn("assistant", "好的,已打开灯"),
    )


def test_barge_in_sets_interrupted() -> None:
    bridge = StateBridge()
    bridge.push_event(_ev("wake"))
    bridge.push_event(_ev("speech_end"))
    bridge.push_event(_ev("reply", "播放音乐"))  # user
    bridge.push_event(_ev("reply", "正在播放"))  # assistant
    bridge.push_event(_ev("barge_in"))
    assert bridge.snapshot().interrupted is True


def test_subscribe_receives_snapshots() -> None:
    bridge = StateBridge()
    received: list[UiState] = []
    bridge.subscribe(received.append)
    bridge.push_event(_ev("wake"))
    assert len(received) == 1
    assert received[0].voice_state == UiVoiceState.LISTENING


def test_unsubscribe_stops_notifications() -> None:
    bridge = StateBridge()
    received: list[UiState] = []
    unsub = bridge.subscribe(received.append)
    unsub()
    bridge.push_event(_ev("wake"))
    assert received == []


def test_reset_clears_state() -> None:
    bridge = StateBridge()
    bridge.push_event(_ev("wake"))
    bridge.push_event(_ev("reply", "你好"))  # user
    bridge.reset()
    state = bridge.snapshot()
    assert state.voice_state == UiVoiceState.IDLE
    assert state.history == ()


def test_history_cap() -> None:
    bridge = StateBridge(max_history_turns=2)
    for i in range(5):
        bridge.push_event(_ev("reply", f"u{i}"))  # user
        bridge.push_event(_ev("reply", f"a{i}"))  # assistant
    state = bridge.snapshot()
    # 10 turns total; cap = 2*2 = 4 -> keep the last four.
    assert len(state.history) == 4
    assert state.history[0] == ChatTurn("user", "u3")
    assert state.history[-1] == ChatTurn("assistant", "a4")


class TestTurnStateArrivesAsEvents:
    """The pipeline's ``state`` events are what move the indicator.

    ``wake`` with a command attached jumps straight to PROCESSING without any
    intervening capture events, so an indicator driven only by wake/speech_start
    would sit on "listening" for the whole reply.
    """

    def test_state_event_moves_the_indicator(self) -> None:
        bridge = StateBridge()
        bridge.push_event(PipelineEvent(kind="state", text=PipelineState.PROCESSING.value))

        assert bridge.snapshot().voice_state is UiVoiceState.PROCESSING

    def test_pipeline_names_are_exactly_the_ones_the_bridge_knows(self) -> None:
        """The bridge maps by string, so a rename must fail here, not in the HUD."""
        assert {state.value for state in PipelineState} == {state.value for state in UiVoiceState}

    def test_an_unmapped_state_name_leaves_the_indicator_alone(self) -> None:
        bridge = StateBridge()
        bridge.push_event(_ev("wake"))
        before = bridge.snapshot().voice_state

        bridge.push_event(PipelineEvent(kind="state", text="regenerating"))

        assert bridge.snapshot().voice_state is before


class TestVoiceAvailability:
    """ "Can I talk to it" is a different question from "is it listening now"."""

    def _status(self, phase: VoicePhase, detail: str = "") -> PipelineEvent:
        return PipelineEvent(kind=VOICE_STATUS_KIND, text=phase.value, detail=detail)

    def test_default_is_off(self) -> None:
        assert StateBridge().snapshot().voice is VoicePhase.OFF

    def test_loading_is_visible_as_loading(self) -> None:
        bridge = StateBridge()

        bridge.push_event(self._status(VoicePhase.LOADING))

        state = bridge.snapshot()
        assert state.voice is VoicePhase.LOADING
        assert state.voice_state is UiVoiceState.IDLE

    def test_failure_carries_the_reason(self) -> None:
        bridge = StateBridge()
        bridge.push_event(_ev("wake"))

        bridge.push_event(self._status(VoicePhase.FAILED, "没有可用的麦克风"))

        state = bridge.snapshot()
        assert state.voice is VoicePhase.FAILED
        assert state.voice_detail == "没有可用的麦克风"
        assert state.voice_state is UiVoiceState.IDLE, "a dead loop cannot also be listening"

    def test_running_reports_the_keyword(self) -> None:
        bridge = StateBridge()

        bridge.push_event(self._status(VoicePhase.RUNNING, "唤醒词：你好小夜"))

        assert bridge.snapshot().voice_detail == "唤醒词：你好小夜"

    def test_muted_stops_claims_of_listening(self) -> None:
        bridge = StateBridge()
        bridge.push_event(self._status(VoicePhase.RUNNING))
        bridge.push_event(_ev("wake"))

        bridge.push_event(self._status(VoicePhase.MUTED))

        state = bridge.snapshot()
        assert state.voice is VoicePhase.MUTED
        assert state.voice_state is UiVoiceState.IDLE

    def test_an_unknown_phase_is_logged_and_ignored(self) -> None:
        bridge = StateBridge()
        bridge.push_event(self._status(VoicePhase.RUNNING))

        bridge.push_event(PipelineEvent(kind=VOICE_STATUS_KIND, text="recalibrating"))

        assert bridge.snapshot().voice is VoicePhase.RUNNING

    def test_reset_forgets_availability(self) -> None:
        bridge = StateBridge()
        bridge.push_event(self._status(VoicePhase.RUNNING))

        bridge.reset()

        assert bridge.snapshot().voice is VoicePhase.OFF


def test_add_turn_writes_both_sides_of_a_typed_exchange() -> None:
    """Typed turns share the transcript with spoken ones."""
    bridge = StateBridge()

    bridge.add_turn("user", "今天星期几")
    bridge.add_turn("assistant", "星期六")

    assert bridge.snapshot().history == (
        ChatTurn("user", "今天星期几"),
        ChatTurn("assistant", "星期六"),
    )


def test_add_turn_notifies_subscribers() -> None:
    bridge = StateBridge()
    seen: list[UiState] = []
    bridge.subscribe(seen.append)

    bridge.add_turn("user", "你好")

    assert len(seen) == 1
    assert seen[0].history == (ChatTurn("user", "你好"),)


def test_add_turn_ignores_empty_text() -> None:
    bridge = StateBridge()

    bridge.add_turn("assistant", "")

    assert bridge.snapshot().history == ()


def test_clear_history_keeps_the_voice_indicator_alone() -> None:
    """「清空」 empties the log; it must not pretend the microphone changed."""
    bridge = StateBridge()
    bridge.push_event(_ev("wake", "你好小夜"))
    bridge.push_event(_ev("voice_status", "running"))
    bridge.add_turn("user", "你好")

    bridge.clear_history()

    state = bridge.snapshot()
    assert state.history == ()
    assert state.voice is VoicePhase.RUNNING
    assert (
        state.voice_state is UiVoiceState.LISTENING
    ), "the turn state belongs to the microphone, and clearing a log does not stop it"


def test_clear_history_resets_the_reply_pairing() -> None:
    """Otherwise the next spoken transcript is filed as an assistant answer."""
    bridge = StateBridge()
    bridge.add_turn("user", "半句")  # awaiting its assistant half

    bridge.clear_history()
    bridge.push_event(_ev("reply", "打开灯"))

    assert bridge.snapshot().history == (ChatTurn("user", "打开灯"),)
