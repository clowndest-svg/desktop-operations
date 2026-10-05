"""The desktop voice sampler: recording a sample of the operator, in this process.

The point of these tests is not that bytes move; it is that a take which cannot make a
good voice is *said* at the moment it is noticed (mic busy, silence, too short) rather than
stored quietly and discovered three days later when she answers in a stranger's voice.

Everything real is kept real: :class:`VoiceLibrary` writes into a ``tmp_path`` and the
resulting WAV is checked on disk. Only the microphone and the clock are faked -- the first
because a test cannot hold a device, the second because thirty seconds of cap is not
something a test should wait for.

Anything that touches the sampler's lock from the test thread runs through
:func:`_with_deadline`. Every verb here takes a non-reentrant lock, so a re-entrant call
would show up as a suite that never finishes rather than as a failure -- and that has
already happened twice in this file's first draft.
"""

from __future__ import annotations

import struct
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from jarvis.app.voice_library import VoiceLibrary
from jarvis.app.voice_sample import (
    DEFAULT_TARGET_MS,
    MAX_TAKE_BYTES,
    SAMPLE_RATE,
    SILENT_PEAK,
    VoiceSampler,
)

FRAME_SAMPLES = 512
"""One 32 ms frame at 16 kHz. The sampler asks for this many samples per read."""

FRAME_MS = 32.0
"""How long that frame is, in **milliseconds** -- the unit every ``ms`` field uses.

Written out because the first draft of this file computed "how much audio is left" as
``frames * 0.032`` and compared it to a millisecond counter, which let the fixture return
after a single frame. The tests then passed or failed on thread timing, not on behaviour.
"""

GOOD_FRAMES = 100
"""3.2 秒 of audio: over the store's 2 秒 floor, under its 30 秒 ceiling."""


def tone(samples: int, amplitude: int = 6000) -> bytes:
    """A short sine-ish burst with a known peak, so the level guard has something to see."""
    values = [int(amplitude * ((sample % 16) - 8) / 8) for sample in range(samples)]
    return struct.pack(f"<{len(values)}h", *values)


def silence(samples: int) -> bytes:
    """A frame with the right shape and nothing in it -- the wrong-input-device take."""
    return b"\x00\x00" * samples


class _Source:
    """A microphone that hands out scripted frames and then idles."""

    def __init__(self, frames: list[bytes] | None = None, *, raise_on_open: str = "") -> None:
        self.frames = list(frames or [])
        self.opened = 0
        self.closed = 0
        self.raise_on_open = raise_on_open
        self.raise_after = -1
        self.reads = 0

    def open(self) -> None:
        self.opened += 1
        if self.raise_on_open:
            raise RuntimeError(self.raise_on_open)

    def read(self, samples: int) -> bytes:
        del samples
        self.reads += 1
        if self.reads == self.raise_after:
            raise RuntimeError("设备断了")
        if self.frames:
            return self.frames.pop(0)
        time.sleep(0.002)
        return b""

    def close(self) -> None:
        self.closed += 1


def _sampler(
    tmp_path: Path,
    source: _Source,
    **overrides: Any,
) -> VoiceSampler:
    library = VoiceLibrary(tmp_path / "voices")
    library.start()
    options: dict[str, Any] = {
        "source_factory": lambda: source,
        "library": library,
        "clock": lambda: time.monotonic(),
    }
    options.update(overrides)
    return VoiceSampler(**options)


def _with_deadline(action: Callable[[], Any], *, seconds: float = 5.0) -> Any:
    """Run a verb on its own thread, so a self-deadlock fails instead of hanging the suite.

    A hanging suite is worse than a failing one: the next person lowers the timeouts until
    the test stops being evidence.
    """
    answers: list[Any] = []
    failures: list[BaseException] = []

    def run() -> None:
        try:
            answers.append(action())
        except BaseException as exc:  # re-raised in the caller, so the assert reads there
            failures.append(exc)

    caller = threading.Thread(target=run, daemon=True)
    caller.start()
    caller.join(timeout=seconds)
    assert not caller.is_alive(), "调用卡在自己持有的锁上——这个方法里 status() 套了一层锁"
    if failures:
        raise failures[0]
    return answers[0]


def _wait_until(sampler: VoiceSampler, predicate: Callable[[dict[str, Any]], bool]) -> None:
    """Spin until the capture thread got there. Five seconds is minutes in this loop."""
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        if predicate(sampler.status()):
            return
        time.sleep(0.004)
    raise AssertionError(f"采集线程没走到那一步：{sampler.status()}")


def _recorded(
    tmp_path: Path, frames: list[bytes], **overrides: Any
) -> tuple[VoiceSampler, _Source]:
    """A sampler with a full take already behind it -- the setup nine of these tests need."""
    source = _Source(frames)
    sampler = _sampler(tmp_path, source, **overrides)
    assert sampler.start()["ok"] is True
    # The buffer stops at the ceiling however many frames the fake still owes.
    owed = min(len(frames) * FRAME_MS, 30_000.0)
    _wait_until(sampler, lambda state: state["ms"] >= owed)
    return sampler, source


class TestStartingAndStopping:
    def test_a_busy_microphone_is_refused_before_it_fights_the_wake_word(
        self, tmp_path: Path
    ) -> None:
        """The failure being prevented is "she stopped hearing me", with no error anywhere."""
        source = _Source()
        sampler = _sampler(tmp_path, source, mic_busy=lambda: True)

        result = sampler.start()

        assert result["ok"] is False
        assert "聆听" in result["error"]
        assert source.opened == 0, "拒了就不该碰设备"

    def test_a_double_click_on_start_answers_instead_of_deadlocking(self, tmp_path: Path) -> None:
        """The second start reads the state *while the lock is held* -- a self-deadlock.

        Asserted from another thread with a deadline, because a test that simply called
        ``start()`` twice would hang the whole suite instead of failing.
        """
        source = _Source()
        sampler = _sampler(tmp_path, source)
        assert sampler.start()["ok"] is True

        answered = _with_deadline(sampler.start)

        assert answered["ok"] is True
        assert source.opened == 1, "两条采集流抢一个输入，只会两件事都做不好"

    def test_stopping_when_nothing_is_running_answers_instead_of_deadlocking(
        self, tmp_path: Path
    ) -> None:
        """The page can send 停止 twice, or after the take ended by itself."""
        sampler = _sampler(tmp_path, _Source())

        answered = _with_deadline(sampler.stop)

        assert answered["ok"] is False
        assert "现在没在录" in answered["error"]

    def test_saving_a_take_that_was_never_recorded_answers_instead_of_deadlocking(
        self, tmp_path: Path
    ) -> None:
        sampler = _sampler(tmp_path, _Source())

        answered = _with_deadline(lambda: sampler.save(name="我的声音", prompt_text="一句话"))

        assert answered["ok"] is False
        assert "先录一段再存" in answered["error"]

    def test_a_device_that_refuses_to_open_says_so_and_leaves_the_state_clean(
        self, tmp_path: Path
    ) -> None:
        source = _Source(raise_on_open="no device")
        sampler = _sampler(tmp_path, source)

        result = sampler.start()

        assert result["ok"] is False
        assert "麦克风打不开" in result["error"]
        assert sampler.status()["phase"] == "idle"

    def test_a_device_dying_mid_take_is_reported_rather_than_hanging(self, tmp_path: Path) -> None:
        source = _Source([tone(FRAME_SAMPLES)])
        source.raise_after = 3
        sampler = _sampler(tmp_path, source)

        assert sampler.start()["ok"] is True
        _wait_until(sampler, lambda state: state["phase"] != "recording")

        status = sampler.status()
        assert status["phase"] == "idle"
        assert "设备断了" in status["error"]

    def test_stopping_keeps_the_take_and_closes_the_device(self, tmp_path: Path) -> None:
        sampler, source = _recorded(tmp_path, [tone(FRAME_SAMPLES)] * GOOD_FRAMES)

        stopped = sampler.stop()

        assert stopped["ok"] is True
        assert stopped["phase"] == "captured"
        assert stopped["ms"] == pytest.approx(GOOD_FRAMES * 32.0)
        assert source.closed == 1

    def test_a_second_start_while_a_take_awaits_its_verdict_is_refused(
        self, tmp_path: Path
    ) -> None:
        """Otherwise the held take vanishes the moment somebody reaches for the button."""
        sampler, _ = _recorded(tmp_path, [tone(FRAME_SAMPLES)] * GOOD_FRAMES)
        sampler.stop()

        again = sampler.start()

        assert again["ok"] is False
        assert "还没存" in again["error"]
        assert sampler.status()["ms"] > 0, "拒了不该顺手把这段清了"


class TestTheGuardsThatSaveSomebodyAnHour:
    def test_elapsed_comes_from_the_samples_not_the_wall_clock(self, tmp_path: Path) -> None:
        """The operator is told how much they said, not how long they sat there.

        A clock-based number would keep growing while the device stalls, and the page
        would cheerfully say "12 秒，够了" about two seconds of audio.
        """
        clock = [0.0]
        source = _Source([tone(FRAME_SAMPLES)])
        sampler = _sampler(tmp_path, source, clock=lambda: clock[0])
        sampler.start()
        _wait_until(sampler, lambda state: state["ms"] > 0)
        clock[0] = 900.0

        status = sampler.status()

        assert status["ms"] == pytest.approx(32.0), "一帧 512 样本 @16k 就是 32 毫秒"

    def test_passing_the_nudge_mark_does_not_end_the_take(self, tmp_path: Path) -> None:
        """target_ms is where the page says "够了", not where the audio is cut.

        Stopping a take mid-sentence truncates whoever is still talking, and the clip they
        lose is the thing that costs an hour to redo.
        """
        frames = [tone(FRAME_SAMPLES) for _ in range(int(DEFAULT_TARGET_MS / FRAME_MS) + 40)]
        sampler, source = _recorded(tmp_path, frames)

        state = sampler.status()
        assert state["phase"] == "recording"
        assert state["ms"] > state["target_ms"]
        assert source.closed == 0

    def test_the_nudge_is_the_store_comfort_number_not_a_second_one(self, tmp_path: Path) -> None:
        """The popup says "15 秒上下最稳" from the library; the timer must agree with it."""
        from jarvis.app.voice_library import COMFORT_MS

        sampler = _sampler(tmp_path, _Source())

        assert sampler.status()["target_ms"] == float(COMFORT_MS[1])

    def test_a_silent_take_is_named_at_the_moment_it_ends(self, tmp_path: Path) -> None:
        source = _sampler(tmp_path, _Source([silence(FRAME_SAMPLES)] * GOOD_FRAMES))
        assert source.start()["ok"] is True
        _wait_until(source, lambda state: state["ms"] > 2_000)

        stopped = source.stop()

        assert stopped["silent"] is True
        assert "没听到声音" in stopped["error"]

    def test_a_silent_take_cannot_be_saved_even_when_a_sentence_is_offered(
        self, tmp_path: Path
    ) -> None:
        """The store would accept it. That is exactly the problem."""
        sampler, _ = _recorded(tmp_path, [silence(FRAME_SAMPLES)] * GOOD_FRAMES)
        sampler.stop()

        saved = _with_deadline(lambda: sampler.save(name="我的声音", prompt_text="今天天气不错"))

        assert saved["ok"] is False
        assert "不像你" in saved["error"]
        assert sampler.status()["phase"] == "captured", "拒了不许把录音顺手扔掉"

    def test_a_missing_sentence_refuses_the_save_and_keeps_the_take(self, tmp_path: Path) -> None:
        """Recording is the awkward half; a blank field must not throw it away."""
        sampler, _ = _recorded(tmp_path, [tone(FRAME_SAMPLES)] * GOOD_FRAMES)
        sampler.stop()

        saved = sampler.save(name="我的声音", prompt_text="   ")

        assert saved["ok"] is False
        assert "那句话" in saved["error"]
        assert sampler.status()["phase"] == "captured"

    def test_a_take_shorter_than_the_store_floor_is_said_not_stored(self, tmp_path: Path) -> None:
        """Loud enough to pass the silence guard, too short to make a voice."""
        sampler, _ = _recorded(tmp_path, [tone(FRAME_SAMPLES)] * 20)

        stopped = sampler.stop()

        assert "太短了" in stopped["error"]
        assert stopped["can_save"] is False

    def test_a_take_that_hits_the_ceiling_ends_itself_and_says_so(self, tmp_path: Path) -> None:
        """30 seconds is the store's limit. Stopping there must also *finish* the take.

        Closing the device without moving the state would leave the page counting up to
        30 秒 forever, with no 停止 button left to press and nothing on the error line.
        """
        sampler, source = _recorded(tmp_path, [tone(FRAME_SAMPLES) for _ in range(2000)])
        _wait_until(sampler, lambda state: state["phase"] != "recording")

        state = sampler.status()
        assert state["phase"] == "captured", "录满了要自己收尾，不能把人挂在录音中"
        assert state["ms"] <= 30_000 + 100
        assert "最长" in state["error"]
        assert source.closed == 1
        ceiling_bytes = 30 * SAMPLE_RATE * 2
        assert ceiling_bytes == MAX_TAKE_BYTES, "三十秒的硬顶就是仓库的上限"


class TestSavingMakesARealVoice:
    def test_a_good_take_becomes_a_readable_wav_in_the_library(self, tmp_path: Path) -> None:
        sampler, _ = _recorded(tmp_path, [tone(FRAME_SAMPLES)] * GOOD_FRAMES)
        sampler.stop()

        saved = sampler.save(name="我的声音", prompt_text="今天天气不错，我念一句给你听")

        assert saved["ok"] is True
        voice = saved["voice"]
        assert voice["kind"] == "clone"
        assert voice["duration_ms"] > 0
        assert saved["phase"] == "idle"
        listing = sampler._library.voices()  # the store is the point
        assert [entry.voice_id for entry in listing] == [voice["id"]]
        assert listing[0].prompt_text == "今天天气不错，我念一句给你听"
        assert Path(listing[0].ref_path).read_bytes()[:4] == b"RIFF"

    def test_discarding_forgets_the_take_without_touching_the_store(self, tmp_path: Path) -> None:
        sampler, _ = _recorded(tmp_path, [tone(FRAME_SAMPLES)] * GOOD_FRAMES)
        sampler.stop()

        state = sampler.discard()

        assert state["phase"] == "idle"
        assert state["ms"] == 0
        assert sampler._library.voices() == []

    def test_a_silent_take_can_be_thrown_away_as_easily_as_a_good_one(self, tmp_path: Path) -> None:
        """The way out of a refused take has to be as cheap as the refusal."""
        sampler, _ = _recorded(tmp_path, [silence(FRAME_SAMPLES)] * GOOD_FRAMES)
        sampler.stop()

        assert sampler.discard()["phase"] == "idle"

    def test_the_peak_floor_is_looser_than_speech_and_tighter_than_noise(
        self, tmp_path: Path
    ) -> None:
        """The number that guards "wrong input device" must not grade audio quality."""
        assert SILENT_PEAK < 2000, "普通说话远高于这个值，别把安静的房间当静音"
        sampler, _ = _recorded(
            tmp_path, [tone(FRAME_SAMPLES, amplitude=SILENT_PEAK + 50)] * GOOD_FRAMES
        )

        assert sampler.stop()["silent"] is False

    def test_a_store_that_refuses_says_so_and_keeps_the_take(self, tmp_path: Path) -> None:
        """A name the library dislikes must not cost the operator another recording."""
        sampler, _ = _recorded(tmp_path, [tone(FRAME_SAMPLES)] * GOOD_FRAMES)
        sampler.stop()

        saved = sampler.save(name="   ", prompt_text="今天天气不错")

        assert saved["ok"] is False
        assert "没能存下" in saved["error"]
        assert sampler.status()["phase"] == "captured"


class TestTheCloudHalf:
    """``upload`` is a disclosure, so it is off by default and its failure is not fatal."""

    @staticmethod
    def _install(monkeypatch: pytest.MonkeyPatch, answer: str = "") -> list[tuple[Any, ...]]:
        """Replace the vendor call with a recorder of what it was handed."""
        seen: list[tuple[Any, ...]] = []

        def fake(library: Any, voice_id: str, audio: bytes, rate: int, text: object) -> str:
            seen.append((library, voice_id, audio, rate, text))
            if not answer and library is not None:
                # The real seam links the vendor id back onto the stored row.
                library.link_cloud(voice_id, cloud_voice="cosyvoice-up-1", cloud_model="m")
            return answer

        monkeypatch.setattr("jarvis.app.voice_cloud.upload_voice", fake)
        return seen

    def test_nobody_ticked_it_so_nothing_left_the_machine(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen = self._install(monkeypatch)
        sampler, _ = _recorded(tmp_path, [tone(FRAME_SAMPLES)] * GOOD_FRAMES)
        sampler.stop()

        saved = sampler.save(name="我的声音", prompt_text="今天天气不错")

        assert saved["ok"] is True
        assert seen == [], "默认必须是只存本机"
        assert saved["uploaded"] is False

    def test_the_clip_that_leaves_is_the_clip_that_was_stored(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Not a second copy, not a resample: the bytes the operator just recorded."""
        import wave

        seen = self._install(monkeypatch)
        sampler, _ = _recorded(tmp_path, [tone(FRAME_SAMPLES)] * GOOD_FRAMES)
        sampler.stop()

        saved = sampler.save(name="我的声音", prompt_text="今天天气不错", upload=True)

        assert saved["uploaded"] is True
        library, voice_id, audio, rate, text = seen[0]
        entry = next(row for row in library.voices() if row.voice_id == voice_id)
        assert text == "今天天气不错"
        assert rate == SAMPLE_RATE
        with wave.open(str(entry.ref_path), "rb") as handle:
            assert handle.readframes(handle.getnframes()) == audio

    def test_a_refused_upload_still_leaves_a_working_local_voice(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The recording is the expensive half; a vendor "no" must not cost it."""
        self._install(monkeypatch, answer="没设 DASHSCOPE_API_KEY")
        sampler, _ = _recorded(tmp_path, [tone(FRAME_SAMPLES)] * GOOD_FRAMES)
        sampler.stop()

        saved = sampler.save(name="我的声音", prompt_text="今天天气不错", upload=True)

        assert saved["ok"] is True
        assert saved["uploaded"] is False
        assert "没设 DASHSCOPE_API_KEY" in saved["cloud_error"]
        assert len(sampler._library.voices()) == 1

    def test_an_uploaded_voice_is_marked_so_the_page_can_say_why_it_answers_fast(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``cloud`` decides whether 试听 takes half a second or forty; the row must carry it."""
        self._install(monkeypatch)
        sampler, _ = _recorded(tmp_path, [tone(FRAME_SAMPLES)] * GOOD_FRAMES)
        sampler.stop()

        saved = sampler.save(name="我的声音", prompt_text="今天天气不错", upload=True)

        assert saved["voice"]["cloud"] is True
        assert saved["voice"]["cloud_model"] == "m"


class TestTheBridgeSurface:
    """The page's five verbs, including what they say when nothing is behind them."""

    @staticmethod
    def _picker(tmp_path: Path, library: VoiceLibrary) -> Any:
        """A real picker around the sampler's store.

        ``voice_clone_list`` reads the picker rather than the library, so a bridge built
        without one answers 「语音选择不可用」 -- a real state, just not the one under test.
        """
        from jarvis.app.preferences import Preferences
        from jarvis.app.voice_picker import VoicePicker
        from jarvis.config.schema import TtsSection

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
            Preferences(tmp_path / "preferences.json"),
            library=library,
        )

    @classmethod
    def _bridge(cls, tmp_path: Path, sampler: Any = None) -> Any:
        from jarvis.app.system_service import SystemService
        from jarvis.ui.desktop import HudBridge

        class _NoTelemetry:
            def __call__(self) -> Any:
                raise AssertionError("这几个调用不该读遥测")

        picker: Any = None
        if sampler is not None:
            picker = cls._picker(tmp_path, sampler._library)
        return HudBridge(
            SystemService(_NoTelemetry()),
            _disk_service(tmp_path),
            voice_picker=picker,
            voice_sample=sampler,
        )

    def test_a_process_without_a_recorder_says_so_on_every_verb(self, tmp_path: Path) -> None:
        bridge = self._bridge(tmp_path)

        for result in (
            bridge.voice_sample_start(),
            bridge.voice_sample_status(),
            bridge.voice_sample_stop(),
            bridge.voice_sample_discard(),
            bridge.voice_sample_save(name="a", prompt_text="b"),
        ):
            assert "没有接录音" in str(result.get("error"))

    def test_the_page_sees_the_same_state_the_sampler_holds(self, tmp_path: Path) -> None:
        sampler, _ = _recorded(tmp_path, [tone(FRAME_SAMPLES)] * GOOD_FRAMES)
        bridge = self._bridge(tmp_path, sampler)

        status = bridge.voice_sample_status()

        assert status["phase"] == "recording"
        assert status["max_ms"] == 30_000
        stopped = bridge.voice_sample_stop()
        assert stopped["phase"] == "captured"
        assert stopped["can_save"] is True

    def test_a_saved_take_comes_back_with_the_list_so_the_row_can_appear_now(
        self, tmp_path: Path
    ) -> None:
        sampler, _ = _recorded(tmp_path, [tone(FRAME_SAMPLES)] * GOOD_FRAMES)
        bridge = self._bridge(tmp_path, sampler)
        bridge.voice_sample_stop()

        saved = bridge.voice_sample_save(name="我的声音", prompt_text="今天念的这句话")

        assert saved["ok"] is True
        assert [row["id"] for row in saved["voices"]] == [saved["voice"]["id"]]

    def test_the_page_can_tick_upload_and_the_clip_actually_leaves(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """WebView2 hands ``true`` over as a string; a bool-only check drops it silently."""
        seen: list[str] = []

        def fake(_library: Any, _voice_id: str, _audio: bytes, _rate: int, text: object) -> str:
            seen.append(str(text))
            return ""

        monkeypatch.setattr("jarvis.app.voice_cloud.upload_voice", fake)
        sampler, _ = _recorded(tmp_path, [tone(FRAME_SAMPLES)] * GOOD_FRAMES)
        bridge = self._bridge(tmp_path, sampler)
        bridge.voice_sample_stop()

        saved = bridge.voice_sample_save(
            name="我的声音", prompt_text="今天念的这句话", upload="true"
        )

        assert saved["ok"] is True
        assert seen == ["今天念的这句话"], "字符串 true 没被认成「要传」，就等于没传"
        assert saved["uploaded"] is True

    def test_a_double_stop_from_the_page_does_not_freeze_the_bridge_thread(
        self, tmp_path: Path
    ) -> None:
        """WebView2 can hand two calls to one object; the second must answer."""
        sampler, _ = _recorded(tmp_path, [tone(FRAME_SAMPLES)] * GOOD_FRAMES)
        bridge = self._bridge(tmp_path, sampler)
        bridge.voice_sample_stop()

        second = _with_deadline(bridge.voice_sample_stop)

        assert second["ok"] is False
        assert second["phase"] == "captured"


def _disk_service(tmp_path: Path) -> Any:
    from jarvis.app.disk_service import DiskService
    from jarvis.tools.disk_cleaner import DiskCleaner

    return DiskService(lambda: DiskCleaner(audit_log=tmp_path / "audit.jsonl", sources={}))


def test_the_production_capture_format_is_the_one_this_assumes() -> None:
    """``SounddeviceSource`` is what the real process hands this sampler.

    Every elapsed number on screen is computed from :data:`SAMPLE_RATE`, and the store
    refuses any other rate -- so if the shared capture default drifted to 44.1 kHz or
    stereo, the timer and the refusal would both be wrong at once, and nothing here
    would notice unless a test says so.
    """
    from jarvis.audio.format import DEFAULT_FORMAT

    assert DEFAULT_FORMAT.sample_rate == SAMPLE_RATE
    assert DEFAULT_FORMAT.channels == 1
    assert DEFAULT_FORMAT.sample_width == 2


class TestTheMicrophoneQuestion:
    """``VoiceService.capturing`` is the answer the sampler asks before opening a stream."""

    def test_it_follows_the_loop_rather_than_the_phase(self) -> None:
        from jarvis.app.voice_service import VoiceService

        class _Loop:
            def __init__(self) -> None:
                self.listening = True

            def speak_now(self) -> bool:
                return False

            def speak_text(self, text: str) -> bool:
                del text
                return False

            def stop_speaking(self) -> bool:
                return False

            def stop_listening(self) -> None:
                self.listening = False

            def start(self) -> None:
                pass

            def stop(self) -> None:
                pass

        loop = _Loop()
        service = VoiceService(lambda _on_event: loop)
        assert service.capturing is False, "没启动过就是没在录"

        service.enable()
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and not service.capturing:
            time.sleep(0.005)
        # Read into named locals: two asserts on the same property expression narrow it
        # once and then make everything after the second one unreachable to mypy.
        running: bool = service.capturing
        assert running is True

        loop.listening = False
        stopped: bool = service.capturing
        assert stopped is False
