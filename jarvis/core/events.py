"""Cross-layer event contracts: plain data, no behaviour, no third-party imports.

``core`` may be imported by any layer (architecture rule 1), so an event type that
both the orchestration layer *produces* and the UI layer *consumes* has to live
here. It used to live in :mod:`jarvis.orchestration.types`, which forced ``ui`` to
import ``orchestration`` — a rule 6 violation that only stayed invisible because
nothing enched the layering until ``tests/test_architecture_layers.py``.

Keep this module stdlib-only. Adding an import from anywhere else in ``jarvis``
here would quietly re-open the cycle the layering exists to prevent.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

VOICE_STATUS_KIND = "voice_status"
PIPELINE_STATE_KIND = "state"


@dataclass(frozen=True, slots=True)
class PipelineEvent:
    """A milestone emitted by the voice pipeline (for logging / UI).

    Producers: :mod:`jarvis.orchestration.voice_pipeline`. Consumers: the console
    reporter in ``jarvis/__main__.py`` and :class:`jarvis.ui.state_bridge.StateBridge`.

    ``kind`` is intentionally not validated here: both consumers dispatch through a
    lookup table that tolerates an unknown key, and this object is built on the
    microphone capture thread, where raising would cost the user their turn.
    """

    kind: str
    """One of: wake | speech_start | speech_end | user_text | reply | barge_in | error | state.

    ``user_text`` is what the microphone heard and ``reply`` is what she answered. They
    are different kinds because a listener must never have to guess who spoke from what
    arrived last -- one half-finished turn would then mislabel every turn after it.
    """

    text: str = ""
    """Associated text (the transcribed utterance or the agent reply)."""

    detail: object = None
    """Optional structured payload (e.g. a score, a sample count, or a state name)."""


@dataclass(frozen=True, slots=True)
class UsageEvent:
    """One completed model call, accounted. Produced by ``llm``, persisted by ``app``.

    The producer knows the numbers but not where they should be stored, and the
    store must not be importable from ``llm`` (layer rule), so the composition root
    wires one to the other through this record. There is deliberately no timestamp
    field: the sink stamps the row with its own canonical format, because a
    duplicated ``strftime`` pattern across two layers is a drift bug waiting to
    happen -- and the sink runs within milliseconds of the call finishing anyway.

    ``cached_tokens`` is ``None`` when the provider did not say. That is *not* the
    same as ``0``, and the statistics screen has to keep them apart: a cache-hit
    rate drawn from missing data is the kind of number that gets quoted in a demo
    and believed for years.
    """

    provider: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    cached_tokens: int | None = None
    latency_ms: float = 0.0
    task_id: str = ""
    """Which turn or round-table task this call was made for, when there was one.

    Empty is honest for a call made outside a tracked turn. The ledger groups by it so
    "what did that cost" can be answered for one piece of work instead of only for a
    week -- the grouping that a three-model task needs and a time window cannot give.
    """


class VoicePhase(StrEnum):
    """Whether the microphone is available to the user, and what it is doing about it.

    This is deliberately *not* :class:`jarvis.orchestration.voice_pipeline.PipelineState`:
    one answers "can I talk to it", the other "is it listening or thinking right
    now". Keeping them apart is what lets the HUD say "语音加载中" instead of going
    blank, and it stops an L5 enum from leaking into L1/L3 contracts.
    """

    OFF = "off"
    """Voice was not requested (no ``--voice``): the microphone is untouched."""

    LOADING = "loading"
    """Models are being pulled in. Takes tens of seconds; the UI must not block."""

    RUNNING = "running"
    """Wake word armed, microphone open."""

    FAILED = "failed"
    """Startup raised. ``detail`` carries the reason."""

    MUTED = "muted"
    """The user let go of the microphone on purpose; engines stay loaded."""


@dataclass(frozen=True, slots=True)
class VoiceStatus:
    """The voice feature's availability, as the UI is told it."""

    phase: VoicePhase
    detail: str = ""
    """Human-readable reason, shown for ``failed`` and anything unusual."""

    keyword: str = ""
    """The configured wake word, so the HUD can print the right thing to say."""

    def to_event(self) -> PipelineEvent:
        """Encode as the one event type the bridge already carries.

        ``detail`` stays a plain string here on purpose: the payload crosses a
        thread boundary and a Python-level object graph would have to be pickled by
        whoever renders it.
        """
        reason = self.detail
        if self.keyword and self.phase in (VoicePhase.RUNNING, VoicePhase.MUTED):
            reason = f"唤醒词：{self.keyword}" + (f"；{reason}" if reason else "")
        return PipelineEvent(kind=VOICE_STATUS_KIND, text=self.phase.value, detail=reason)
