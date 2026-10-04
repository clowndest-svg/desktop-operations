"""Speech out of Python and into the page, so the visuals can measure it.

Why the audio crosses the bridge at all
---------------------------------------
The voice stack used to play every answer through PortAudio on the Python side.
That works, and it is also the reason the HUD could not draw anything honest
while the assistant talked: the samples went straight to the sound card and never
passed anywhere the page could observe. ``VoiceCore`` said so in its own docstring
-- no level, no waveform, because a lively wave drawn from a state machine is a
lie about listening in the one place the operator is watching.

Pushing PCM into the page fixes both halves at once. The page plays it, so the
same signal that reaches the speaker also reaches an ``AnalyserNode``: the rhythm
and the avatar's mouth cannot drift out of sync with the voice, because there is
exactly one voice and they are all reading it.

Why base64 over ``evaluate_js`` rather than a URL the page fetches
------------------------------------------------------------------
A loopback HTTP endpoint would be tidier -- no encoding, no size questions -- but
it only exists while that server does, and the page must be served from the same
origin to fetch it. This module has to work whether the window was opened from
``file://`` or over HTTP, and ``evaluate_js`` is the one channel pywebview
guarantees in both. So: base64, sliced, at the cost of a few kilobytes per push.

Failure posture
---------------
The page has to *claim* it can play audio before any of it is sent here
(:meth:`mark_ready`). Until it does, every chunk is refused and the caller plays
it out of the speaker instead. Silence is the worst possible failure for a voice
assistant, and "the webview was not ready yet" is a real state -- so the default
here is the thing that cannot break, not the new thing.
"""

from __future__ import annotations

import base64
import json
import logging
import time
from typing import Any

logger = logging.getLogger("jarvis.ui.audio_bridge")

AUDIO_SCRIPT = "window.__jarvisAudio && window.__jarvisAudio({payload});"
"""The single entry point the bundle must expose.

Optional chaining on the callee rather than a throw: a page that has not defined
``__jarvisAudio`` yet is a page still loading, and the answer belongs on the
speaker, not in a Python stack trace.
"""

BYTES_PER_SAMPLE = 2
"""One mono s16le sample. Everything about slicing follows from this."""

SLICE_SECONDS: float = 0.5
"""How much audio goes into one push.

Small enough that playback starts on the first slice, large enough that a
thirty-second answer costs about sixty bridge calls instead of six hundred.
"""

SEND_ALARM_SECONDS: float = 2.0
"""A push slower than this means the page is not keeping up.

``evaluate_js`` blocks until the browser answers, so a wedged renderer shows up
here as a slow call rather than an exception. The call cannot be cancelled, but it
can be *reported* and the rest of the utterance handed to the speaker -- see
:meth:`AudioPusher.emit`.
"""


_FLUSH_KINDS = frozenset({"wake", "barge_in"})
"""Pipeline events that mean "the user took over"; see :meth:`AudioPusher.on_event`."""

SPECTATOR_GIVE_UP = 3
"""Consecutive failures before the desktop figure stops being fed levels.

Her mouth is not worth a log line per slice, and a window that has been destroyed
answers every call the same way. Three is the shortest count that cannot be a
one-off hiccup at the exact moment a page reloads.
"""

ROLE_HUD = "hud"
ROLE_PET = "pet"
"""Which page a readiness claim came from. See :meth:`AudioPusher.mark_ready`."""


class AudioPusher:
    """Encodes synthesized PCM and hands it to the desktop window.

    One instance is created by the desktop composition root and lives as long as
    the process. It is shared by both ways an answer gets spoken -- the
    microphone's own turn and a typed reply read aloud -- which is the reason it
    is an object rather than a function: the two callers are different threads and
    must not interleave halves of different utterances.
    """

    def __init__(
        self,
        *,
        slice_seconds: float = SLICE_SECONDS,
        send_alarm: float = SEND_ALARM_SECONDS,
    ) -> None:
        self._slice_seconds = slice_seconds
        self._send_alarm = send_alarm
        self._window: Any | None = None
        self._role = ROLE_HUD
        self._claims: dict[str, bool] = {}
        self._reasons: dict[str, str] = {}
        self._spectator: Any | None = None
        self._spectator_misses = 0
        self._seq = 0
        self._sent_bytes = 0

    # ------------------------------------------------------------------
    # What the page and the shell tell us
    # ------------------------------------------------------------------

    def attach_window(self, window: Any | None, role: str = ROLE_HUD) -> None:
        """Point at the window to push into, or away from it when it is gone.

        ``role`` says *which* page it is, because that is whose claim counts: the
        dashboard and the desktop figure each open an audio channel, and only the one
        being fed the sound may decide whether the output is the browser or the
        speaker. See :meth:`mark_ready`.
        """
        self._window = window
        self._role = role if window is not None else ROLE_HUD

    def attach_spectator(self, window: Any | None) -> None:
        """Point at the window that has to *watch* the voice without playing it.

        That is the desktop figure while the HUD is on screen: exactly one window may
        make sound or the operator hears the answer twice, slightly out of time -- but
        the figure's mouth reads the same samples, and a page that receives nothing
        draws a still image. So she gets the identical slices with ``mute`` set, and the
        page closes its own gain while leaving the analyser in the path.

        Passing the owner in here is refused by the caller (the shell owns that rule);
        passing it out here means "there is no second window", which is the ordinary
        case when both windows are on the same screen or the pet is hidden.
        """
        if window is self._window:
            window = None
        self._spectator = window
        self._spectator_misses = 0

    def mark_ready(self, ok: bool, reason: str = "", role: str | None = None) -> None:
        """Record the page's own verdict on whether it can play audio.

        Called from the bridge, so ``ok`` arrives as whatever JavaScript decided
        after trying to build an ``AudioContext`` and resume it. A page that has
        not answered is not ready: absence of a claim is not a claim.

        The reason is kept even when the verdict did not change -- a page that
        reloads and re-states the same refusal is still the source of the sentence
        the HUD shows next to 「本机扬声器输出」, and a repeated answer is the common
        case, not the interesting one.

        A claim from the window that is *not* currently the loudspeaker is stored and
        otherwise ignored. Without that split the desktop figure's cheerful "I can play"
        would overrule the dashboard's "I cannot", and the assistant would be silent
        with every indicator green -- the failure this file exists to avoid.
        """
        claimed = bool(ok)
        wanted = str(role or self._role)
        was_ready = self.ready
        self._claims[wanted] = claimed
        self._reasons[wanted] = "" if claimed else (reason or "页面没有声明它可以播放音频")
        if wanted != self._role:
            # Her refusal is *recorded* and does not reroute the sound. Without the
            # record, handing her the output later would leave the screen explaining
            # nothing about a page that had already said why it could not play.
            logger.debug("audio readiness claimed by %s, which is not the output", wanted)
            return
        if was_ready is not claimed:
            logger.info(
                "desktop audio output switched to %s%s",
                "browser" if claimed else "speaker",
                "" if claimed else f" ({self._reasons[wanted]})",
            )

    @property
    def ready(self) -> bool:
        """Whether the current output window may be sent audio."""
        return self._window is not None and self._claims.get(self._role, False)

    @property
    def detail(self) -> str:
        """Why the page is not the output device; empty when it is."""
        if self.ready:
            return ""
        return self._reasons.get(self._role) or "页面没有声明它可以播放音频"

    @property
    def sent_bytes(self) -> int:
        """PCM bytes handed to the page since this process started.

        Not a quality metric the HUD plots -- it is what makes "the bridge said it
        was ready and nothing arrived" answerable from the log instead of by
        guesswork, which is the difference between a bug and a mystery.
        """
        return self._sent_bytes

    # ------------------------------------------------------------------
    # Pipeline events
    # ------------------------------------------------------------------

    def on_event(self, event: object) -> None:
        """Retract the audio already given to the page when the user takes over.

        Barge-in is the reason this exists. By the time a sentence is playing, its
        samples have crossed the bridge and are queued in the page's audio graph --
        cancelling synthesis in Python would leave the assistant talking over the
        person who just interrupted it. The duck-typed ``kind`` read is also why
        this can sit in the web layer at all: L5 may not import L3's event types.
        """
        kind = getattr(event, "kind", None)
        if isinstance(kind, str) and kind in _FLUSH_KINDS:
            self.flush()

    def flush(self) -> bool:
        """Tell the page to drop everything it has not played yet."""
        # The spectator first and unconditionally: it may be playing a silenced copy of
        # something the owner has already thrown away, and her mouth has to stop when
        # the voice does -- that is the entire point of measuring the same signal.
        self._watch({"flush": True})
        window = self._window
        if not self.ready or window is None:
            return False
        evaluate = getattr(window, "evaluate_js", None)
        if not callable(evaluate):  # pragma: no cover - window gone mid-call
            return False
        payload = {"flush": True, "seq": self._seq}
        self._seq += 1
        try:
            evaluate(AUDIO_SCRIPT.format(payload=json.dumps(payload, separators=(",", ":"))))
        except Exception:
            logger.exception("audio flush did not reach the page")
            return False
        return True

    # ------------------------------------------------------------------
    # The sink handed to the audio player
    # ------------------------------------------------------------------

    def emit(self, pcm: bytes, sample_rate: int, is_final: bool) -> bool:
        """Send one chunk of s16le mono PCM to the page.

        Returns ``True`` only when every slice reached the window. ``False`` means
        "this chunk is yours again" and the caller must play it out of the speaker
        -- which is also why the slices are sent before anything is committed: a
        half-delivered utterance is the one outcome worse than none.

        The spectator gets the same slices whether the owner is reachable or not. A
        page that cannot play sound is a real state, and it is precisely the state in
        which the desktop figure still has to move her mouth.
        """
        window = self._window
        if sample_rate <= 0:
            logger.error("refusing to push audio with sample rate %s", sample_rate)
            return False
        if not pcm:
            return True
        frame = int(sample_rate * self._slice_seconds) * BYTES_PER_SAMPLE
        frame -= frame % BYTES_PER_SAMPLE
        frame = max(BYTES_PER_SAMPLE, frame)
        owner = self.ready
        total = len(pcm)
        for offset in range(0, total, frame):
            last = offset + frame >= total
            payload = pcm[offset : offset + frame]
            final = last and is_final
            self._watch(
                {
                    "sample_rate": sample_rate,
                    "channels": 1,
                    "format": "pcm_s16le",
                    "final": final,
                    "pcm": base64.b64encode(payload).decode("ascii"),
                }
            )
            if not owner:
                continue
            if not self._send(window, payload, sample_rate, final):
                self.mark_ready(False, "页面接不住音频，已改回本机扬声器")
                return False
        return owner

    def _watch(self, message: dict[str, object]) -> None:
        """One message to the spectator, if there is one. Best effort, never raises.

        Nothing here may change what the owner's audio does: a desktop figure that
        cannot keep up costs the operator a still picture, while an answer that does
        not reach the speaker costs them the answer.
        """
        window = self._spectator
        if window is None or self._spectator_misses >= SPECTATOR_GIVE_UP:
            return
        evaluate = getattr(window, "evaluate_js", None)
        if not callable(evaluate):
            self._detach_spectator("窗口已经不接电话")
            return
        body = dict(message)
        body.setdefault("mute", True)
        body["seq"] = self._seq
        self._seq += 1
        try:
            evaluate(AUDIO_SCRIPT.format(payload=json.dumps(body, separators=(",", ":"))))
        except Exception as exc:
            self._spectator_misses += 1
            if self._spectator_misses >= SPECTATOR_GIVE_UP:
                self._detach_spectator(str(exc))
            return
        self._spectator_misses = 0

    def _detach_spectator(self, reason: str) -> None:
        """Say it once, in the log, and stop paying for a window that is not there."""
        self._spectator = None
        self._spectator_misses = 0
        logger.warning("stopped feeding the desktop figure's audio: %s", reason)

    def _send(self, window: Any, pcm: bytes, sample_rate: int, final: bool) -> bool:
        """Push one slice. Never raises: a dead window must not reach the pipeline."""
        evaluate = getattr(window, "evaluate_js", None)
        if not callable(evaluate):  # pragma: no cover - window torn down mid-call
            return False
        payload = {
            "seq": self._seq,
            "sample_rate": sample_rate,
            "channels": 1,
            "format": "pcm_s16le",
            "final": final,
            "pcm": base64.b64encode(pcm).decode("ascii"),
        }
        self._seq += 1
        script = AUDIO_SCRIPT.format(payload=json.dumps(payload, separators=(",", ":")))
        started = time.monotonic()
        try:
            evaluate(script)
        except Exception:
            logger.exception("audio push failed; falling back to the speaker")
            return False
        elapsed = time.monotonic() - started
        self._sent_bytes += len(pcm)
        if elapsed > self._send_alarm:
            logger.warning(
                "audio push took %.2fs (page is wedged or busy); seq=%s",
                elapsed,
                payload["seq"],
            )
            return False
        if final:
            # One line per utterance, and only on success. Without it, "the assistant
            # was silent" and "the assistant spoke and the operator had the volume
            # down" are indistinguishable in the log -- and they are different bugs
            # in different halves of the system.
            logger.info(
                "audio: %d bytes in %d slices delivered to the page",
                self._sent_bytes,
                self._seq,
            )
        return True

    def stop(self) -> None:
        """Detach both windows. Safe to call twice; nothing else to release."""
        self._window = None
        self._spectator = None
        self._spectator_misses = 0
        self._role = ROLE_HUD
        self._claims.clear()
        self._reasons.clear()


__all__ = ["AUDIO_SCRIPT", "AudioPusher"]
