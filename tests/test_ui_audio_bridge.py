"""The audio channel: PCM in, honest levels out.

These tests are the contract the page relies on. Two of them matter more than the
rest:

* **The bytes must survive the trip.** Everything the visualizer shows -- the
  rhythm, and in the next step the avatar's mouth -- is read out of samples the page
  decoded from what this module wrote. If base64 or the slicing loses, reorders or
  resamples a single frame, the picture is not "slightly off", it is measuring
  something that never happened.
* **The default is the speaker.** A page that has not claimed it can play audio gets
  nothing pushed at it, and the chunk goes back to the caller to play out of
  PortAudio. A voice assistant whose answers are swallowed by a half-loaded webview
  is the failure this ordering exists to make impossible.
"""

from __future__ import annotations

import base64
import json
import time
from typing import Any, cast

from jarvis.core.events import PipelineEvent
from jarvis.orchestration.player import BridgePlayer
from jarvis.tts.types import AudioChunk
from jarvis.ui.audio_bridge import ROLE_HUD, ROLE_PET, AudioPusher

_RATE = 24_000
"""Edge-TTS's native rate; every expectation about slice counts follows from it."""

_CHUNK = b"\x01\x02" * 8


def _payload(script: str) -> dict[str, Any]:
    """Pull the JSON object out of the one line of JavaScript we are allowed to run.

    ``window.__jarvisAudio && window.__jarvisAudio({...});`` -- the braces inside a
    base64 payload are impossible, so the outer pair is unambiguous.
    """
    start = script.index("({") + 1
    end = script.rindex("})") + 1
    return cast("dict[str, Any]", json.loads(script[start:end]))


class FakeWebviewWindow:
    """Stand-in for ``webview.Window``: keeps every script, decides how slow it is."""

    def __init__(self, *, delay: float = 0.0, fail_after: int | None = None) -> None:
        self.scripts: list[str] = []
        self._delay = delay
        self._fail_after = fail_after

    def evaluate_js(self, script: str) -> None:
        if self._delay:
            time.sleep(self._delay)
        if self._fail_after is not None and len(self.scripts) >= self._fail_after:
            raise RuntimeError("window vanished")
        self.scripts.append(script)


class Sink:
    """A stand-in speaker, and a counter of how many were ever built."""

    def __init__(self) -> None:
        self.chunks: list[AudioChunk] = []
        self.closed = 0
        self.built = 0

    def factory(self) -> Sink:
        self.built += 1
        return self

    def play(self, chunk: AudioChunk) -> None:
        self.chunks.append(chunk)

    def close(self) -> None:
        self.closed += 1


class Recorder:
    """An ``emit`` callback that keeps what it was handed and always accepts it."""

    def __init__(self) -> None:
        self.calls: list[tuple[bytes, int, bool]] = []

    def __call__(self, pcm: bytes, rate: int, final: bool) -> bool:
        self.calls.append((pcm, rate, final))
        return True


def _ready_pusher(**kwargs: Any) -> tuple[AudioPusher, FakeWebviewWindow]:
    """A pusher whose page has claimed it can play, and the window it pushes to."""
    window = FakeWebviewWindow()
    pusher = AudioPusher(**kwargs)
    pusher.attach_window(window)
    pusher.mark_ready(True)
    return pusher, window


class TestReadinessGate:
    """Nothing crosses the bridge until the page says it can play."""

    def test_a_page_that_never_answered_gets_no_audio(self) -> None:
        pusher = AudioPusher()
        window = FakeWebviewWindow()
        pusher.attach_window(window)

        assert pusher.emit(_CHUNK * 50, _RATE, True) is False
        assert window.scripts == []

    def test_no_window_means_no_push_and_a_refusal(self) -> None:
        pusher = AudioPusher()
        pusher.mark_ready(True)

        assert pusher.emit(_CHUNK * 50, _RATE, True) is False

    def test_a_claim_is_revocable(self) -> None:
        pusher, window = _ready_pusher()
        pusher.mark_ready(False, "音频上下文被浏览器策略拦住了")

        assert pusher.emit(_CHUNK * 50, _RATE, True) is False
        assert window.scripts == []
        assert "浏览器策略" in pusher.detail

    def test_stopping_detaches_the_window(self) -> None:
        """The window goes away before the voice stack does. Pushing into it after
        would be the one place a shutdown could hang."""
        pusher, window = _ready_pusher()
        pusher.stop()

        assert pusher.emit(_CHUNK * 50, _RATE, True) is False
        assert window.scripts == []


class TestTransportIsLossless:
    def test_the_bytes_arrive_unchanged_and_in_order(self) -> None:
        pusher, window = _ready_pusher(slice_seconds=0.01)
        pcm = bytes(range(256)) * 40  # 10 240 bytes, distinctive at every offset

        assert pusher.emit(pcm, _RATE, True) is True

        received = b"".join(base64.b64decode(_payload(s)["pcm"]) for s in window.scripts)
        assert received == pcm

    def test_every_push_but_the_last_is_marked_not_final(self) -> None:
        pusher, window = _ready_pusher(slice_seconds=0.01)

        assert pusher.emit(b"\x02" * 600, _RATE, True) is True

        finals = [_payload(script)["final"] for script in window.scripts]
        assert len(finals) > 1, "a single push means the slicing did not happen"
        assert finals[-1] is True
        assert not any(finals[:-1])

    def test_a_chunk_that_is_not_the_end_of_an_utterance_marks_nothing_final(self) -> None:
        pusher, window = _ready_pusher(slice_seconds=0.001)

        assert pusher.emit(b"\x02" * 100, _RATE, False) is True

        assert all(_payload(script)["final"] is False for script in window.scripts)

    def test_sequence_numbers_increase_across_utterances(self) -> None:
        """The page schedules on arrival order. Two callers -- a spoken turn and a
        read-aloud -- share this channel, so a repeat or a collision is a real bug."""
        pusher, window = _ready_pusher(slice_seconds=0.01)

        pusher.emit(b"\x01" * 600, _RATE, True)
        pusher.emit(b"\x02" * 600, _RATE, True)

        seqs = [_payload(script)["seq"] for script in window.scripts]
        assert seqs == sorted(seqs)
        assert len(set(seqs)) == len(seqs)

    def test_the_sample_rate_travels_with_every_slice(self) -> None:
        """24 kHz from Edge-TTS, 16 kHz from an offline engine. A page that assumed
        one rate would pitch-shift the other without a single word sounding wrong."""
        pusher, window = _ready_pusher()

        assert pusher.emit(b"\x00" * 64, 16_000, True) is True

        assert all(_payload(script)["sample_rate"] == 16_000 for script in window.scripts)

    def test_an_absurd_rate_is_refused_rather_than_encoded(self) -> None:
        pusher, _ = _ready_pusher()

        assert pusher.emit(b"\x00" * 64, 0, True) is False

    def test_silence_is_not_a_push(self) -> None:
        pusher, window = _ready_pusher()

        assert pusher.emit(b"", _RATE, True) is True
        assert window.scripts == []


class TestFailureHandling:
    def test_a_window_that_throws_hands_the_rest_to_the_speaker(self) -> None:
        pusher = AudioPusher()
        pusher.attach_window(FakeWebviewWindow(fail_after=0))
        pusher.mark_ready(True)

        assert pusher.emit(b"\x00" * 400, _RATE, True) is False
        assert pusher.ready is False, "a dead window must not be retried on every chunk"

    def test_a_push_that_takes_too_long_counts_as_a_failure(self) -> None:
        """``evaluate_js`` blocks, so a wedged renderer reaches this code as a slow
        call rather than an exception -- and an uncalled alarm is how the assistant
        ends up silent while the log says everything worked."""
        pusher = AudioPusher(send_alarm=0.01)
        pusher.attach_window(FakeWebviewWindow(delay=0.05))
        pusher.mark_ready(True)

        assert pusher.emit(b"\x00" * 400, _RATE, True) is False
        assert pusher.ready is False

    def test_the_byte_counter_tells_you_audio_really_left(self) -> None:
        """Without this, "the page said it was ready and I heard nothing" has no
        answer at all: you cannot tell a silent bridge from a muted speaker."""
        pusher, _ = _ready_pusher(slice_seconds=0.01)
        before = pusher.sent_bytes

        pusher.emit(b"\x00" * 600, _RATE, True)

        assert pusher.sent_bytes - before == 600


class TestFlush:
    def test_a_wake_word_retracts_what_the_page_already_holds(self) -> None:
        """Barge-in is why ``flush`` exists. By the time the operator starts talking,
        the samples have crossed the bridge and are queued in the page's audio graph;
        cancelling synthesis in Python would still let the assistant finish its
        sentence over the top of the person interrupting it."""
        pusher, window = _ready_pusher()
        pusher.emit(b"\x00" * 400, _RATE, True)
        window.scripts.clear()

        pusher.on_event(PipelineEvent(kind="wake", text="你好小夜"))

        assert len(window.scripts) == 1
        assert _payload(window.scripts[0])["flush"] is True

    def test_barge_in_flushes_too(self) -> None:
        pusher, window = _ready_pusher()
        window.scripts.clear()

        pusher.on_event(PipelineEvent(kind="barge_in"))

        assert _payload(window.scripts[0])["flush"] is True

    def test_an_unrelated_event_sends_nothing(self) -> None:
        """One spoken line per turn, and neither kind of it is an audio cue: flushing on
        those would cut off the answer they are announcing."""
        pusher, window = _ready_pusher()
        window.scripts.clear()

        pusher.on_event(PipelineEvent(kind="user_text", text="现在几点"))
        pusher.on_event(PipelineEvent(kind="reply", text="好的"))
        pusher.on_event(PipelineEvent(kind="state", text="idle"))
        pusher.on_event(PipelineEvent(kind="speech_start"))

        assert window.scripts == []

    def test_a_flush_reaches_no_one_when_the_page_is_not_the_output(self) -> None:
        pusher = AudioPusher()
        pusher.attach_window(FakeWebviewWindow())

        assert pusher.flush() is False


class TestBridgePlayer:
    """The routing decision, which must never come out as silence."""

    def test_a_receiver_that_takes_the_audio_means_no_speaker_at_all(self) -> None:
        taken = Recorder()
        sink = Sink()
        player = BridgePlayer(taken, fallback_factory=sink.factory)
        chunk = AudioChunk(audio=_CHUNK, sample_rate=_RATE, is_final=True)

        player.play(chunk)

        assert taken.calls == [(chunk.audio, _RATE, True)]
        assert sink.built == 0, "a page that works must not pay for a PortAudio stream"

    def test_a_receiver_that_refuses_hands_the_same_bytes_to_the_speaker(self) -> None:
        sink = Sink()
        player = BridgePlayer(
            lambda _pcm, _rate, _final: False,
            fallback_factory=sink.factory,
        )
        chunk = AudioChunk(audio=_CHUNK, sample_rate=_RATE, is_final=True)

        player.play(chunk)

        assert sink.chunks == [chunk]

    def test_a_non_pcm_chunk_is_dropped_on_both_paths(self) -> None:
        """MP3 reaching a player is an engine bug; playing it as PCM is how that bug
        turns into a second of loud noise instead of a line in the log."""
        sink = Sink()
        taken = Recorder()
        player = BridgePlayer(taken, fallback_factory=sink.factory)

        player.play(AudioChunk(audio=b"ID3", sample_rate=_RATE, format="mp3"))

        assert taken.calls == []
        assert sink.chunks == []

    def test_close_releases_only_a_fallback_that_was_built(self) -> None:
        sink = Sink()
        player = BridgePlayer(lambda _p, _r, _f: False, fallback_factory=sink.factory)

        player.close()
        assert sink.closed == 0

        player.play(AudioChunk(audio=_CHUNK, sample_rate=_RATE))
        player.close()
        assert sink.closed == 1


class TestSpectator:
    """The desktop figure is fed the voice in order to *watch* it, not to play it.

    She reads the same samples the speaker plays because her mouth is drawn from them.
    The rule that keeps this honest is that she must never be able to change what the
    loudspeaker does: her window can be hidden, reloaded, or gone entirely, and the
    operator's answer is not allowed to depend on that.
    """

    def test_she_gets_the_same_slices_with_the_speaker_closed(self) -> None:
        pusher, owner = _ready_pusher(slice_seconds=0.01)
        pet = FakeWebviewWindow()
        pusher.attach_spectator(pet)

        assert pusher.emit(_CHUNK * 60, _RATE, True) is True

        assert owner.scripts and pet.scripts
        assert [_payload(s)["pcm"] for s in pet.scripts] == [
            _payload(s)["pcm"] for s in owner.scripts
        ]
        assert all(_payload(s)["mute"] is True for s in pet.scripts)
        assert not any(_payload(s).get("mute") for s in owner.scripts), "one voice, one speaker"

    def test_she_is_fed_even_when_the_page_cannot_play_at_all(self) -> None:
        """The speaker-fallback case is exactly when she would otherwise freeze.

        Nothing audible can come out of a second window -- Python has the chunk and is
        already playing it -- so feeding her silenced samples costs nothing and keeps
        her mouth in time with a voice she is not making.
        """
        pusher = AudioPusher(slice_seconds=0.01)
        pusher.attach_window(FakeWebviewWindow())  # never claims readiness
        pet = FakeWebviewWindow()
        pusher.attach_spectator(pet)

        assert pusher.emit(_CHUNK * 60, _RATE, True) is False, "the caller still owns the speaker"
        assert pet.scripts, "and she still saw every slice"

    def test_a_barge_in_stops_her_too(self) -> None:
        """Samples already queued in her graph would keep her talking over the operator."""
        pusher, _owner = _ready_pusher()
        pet = FakeWebviewWindow()
        pusher.attach_spectator(pet)

        pusher.on_event(PipelineEvent(kind="wake"))
        assert any(_payload(s).get("flush") for s in pet.scripts)

    def test_three_failures_and_one_log_line(self, caplog: Any) -> None:
        """A window that is gone answers every call the same way; do not keep asking."""
        pusher, owner = _ready_pusher(slice_seconds=0.01)
        pet = FakeWebviewWindow(fail_after=0)
        pusher.attach_spectator(pet)

        with caplog.at_level("WARNING"):
            pusher.emit(_CHUNK * 600, _RATE, True)
            pusher.emit(_CHUNK * 600, _RATE, True)

        assert pet.scripts == [], "nothing ever landed, and that is the point"
        assert caplog.text.count("desktop figure") == 1, "one line, not one per slice"
        assert owner.scripts, "the answer itself was never her problem"

    def test_her_failure_leaves_the_answer_alone(self) -> None:
        pusher, owner = _ready_pusher(slice_seconds=0.01)
        pusher.attach_spectator(FakeWebviewWindow(fail_after=0))

        assert pusher.emit(_CHUNK * 600, _RATE, True) is True
        assert owner.scripts

    def test_the_owner_is_never_her_own_spectator(self) -> None:
        """Two copies of one answer in one window is the echo this design prevents."""
        pusher, owner = _ready_pusher(slice_seconds=0.01)
        pusher.attach_spectator(owner)

        assert pusher.emit(_CHUNK * 60, _RATE, True) is True
        assert owner.scripts
        assert not any(_payload(s).get("mute") for s in owner.scripts)


class TestReadinessIsPerWindow:
    """Which page may decide the output device, now that two of them open a graph."""

    def test_only_the_current_loudspeakers_claim_counts(self) -> None:
        pusher = AudioPusher()
        pusher.attach_window(FakeWebviewWindow(), ROLE_HUD)

        pusher.mark_ready(True, "", ROLE_PET)
        assert pusher.ready is False, "she is not the one playing"
        pusher.mark_ready(True, "", ROLE_HUD)
        assert pusher.ready is True

    def test_handing_her_the_output_switches_whose_answer_counts(self) -> None:
        pusher = AudioPusher()
        hud, pet = FakeWebviewWindow(), FakeWebviewWindow()
        pusher.attach_window(hud, ROLE_HUD)
        pusher.mark_ready(True, "", ROLE_HUD)
        pusher.mark_ready(False, "宠物页没有可用的音频上下文", ROLE_PET)

        pusher.attach_window(pet, ROLE_PET)
        assert pusher.ready is False
        assert "宠物页" in pusher.detail

        pusher.attach_window(hud, ROLE_HUD)
        assert pusher.ready is True, "the dashboard's earlier yes still stands"

    def test_a_page_that_has_not_answered_has_not_claimed_anything(self) -> None:
        """Absence of a claim is not a claim -- for either of them."""
        pusher = AudioPusher()
        pet = FakeWebviewWindow()
        pusher.attach_window(pet, ROLE_PET)

        assert pusher.emit(_CHUNK * 60, _RATE, True) is False
        assert pet.scripts == []
