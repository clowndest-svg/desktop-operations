"""Recording the operator's own voice on this machine, so a voice can be made from it.

The phone already does this: the browser opens ``getUserMedia``, and the clip crosses the
LAN as base64 to ``voice_clone_add``. The desktop has no such path -- the microphone here
is owned by Python (the ASR loop that answers the wake word), so a second capture from the
page would either be refused by the device or, worse, quietly share it and steal audio from
the thing that is supposed to hear the wake word. So this records in-process and **the audio
never crosses the bridge**: the clip lives in this one object until it is stored.

Why the recording is a small state machine rather than one blocking ``record(seconds)``
call: the page has to show elapsed time, a level bar and a 取消 button while it happens,
and a bridge method that blocks for thirty seconds freezes the window it was called from.

Two guards that exist because of what failure looks like here:

* **A silent clip is refused.** An empty take (wrong default input, muted OS mixer, nobody
  talking) otherwise produces a stored voice that sounds like a stranger -- and the operator
  only finds out the next time she speaks. The peak is measured from the samples themselves,
  and the reason is said out loud at the moment it is noticed.
* **The clip is kept until the operator saves or discards it.** Recording is the expensive,
  embarrassing half; a rejected name or a missing sentence must not throw the take away,
  which is what makes it worth re-talking rather than giving up.

One door here is a disclosure rather than a mechanism: :meth:`VoiceSampler.save` can send
the clip to the vendor for a cloud clone. It is off by default, ticked once per recording,
and the page says in those words what ticking it means.
"""

from __future__ import annotations

import base64
import logging
import struct
import threading
import time
from collections.abc import Callable
from typing import Any

from jarvis.app import voice_cloud
from jarvis.app.voice_library import COMFORT_MS, VoiceLibrary
from jarvis.app.voice_library import MAX_MS as LIBRARY_MAX_MS
from jarvis.app.voice_library import MIN_MS as LIBRARY_MIN_MS

logger = logging.getLogger("jarvis.app.voice_sample")

SAMPLE_RATE: int = 16_000
"""What :class:`jarvis.app.voice_library.VoiceLibrary` accepts, exactly.

The library refuses any other rate rather than resampling: a silently wrong resample makes
a cloned voice sound like a different person with no error to explain it.
"""

FRAME_MS: float = 32.0
"""How much audio to pull per read. Long frames under-count elapsed time on a fast loop;
short ones cost a syscall. Thirty-two milliseconds is both smooth and cheap."""

DEFAULT_TARGET_MS: float = float(COMFORT_MS[1])
"""Where the page starts nudging ("够了，停吧") rather than a place it stops by itself.

Borrowed from the library's own comfort number so the popup cannot say "15 秒上下最稳" while
its timer insists twelve seconds is already enough. Deliberately not a hard cut: somebody
still mid-sentence would otherwise be truncated, and the clip they lose costs far more than
a few extra seconds of holding the button.
"""

CEILING_MS: float = float(min(LIBRARY_MAX_MS, 30_000))
"""Hard stop. The library would reject a longer clip anyway; stopping here means the
operator learns about the limit while they still have the microphone, not after."""

MIN_MS: float = float(LIBRARY_MIN_MS)
"""Shorter than this and the voice cannot be made. The page is told the same number the
store enforces rather than a second copy that can drift."""

SILENT_PEAK: int = 400
"""Peak absolute sample below which a take counts as silent.

Chosen against 16-bit full scale (32767): 400 is about -38 dBFS, which is below room
noise on a desk microphone while ordinary speech sits an order of magnitude higher. The
point is not to grade audio quality, only to catch "the wrong device was recording".
"""

IDLE = "idle"
RECORDING = "recording"
CAPTURED = "captured"

MAX_TAKE_BYTES = int(CEILING_MS / 1000 * SAMPLE_RATE * 2)
"""30 s at 16 kHz s16le. The buffer stops here even if the thread is slow to notice."""


class VoiceSampler:
    """One recording in flight: start, watch, stop, then save it as a voice or drop it.

    Thread-safe: the capture thread appends while the page's bridge calls read state.
    Only one take exists at a time, which is also the only sane interaction -- two
    overlapping recordings of one microphone are just a corrupted file.
    """

    def __init__(
        self,
        *,
        source_factory: Callable[[], Any],
        library: VoiceLibrary,
        mic_busy: Callable[[], bool] | None = None,
        target_ms: float = DEFAULT_TARGET_MS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Create the recorder.

        Args:
            source_factory: Builds the capture source (``SounddeviceSource`` in the
                desktop, an injected fake in tests). Called once per take, never held.
            library: Where a saved take becomes a voice.
            mic_busy: Whether something else already owns the microphone -- the wake-word
                loop does. Starting a second stream on a live device is the kind of
                failure that shows up as "she stopped hearing me" rather than as an error,
                so this is asked first and the refusal is named.
            clock: Injectable so the cap and the elapsed count are testable without
                waiting in real time.
        """
        self._source_factory = source_factory
        self._library = library
        self._mic_busy = mic_busy or (lambda: False)
        self._target_ms = max(MIN_MS, min(float(target_ms), CEILING_MS))
        self._clock = clock
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._audio = bytearray()
        self._phase = IDLE
        self._error = ""
        self._peak = 0
        self._started_at = 0.0

    # -- the page's four verbs ------------------------------------------------

    def status(self) -> dict[str, Any]:
        """Where the take stands. The page polls this; it never blocks on audio."""
        with self._lock:
            return self._state_locked()

    def _state_locked(self) -> dict[str, Any]:
        """The state, for callers that already hold the lock.

        Split out because ``threading.Lock`` is **not** reentrant: ``start()`` reads this
        while holding the lock, and calling :meth:`status` from there deadlocks the bridge
        thread the second somebody double-clicks 开始录音 -- which is exactly what the
        branch below used to do, and what ``stop()`` and ``save()`` did right after it.
        """
        ms = self._ms_locked()
        return {
            "phase": self._phase,
            "ms": ms,
            "peak": self._peak,
            "silent": self._phase == CAPTURED and self._peak < SILENT_PEAK,
            "error": self._error,
            "target_ms": self._target_ms,
            "min_ms": MIN_MS,
            "max_ms": CEILING_MS,
            "sample_rate": SAMPLE_RATE,
            # The store enforces both of these. A 存成音色 button that is lit for a take it
            # will refuse is a button whose failure the operator only meets after pressing.
            "can_save": self._phase == CAPTURED and self._peak >= SILENT_PEAK and ms >= MIN_MS,
        }

    def start(self) -> dict[str, Any]:
        """Open the microphone and begin capturing. Returns immediately."""
        if self._mic_busy():
            return {
                "phase": IDLE,
                "ok": False,
                "ms": 0.0,
                "peak": 0,
                "error": "麦克风正被聆听占用着——先关掉「聆听」再录，"
                "两边抢一个设备只会两件事都做不好",
            }
        with self._lock:
            if self._phase == RECORDING:
                return {**self._state_locked(), "ok": True, "error": ""}  # idempotent
            if self._phase == CAPTURED:
                return {
                    **self._state_locked(),
                    "ok": False,
                    "error": "上一段还没存也没丢，先处理它（存成音色或放弃）",
                }
            self._audio = bytearray()
            self._peak = 0
            self._error = ""
            self._phase = RECORDING
            self._started_at = self._clock()
            self._stop.clear()
        try:
            source = self._source_factory()
            source.open()
        except Exception as exc:  # a missing/forbidden mic is a fact about the machine
            with self._lock:
                self._phase = IDLE
                self._error = f"麦克风打不开：{exc}"
            logger.exception("could not open the microphone for a voice sample")
            return {**self.status(), "ok": False, "error": self._error}
        self._thread = threading.Thread(
            target=self._pump, args=(source,), name="jarvis-voice-sample", daemon=True
        )
        self._thread.start()
        return {**self.status(), "ok": True, "error": ""}

    def stop(self) -> dict[str, Any]:
        """Close the device and keep the take for review. Empty and short takes are said,
        not stored -- and the buffer stays either way so nothing was thrown away silently."""
        with self._lock:
            if self._phase != RECORDING:
                return {**self._state_locked(), "ok": False, "error": "现在没在录"}
            self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=5.0)
        with self._lock:
            self._thread = None
            self._phase = CAPTURED
            ms = self._ms_locked()
            if ms < MIN_MS:
                self._error = f"太短了（{ms / 1000:.1f} 秒），至少要说 {MIN_MS / 1000:g} 秒"
            elif self._peak < SILENT_PEAK:
                self._error = "这段没听到声音，检查一下是不是选了错的输入设备"
            return {**self._state_locked(), "ok": True}

    def discard(self) -> dict[str, Any]:
        """Throw the take away. Explicit, because re-recording is the normal path."""
        with self._lock:
            self._audio = bytearray()
            self._peak = 0
            self._phase = IDLE
            self._error = ""
        return self.status()

    def save(self, *, name: object, prompt_text: object, upload: bool = False) -> dict[str, Any]:
        """Turn the held take into a stored voice.

        The audio stays in this process unless ``upload`` is on. That switch is the
        operator's own, ticked once per recording, because it means a copy of their voice
        leaves this machine to the vendor -- and because on a machine whose graphics card
        cannot load the offline model, the cloud copy is the only way the answer ever
        actually sounds like them. Store first, then upload: a vendor refusal must degrade
        to a working local voice plus a sentence, never to a lost recording.
        """
        text = str(prompt_text or "").strip()
        if not text:
            return {
                **self.status(),
                "ok": False,
                "error": "要写下你刚才念的那句话（复刻要靠它对得上口型），没填我不存",
            }
        with self._lock:
            if self._phase != CAPTURED:
                return {**self._state_locked(), "ok": False, "error": "先录一段再存"}
            if self._peak < SILENT_PEAK:
                # The store would happily accept it; a voice made from silence is not a
                # voice, and the only way to notice is to listen to her later.
                return {
                    **self._state_locked(),
                    "ok": False,
                    "error": "这段没听到声音，存进去只会得到一个不像你的音色",
                }
            pcm = bytes(self._audio)
        try:
            voice = self._library.add(
                name=str(name or "").strip(),
                pcm=pcm,
                sample_rate=SAMPLE_RATE,
                prompt_text=text,
            )
        except Exception as exc:
            # The clip is kept on purpose: the recording is the awkward half, the field
            # that was wrong is the cheap half to fix.
            logger.exception("could not store the recorded voice")
            return {**self.status(), "ok": False, "error": f"没能存下这段录音：{exc}"}
        with self._lock:
            self._audio = bytearray()
            self._peak = 0
            self._phase = IDLE
            self._error = ""
        logger.info("已把这段录音存成音色：%s", voice.name)
        uploaded = False
        cloud_error = ""
        if upload:
            cloud_error = voice_cloud.upload_voice(
                self._library, voice.voice_id, pcm, SAMPLE_RATE, text
            )
            if not cloud_error:
                uploaded = True
                linked = self._library.resolve(voice.voice_id)
                if linked is not None:
                    # The row has to say 云端 on it, or the page cannot explain why this
                    # voice answers in half a second and the next one takes forty.
                    voice = linked
        return {
            **self.status(),
            "ok": True,
            "error": "",
            "uploaded": uploaded,
            "cloud_error": cloud_error,
            "voice": voice.to_public(),
        }

    # -- internals -----------------------------------------------------------

    def _reached_ceiling(self) -> bool:
        """Whether the buffer is full, so the take has to end whether they liked it or not."""
        with self._lock:
            return len(self._audio) >= MAX_TAKE_BYTES

    def _ms_locked(self) -> float:
        """Elapsed from the samples held, not from the wall clock.

        A wall-clock number would keep running while the device stalls, and the operator
        would be told they have said enough when the buffer says they have not.
        """
        return len(self._audio) / 2 / SAMPLE_RATE * 1000.0

    def _pump(self, source: Any) -> None:
        """Read frames until stopped or capped. Survives a raising device.

        ``AudioSource.read`` counts **samples**, not bytes (16 kHz mono s16le is two bytes
        a sample), while the buffer and the cap are in bytes. Getting that backwards makes
        every elapsed number on screen wrong by half, and the operator is the one who has
        to notice.
        """
        samples = int(SAMPLE_RATE * FRAME_MS / 1000)
        try:
            while not self._stop.is_set():
                try:
                    chunk = source.read(samples)
                except Exception as exc:
                    logger.exception("capture failed mid-take")
                    with self._lock:
                        if self._phase == RECORDING:
                            self._phase = IDLE
                            self._error = f"录到一半设备断了：{exc}"
                    return
                if not chunk:
                    continue
                with self._lock:
                    room = MAX_TAKE_BYTES - len(self._audio)
                    if room > 0:
                        self._audio.extend(chunk[:room])
                    self._peak = max(self._peak, _peak_of(chunk))
                    done = len(self._audio) >= MAX_TAKE_BYTES
                if done:
                    break
            if self._reached_ceiling():
                # The device is about to be closed either way; saying so here is what
                # keeps the page from counting up to 30 秒 and sitting there forever.
                with self._lock:
                    if self._phase == RECORDING:
                        self._phase = CAPTURED
                        self._error = "到最长了，先存下来或者重录"
        finally:
            try:
                source.close()
            except Exception:  # pragma: no cover - nothing left to do about it
                logger.debug("the capture source did not close cleanly", exc_info=True)


def _peak_of(chunk: bytes) -> int:
    """Largest absolute sample in one frame. Cheap enough to run per read."""
    count = len(chunk) // 2
    if count == 0:
        return 0
    samples: tuple[int, ...] = struct.unpack(f"<{count}h", chunk[: count * 2])
    return max(max(samples), -min(samples))


def encode_clip(pcm: bytes) -> str:
    """Base64 for the one place a clip must cross the bridge: a preview the page plays."""
    return base64.b64encode(pcm).decode("ascii")


__all__ = [
    "CAPTURED",
    "CEILING_MS",
    "IDLE",
    "MIN_MS",
    "RECORDING",
    "SAMPLE_RATE",
    "SILENT_PEAK",
    "VoiceSampler",
    "encode_clip",
]
