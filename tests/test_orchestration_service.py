"""Tests for :class:`~jarvis.orchestration.service.OrchestrationService`.

These exercise the lifecycle wrapper around the voice pipeline. The wake-word
and VAD engines are injected as fakes (the real OpenWakeWord / Silero models
are not available in this environment), but the ASR / TTS services, the agent
graph, and the microphone loop are all exercised for real — just with
deterministic stand-ins.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from threading import Event
from typing import cast

from jarvis.config.loader import load_defaults
from jarvis.config.schema import OrchestrationSection, VadSection, WakeWordSection
from jarvis.core.exceptions import ConfigurationError
from jarvis.orchestration import OrchestrationService, OrchestrationSettings
from jarvis.orchestration.player import NullAudioPlayer
from jarvis.orchestration.types import PipelineEvent
from jarvis.orchestration.voice_pipeline import PipelineState
from jarvis.tts.types import AudioChunk
from jarvis.wakeword.types import WakeHit
from tests._fakes import (
    FakeAsr,
    FakeGraph,
    FakeLlmClient,
    FakeTts,
    ScriptedAudioSource,
    ScriptedVadEngine,
    ScriptedWakeWordEngine,
)

_FRAME = b"\x00" * 1024


def _wait_for(cond: Callable[[], bool], timeout: float = 2.0) -> bool:
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        time.sleep(0.01)
    return False


def fast_vad_section() -> VadSection:
    """A ``vad.*`` section with tiny endpointing windows for fast, deterministic tests."""
    return VadSection.from_mapping(
        {
            "enabled": False,
            "engine": "silero",
            "threshold": 0.5,
            "min_speech_ms": 10,
            "max_silence_ms": 10,
            "speech_pad_ms": 0,
            "max_speech_ms": 0,
        }
    )


def orch_settings(*, enabled: bool = True, barge_in: bool = True) -> OrchestrationSettings:
    raw = load_defaults()
    return OrchestrationSettings(
        section=OrchestrationSection(enabled=enabled, barge_in=barge_in, default_agent="chat"),
        wakeword=WakeWordSection.from_mapping(cast("Mapping[str, object]", raw["wakeword"])),
        vad=fast_vad_section(),
    )


def test_disabled_service_is_noop() -> None:
    service = OrchestrationService(
        lambda: orch_settings(enabled=False),
        asr=FakeAsr(running=False),
        tts=FakeTts(running=False),
        llm_client_provider=lambda: FakeLlmClient(),
    )
    service.start()
    assert service._pipeline is None  # nothing was wired up
    service.stop()  # must be a safe no-op


def test_requires_asr_loaded() -> None:
    service = OrchestrationService(
        lambda: orch_settings(enabled=True),
        asr=FakeAsr(running=False),
        tts=FakeTts(running=True),
        llm_client_provider=lambda: FakeLlmClient(),
    )
    try:
        service.start()
        raise AssertionError("expected ConfigurationError")
    except ConfigurationError as exc:
        assert "asr" in str(exc).lower()


def test_requires_tts_loaded() -> None:
    service = OrchestrationService(
        lambda: orch_settings(enabled=True),
        asr=FakeAsr(running=True),
        tts=FakeTts(running=False),
        llm_client_provider=lambda: FakeLlmClient(),
    )
    try:
        service.start()
        raise AssertionError("expected ConfigurationError")
    except ConfigurationError as exc:
        assert "tts" in str(exc).lower()


def test_full_turn_with_injected_engines() -> None:
    events: list[PipelineEvent] = []
    reply_count = 0
    finished = Event()

    def on_event(event: PipelineEvent) -> None:
        events.append(event)
        if event.kind == "reply":
            nonlocal reply_count
            reply_count += 1
            if reply_count >= 2:  # transcript + agent answer both seen
                finished.set()

    asr = FakeAsr("你好")
    tts = FakeTts([AudioChunk(audio=b"x", sample_rate=16_000, is_final=True)])
    source = ScriptedAudioSource([_FRAME, _FRAME, _FRAME, _FRAME])
    service = OrchestrationService(
        lambda: orch_settings(enabled=True),
        asr=asr,
        tts=tts,
        llm_client_provider=lambda: FakeLlmClient("chat"),
        source_factory=lambda: source,
        player_factory=lambda: NullAudioPlayer(),
        wakeword_engine_factory=lambda _section: ScriptedWakeWordEngine([WakeHit("jarvis", 1.0)]),
        vad_engine_factory=lambda: ScriptedVadEngine([1.0, 1.0, 0.0]),
        graph_factory=lambda: FakeGraph("chat"),
        on_event=on_event,
    )
    service.start()
    assert finished.wait(timeout=5.0), "orchestration turn did not complete"
    assert _wait_for(
        lambda: service._pipeline is not None
        and service._pipeline._get_state() == PipelineState.LISTENING,
        timeout=2.0,
    )
    service.stop()

    kinds = [e.kind for e in events]
    assert "wake" in kinds
    assert "speech_end" in kinds
    # The real AgentGraph routed to the chat agent (LLM replied "chat").
    assert asr.calls and tts.calls == ["chat"]
