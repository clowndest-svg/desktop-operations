"""The screen-mirror state machine, tested against a stubbed scrcpy and adb.

There is no Android device on the machine this was written on, so these tests are the
entire evidence that the PC half behaves. They deliberately exercise the *failure*
paths hardest -- no device, unauthorized, offline, two devices at once, the window
being closed, scrcpy dying with a code -- because those are what a buyer hits first and
what would otherwise be discovered only by them.

What these tests cannot prove, and no amount of passing here proves: that a real phone
mirrors smoothly. That is scrcpy's side of the line and it is recorded as unverified.
"""

from __future__ import annotations

import socket
import subprocess
import threading
import time
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any, cast

import pytest

from jarvis.app import screen_mirror as sm
from jarvis.app.preferences import (
    MIRROR_AUDIO,
    MIRROR_KEEP_PHONE_SCREEN,
    MIRROR_TIER,
    Preferences,
)
from jarvis.app.screen_mirror import MirrorStatus
from jarvis.app.tool_routing import PACKS, classify_tool_intent, select_tool_names
from jarvis.tools.builtins import mirror_tools

DEVICES_MULTI = """List of devices attached
192.168.1.23:5555        device product:PHU61 model:Pixel_6_Pro device:panther transport_id:2
ZZ9A                   device model:SM_G991B device:o1q transport_id:3
"""

DEVICES_UNAUTHORIZED = """List of devices attached
R5CT90QVZGE              unauthorized model:SM_A515F device:a52q transport_id:1
"""

#: One offline row and one that works. The working row is a *cabled* serial because the
#: service's default 方式 is USB -- a wireless row here would be filtered out before the
#: thing under test (offline rows not counting) ever got a chance to matter.
DEVICES_OFFLINE_AND_READY = """List of devices attached
emulator-5554            offline transport_id:1
R5CT90QVZGE              device model:Redmi_K60 device:marble transport_id:2
"""

DEVICES_TWO_CABLED = """List of devices attached
R5CT90QVZGE              device product:a52q model:Pixel_6_Pro device:panther transport_id:1
ZZ9A                     device model:SM_G991B device:o1q transport_id:3
"""


class FakePipe:
    """A stdout that yields scripted lines, then blocks until the child would be gone.

    The block is the whole point: a real pipe only reaches EOF when scrcpy exits, so a
    fake that returned as soon as the script ran out would let the pump read the exit
    code before reading the output -- the opposite order to reality, which hides both
    the "window closed by hand" path and the crash-reason path.
    """

    def __init__(self, lines: list[str], owner: FakeProc) -> None:
        self._lines = lines
        self._owner = owner
        self.closed = False

    def __iter__(self) -> Any:
        for line in self._lines:
            if self.closed:
                return
            yield line
        while not self.closed and self._owner.poll() is None:
            time.sleep(0.01)


class FakeProc:
    def __init__(self, lines: list[str], *, exit_code: int | None = None) -> None:
        self._code = exit_code
        self.returncode: int | None = None
        self.terminated = False
        self.killed = False
        self.stdout = FakePipe(lines, self)

    def poll(self) -> int | None:
        return self._code

    def wait(self, timeout: float | None = None) -> int:
        del timeout
        return self._code if self._code is not None else 0

    def terminate(self) -> None:
        self.terminated = True
        self.stdout.closed = True
        self._code = 0
        self.returncode = 0

    def kill(self) -> None:
        self.killed = True
        self.stdout.closed = True


class Harness:
    """A service with a vendored runtime, a stubbed adb and a stubbed scrcpy."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        self.root = tmp_path / "scrcpy"
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "scrcpy.exe").write_text("x", encoding="utf-8")
        (self.root / "adb.exe").write_text("x", encoding="utf-8")
        monkeypatch.setattr(sm, "runtime_dir", lambda: self.root)
        self.adb_output = "List of devices attached\n"
        self.adb_script: list[str | bytes] = []
        self.adb_returncode = 0
        self.adb_calls: list[list[str]] = []
        self.spawned: list[list[str]] = []
        self.procs: list[FakeProc] = []
        self._next_lines: list[str] = ["scrcpy 5.0.1 <https://github.com/Genymobile/scrcpy>"]
        self._next_code: int | None = None
        monkeypatch.setattr(subprocess, "run", self._run)
        self.prefs = Preferences(tmp_path / "preferences.json")
        self.window_handle = 12450  # pretend the SDL window is already up
        self.window_calls: list[tuple[int, str]] = []
        self.service = sm.ScreenMirrorService(
            self.prefs,
            spawn=cast("Any", self._spawn),
            find_window=lambda: self.window_handle,
            show_window=lambda handle, how: self._show(handle, how),
        )
        self.service.start()

    def _run(self, argv: Any, **kwargs: Any) -> Any:
        class Done:
            def __init__(self, out: str | bytes, code: int) -> None:
                self.stdout, self.returncode = out, code
                self.stderr: str | bytes = ""

        self.adb_calls.append(list(argv))
        out = self.adb_script.pop(0) if self.adb_script else self.adb_output
        if not kwargs.get("text", True) and isinstance(out, str):
            # ``text=False`` 的真 subprocess 给 bytes。夹具不照这个办，读屏那一路
            # 就会在自己的测试里"通过"，到了真机上才发现拿到的是 str。
            out = out.encode("utf-8")
        return Done(out, self.adb_returncode)

    def _show(self, handle: int, how: str) -> bool:
        self.window_calls.append((handle, how))
        return handle != 0

    def script(self, *outputs: str | bytes) -> None:
        """Answer the next adb calls, in order, with these.

        Needed from r92 on: 「开无线端口」 is tcpip → read wlan0 → connect, three calls
        with three different answers, and one canned string cannot walk that path.
        """
        self.adb_script = list(outputs)

    def _spawn(self, argv: Any, **kwargs: Any) -> FakeProc:
        del kwargs
        self.spawned.append(list(argv))
        proc = FakeProc(list(self._next_lines), exit_code=self._next_code)
        self.procs.append(proc)
        return proc

    def expect(self, lines: list[str], exit_code: int | None = None) -> None:
        self._next_lines = lines
        self._next_code = exit_code

    def settle(self, seconds: float = 0.4) -> None:
        deadline = time.time() + seconds
        while time.time() < deadline and self.service.status()["phase"] in ("starting",):
            time.sleep(0.02)
        time.sleep(0.05)

    def close(self) -> None:
        """Take the pump thread down; without this every test leaks a spinning reader."""
        self.service.stop()


@pytest.fixture
def hub(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[Harness]:
    harness = Harness(monkeypatch, tmp_path)
    try:
        yield harness
    finally:
        harness.close()


def _wait_phase(hub: Harness, phase: str, seconds: float = 3.0) -> MirrorStatus:
    deadline = time.time() + seconds
    while time.time() < deadline:
        status = hub.service.status()
        if status["phase"] == phase:
            return status
        time.sleep(0.02)
    return hub.service.status()


class TestParseDevices:
    def test_reads_model_state_and_transport(self) -> None:
        rows = sm.parse_devices(DEVICES_MULTI)
        assert [row.serial for row in rows] == ["192.168.1.23:5555", "ZZ9A"]
        assert rows[0].ready and rows[0].wireless and not rows[1].wireless
        assert rows[0].label == "Pixel 6 Pro", "下划线要换成空格，屏上给人看的"

    def test_empty_list_is_empty_not_an_error(self) -> None:
        assert sm.parse_devices("List of devices attached\n\n") == ()

    def test_daemon_chatter_is_not_a_device(self) -> None:
        text = (
            "* daemon not running; starting now at tcp:5037\n"
            "* daemon started successfully\n"
            "List of devices attached\nZZ9A device model:Pixel\n"
        )
        assert [row.serial for row in sm.parse_devices(text)] == ["ZZ9A"]

    def test_unrecognised_lines_are_skipped_not_fatal(self) -> None:
        """A format change must not read as 'no phone connected'."""
        rows = sm.parse_devices("List of devices attached\nsome adb prose here\nZZ9A device\n")
        assert [row.serial for row in rows] == ["ZZ9A"]

    @pytest.mark.parametrize(
        ("state", "needle"),
        [
            ("unauthorized", "允许 USB 调试"),
            ("offline", "拔插"),
            ("recovery", "恢复模式"),
        ],
    )
    def test_each_broken_state_says_what_to_do(self, state: str, needle: str) -> None:
        row = sm.parse_devices(f"List of devices attached\nZZ9A {state}\n")[0]
        assert not row.ready
        assert needle in row.how_to_fix

    def test_a_ready_device_asks_for_nothing(self) -> None:
        assert sm.parse_devices("List of devices attached\nZZ9A device\n")[0].how_to_fix == ""


class TestExplain:
    @pytest.mark.parametrize(
        ("line", "needle"),
        [
            ("ERROR: Could not find any ADB device", "没有可投屏的手机"),
            ("ERROR: device 'ZZ9A' not found", "已经不在了"),
            ("adb: device unauthorized", "允许 USB 调试"),
            ("ERROR: adb server version (41) doesn't match this client (39)", "5037"),
            ("ERROR: ARDEADB failed", "撤销安全设置"),
            ("ERROR: ADB push failed", "只能充电"),
            ("ERROR: could not connect to 192.168.1.9:5555: Connection refused", "端口都会变"),
            ("ERROR: Codec not supported", "不支持刚才选的编码器"),
        ],
    )
    def test_known_failures_become_instructions(self, line: str, needle: str) -> None:
        assert needle in sm.explain(line)

    def test_an_unknown_failure_keeps_the_raw_line(self) -> None:
        """No confident guess replaces a message nobody mapped."""
        out = sm.explain("ERROR: something nobody predicted")
        assert "something nobody predicted" in out

    def test_total_silence_says_so(self) -> None:
        assert "没有留下任何输出" in sm.explain("")


class TestCommandShape:
    def test_balanced_tier_carries_the_smoothness_flags(self, hub: Harness) -> None:
        command = hub.service.build_command("ZZ9A", sm.tier_by_id("balanced"), audio=False)
        joined = " ".join(command)
        assert "--max-fps=60" in joined
        assert "--video-bit-rate=6M" in joined
        assert "--video-codec=h264" in joined
        assert "--max-size=1024" in joined
        assert "--no-audio" in joined
        assert "--serial ZZ9A" in joined

    def test_the_renderer_and_the_buffer_policy_are_pinned(self, hub: Harness) -> None:
        """These two are the difference between 60fps and a queue of late frames."""
        joined = " ".join(hub.service.build_command("ZZ9A", sm.tier_by_id("balanced"), audio=False))
        assert "--render-driver=direct3d" in joined
        assert "--video-buffer=0" in joined

    def test_native_tier_omits_the_resize(self, hub: Harness) -> None:
        joined = " ".join(hub.service.build_command("ZZ9A", sm.tier_by_id("native"), audio=False))
        assert "--max-size" not in joined

    def test_audio_adds_the_opus_pair(self, hub: Harness) -> None:
        joined = " ".join(hub.service.build_command("ZZ9A", sm.tier_by_id("balanced"), audio=True))
        assert "--audio-codec=opus" in joined and "--no-audio" not in joined

    def test_turning_the_phone_screen_off_is_opt_in(self, hub: Harness) -> None:
        assert "--turn-screen-off" not in " ".join(
            hub.service.build_command("Z", sm.tier_by_id("balanced"), audio=False)
        )
        hub.prefs.set(MIRROR_KEEP_PHONE_SCREEN, False)
        assert "--turn-screen-off" in " ".join(
            hub.service.build_command("Z", sm.tier_by_id("balanced"), audio=False)
        )

    def test_the_window_is_findable(self, hub: Harness) -> None:
        joined = " ".join(hub.service.build_command("Z", sm.tier_by_id("balanced"), audio=False))
        assert "--window-title=" in joined and "小夜" in joined


class TestStartRefusals:
    def test_no_runtime_explains_the_package_is_incomplete(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        hub = Harness(monkeypatch, tmp_path)
        monkeypatch.setattr(sm, "runtime_dir", lambda: None)
        status = hub.service.start_mirror("ZZ9A")
        assert status["phase"] == "failed"
        assert "scrcpy" in status["detail"] and "不可用" in status["detail"]

    def test_no_device_at_all_tells_you_to_plug_one_in(self, hub: Harness) -> None:
        status = hub.service.start_mirror()
        assert status["phase"] == "failed"
        assert "USB 线" in status["detail"] or "无线调试" in status["detail"]

    def test_a_single_unauthorized_phone_is_reported_not_ignored(self, hub: Harness) -> None:
        hub.adb_output = DEVICES_UNAUTHORIZED
        status = hub.service.start_mirror()
        assert status["phase"] == "failed"
        assert "允许 USB 调试" in status["detail"]

    def test_two_phones_refuse_to_guess(self, hub: Harness) -> None:
        hub.adb_output = DEVICES_TWO_CABLED
        status = hub.service.start_mirror()
        assert status["phase"] == "failed"
        assert "2 台" in status["detail"]
        assert "Pixel 6 Pro" in status["detail"] and "SM G991B" in status["detail"]
        assert hub.spawned == [], "没决定投哪台之前不能启动任何东西"

    def test_an_offline_row_does_not_count_as_a_choice(self, hub: Harness) -> None:
        """One ready + one offline is not ambiguous; the offline one cannot be picked."""
        hub.adb_output = DEVICES_OFFLINE_AND_READY
        hub.expect(["device ready"])
        hub.service.start_mirror()
        assert hub.spawned and "R5CT90QVZGE" in hub.spawned[0]

    def test_adb_failing_is_not_reported_as_no_phones(self, hub: Harness) -> None:
        hub.adb_returncode = 1
        hub.adb_output = "error: failed to get device list"
        rows, error = hub.service.devices()
        assert rows == () and error, "空列表 + 没有原因 = 会被读成'你没插手机'"

    def test_wedged_adb_prose_does_not_become_a_phantom_phone(self, hub: Harness) -> None:
        """adb prints sentences when it is out of date; none of them are a device.

        Reading one as a row invented a phone named ``adb`` in state ``server``, and
        two of them made the service refuse to start with "同时连着 2 台" -- shown to
        somebody who had plugged in exactly one.
        """
        hub.adb_output = (
            "List of devices attached\n"
            "adb server is out of date. killing...\n"
            "error: protocol fault (couldn't read statuses)\n"
        )
        rows, _ = hub.service.devices()
        assert rows == ()
        status = hub.service.start_mirror()
        assert "2 台" not in status["detail"]
        assert hub.spawned == []


class TestLifecycle:
    def test_a_healthy_start_goes_starting_then_running(self, hub: Harness) -> None:
        hub.adb_output = DEVICES_OFFLINE_AND_READY
        hub.expect(["scrcpy 5.0.1", "Device ready", "current fps: 59.9"])

        hub.service.start_mirror()
        status = _wait_phase(hub, "running")

        assert status["phase"] == "running"
        assert status["serial"] == "R5CT90QVZGE"
        assert any("fps" in line for line in status["tail"])

    def test_the_fps_line_reaches_the_panel_while_running(self, hub: Harness) -> None:
        hub.adb_output = DEVICES_OFFLINE_AND_READY
        hub.expect(["Device ready", "current fps: 12.3"])
        hub.service.start_mirror()
        status = _wait_phase(hub, "running")
        assert "12.3" in status["detail"], "卡不卡要能在屏上看见，不能只写进日志"

    def test_a_window_closed_by_hand_is_not_an_error(self, hub: Harness) -> None:
        hub.adb_output = DEVICES_OFFLINE_AND_READY
        hub.expect(["Device ready"], exit_code=0)
        hub.service.start_mirror()
        status = _wait_phase(hub, "off")
        assert status["phase"] == "off"
        assert "窗口已关闭" in status["detail"]
        assert status["detail"] and "失败" not in status["detail"]

    def test_a_crash_is_reported_with_scrcpy_own_words(self, hub: Harness) -> None:
        hub.adb_output = DEVICES_OFFLINE_AND_READY
        hub.expect(["ERROR: ADB push failed"], exit_code=1)
        hub.service.start_mirror()
        status = _wait_phase(hub, "failed")
        assert status["phase"] == "failed"
        assert "只能充电" in status["detail"]

    def test_starting_twice_says_which_one_is_already_up(self, hub: Harness) -> None:
        hub.adb_output = DEVICES_OFFLINE_AND_READY
        hub.expect(["Device ready"])
        hub.service.start_mirror()
        _wait_phase(hub, "running")

        second = hub.service.start_mirror("OTHER")
        assert second["phase"] == "running"
        assert "已经在了" in second["detail"] and "R5CT90QVZGE" in second["detail"]
        assert len(hub.spawned) == 1, "第二台不能悄悄再起一个进程"

    def test_stop_terminates_and_clears(self, hub: Harness) -> None:
        hub.adb_output = DEVICES_OFFLINE_AND_READY
        hub.expect(["Device ready"])
        hub.service.start_mirror()
        _wait_phase(hub, "running")

        status = hub.service.stop_mirror()

        assert status["phase"] == "off"
        assert hub.procs[0].terminated
        assert status["serial"] == ""

    def test_stopping_when_nothing_is_running_is_quiet(self, hub: Harness) -> None:
        status = hub.service.stop_mirror()
        assert status["phase"] == "off"

    def test_service_stop_on_shutdown_takes_the_window_with_it(self, hub: Harness) -> None:
        hub.adb_output = DEVICES_OFFLINE_AND_READY
        hub.expect(["Device ready"])
        hub.service.start_mirror()
        _wait_phase(hub, "running")
        hub.service.stop()
        assert hub.procs[0].terminated


class TestTuningAndPersistence:
    def test_an_unknown_tier_is_refused_not_silently_defaulted(self, hub: Harness) -> None:
        assert "没有" in hub.service.set_tier("ultra")
        assert hub.service.tier().id == "balanced"

    def test_the_tier_survives_a_restart(self, hub: Harness) -> None:
        assert hub.service.set_tier("smooth") == ""
        again = sm.ScreenMirrorService(Preferences(hub.prefs.path))
        assert again.tier().id == "smooth"

    def test_the_three_new_keys_round_trip_through_the_file(self, hub: Harness) -> None:
        """Unregistered keys are dropped on write, which would silently forget them."""
        hub.service.set_tier("sharp")
        hub.service.set_audio(True)
        hub.service.set_keep_screen(False)
        reloaded = Preferences(hub.prefs.path)
        assert reloaded.text(MIRROR_TIER) == "sharp"
        assert reloaded.flag(MIRROR_AUDIO, default=False) is True
        assert reloaded.flag(MIRROR_KEEP_PHONE_SCREEN, default=True) is False

    def test_audio_is_off_until_asked(self, hub: Harness) -> None:
        assert hub.service.audio_enabled() is False

    def test_every_tier_names_what_it_costs(self) -> None:
        for tier in sm.TIERS:
            assert tier.label and tier.note and tier.bit_rate.endswith("M")
        assert len({tier.id for tier in sm.TIERS}) == len(sm.TIERS)


class TestWireless:
    def test_connect_reports_what_the_phone_said(self, hub: Harness) -> None:
        hub.adb_output = "connected to 192.168.1.9:5555\n"
        status = hub.service.connect_wireless("192.168.1.9:5555")
        assert "已连上" in status["detail"]
        assert hub.adb_calls[-1][1] == "connect"

    def test_a_refused_connect_explains_pairing(self, hub: Harness) -> None:
        hub.adb_output = "failed to connect to 192.168.1.9:5555\n"
        status = hub.service.connect_wireless("192.168.1.9:5555")
        assert "配对" in status["detail"] and "同一个 WiFi" in status["detail"]

    def test_pair_needs_a_host_and_six_digits(self, hub: Harness) -> None:
        assert "IP:端口" in hub.service.pair_wireless("192.168.1.9", "123456")["detail"]
        assert "6 位" in hub.service.pair_wireless("192.168.1.9:37123", "12")["detail"]

    def test_a_successful_pair_points_at_the_next_step(self, hub: Harness) -> None:
        hub.adb_output = "Successfully paired to 192.168.1.9:37123 [guid=adb-x]\n"
        status = hub.service.pair_wireless("192.168.1.9:37123", "551 392")
        assert "配对成功" in status["detail"] and "连接" in status["detail"]


class TestPanelContract:
    def test_status_carries_everything_the_panel_draws(self, hub: Harness) -> None:
        status = hub.service.status()
        assert set(status) >= {
            "phase",
            "detail",
            "serial",
            "available",
            "tier",
            "tiers",
            "audio",
            "keep_screen",
            "seconds",
            "tail",
        }
        assert len(status["tiers"]) == len(sm.TIERS)
        assert all({"id", "label", "note"} <= set(row) for row in status["tiers"])

    def test_status_is_safe_to_poll(self, hub: Harness) -> None:
        """The HUD polls this; a lock or a spawn on the read path would stutter the UI."""
        for _ in range(50):
            hub.service.status()
        assert hub.spawned == []

    def test_a_missing_runtime_shows_as_unavailable_not_broken(self, hub: Harness) -> None:
        assert hub.service.available() is True
        hub_patched = sm.ScreenMirrorService(hub.prefs)
        import jarvis.app.screen_mirror as real

        original = real.runtime_dir
        real.runtime_dir = lambda: None
        try:
            assert hub_patched.available() is False
            assert hub_patched.status()["available"] is False
        finally:
            real.runtime_dir = original


def test_the_output_thread_goes_away_with_the_window(hub: Harness) -> None:
    """A reader that outlives its scrcpy would keep a thread busy for the whole session."""
    hub.adb_output = DEVICES_OFFLINE_AND_READY
    hub.expect(["Device ready"])
    baseline = threading.active_count()

    hub.service.start_mirror()
    _wait_phase(hub, "running")
    assert threading.active_count() > baseline, "读数线程要真的起来"

    hub.close()
    deadline = time.time() + 2.0
    while threading.active_count() > baseline and time.time() < deadline:
        time.sleep(0.02)
    assert threading.active_count() <= baseline, "关掉投屏之后线程要自己退掉"


class StubMirror:
    """The five calls the tools make, answering whatever the test asked for."""

    def __init__(
        self,
        *,
        available: bool = True,
        rows: tuple[sm.Device, ...] = (),
        error: str = "",
        status: MirrorStatus | None = None,
    ) -> None:
        self._available = available
        self._rows = rows
        self._error = error
        self._status = status or {
            "phase": "off",
            "detail": "",
            "serial": "",
            "available": available,
            "tier": "balanced",
            "tiers": [],
            "audio": False,
            "keep_screen": True,
            "seconds": 0,
            "tail": [],
        }
        self.started: list[str] = []
        self.tier_written: str = ""

    def available(self) -> bool:
        return self._available

    def status(self) -> MirrorStatus:
        return cast("MirrorStatus", self._status)

    def devices(self) -> tuple[tuple[sm.Device, ...], str]:
        return self._rows, self._error

    def phones(self) -> tuple[tuple[sm.Device, ...], str]:
        """这些行本来就是不同的手机，并组是空操作。"""
        return self._rows, self._error

    def start_mirror(self, serial: str = "") -> dict[str, object]:
        self.started.append(serial)
        return dict(self._status)

    def stop_mirror(self) -> dict[str, object]:
        return dict(self._status)

    def set_tier(self, value: object) -> str:
        name = str(value or "")
        if name not in {tier.id for tier in sm.TIERS}:
            return f"没有「{name}」这一档"
        self.tier_written = name
        return ""

    def set_audio(self, value: object) -> str:
        return ""

    def set_keep_screen(self, value: object) -> str:
        return ""


class _FakeReaderMirror:
    """The two calls :class:`PhoneScreenReader` makes, and nothing else."""

    def read_screen(self) -> Mapping[str, object]:
        return {"app": "com.example", "activity": "", "widgets": [], "note": "", "error": ""}

    def capture_frame(self) -> tuple[bytes, str]:
        return b"", "没该拿"


def _handler(tools: list[tuple[Any, Any]], name: str) -> Any:
    return next(handler for spec, handler in tools if spec.name == name)


_RUNNING: MirrorStatus = {
    "phase": "running",
    "detail": "current fps: 57.4",
    "serial": "R5CT90QVZGE",
    "available": True,
    "tier": "sharp",
    "tiers": [],
    "mode": "usb",
    "modes": [],
    "window": "window",
    "windows": [],
    "stretch": False,
    "keys": [],
    "swipes": [],
    "audio": False,
    "keep_screen": True,
    "discovery": "",
    "seconds": 41,
    "tail": ["scrcpy 5.0.1", "Device ready", "current fps: 57.4"],
    "error": "",
}


class TestMirrorTools:
    def test_no_runtime_means_no_tools_advertised(self) -> None:
        """She must not be able to say 「帮你打开投屏了」 about a window that cannot open."""
        assert mirror_tools.build(StubMirror(available=False)) == []

    def test_the_tool_names_are_the_ones_the_pack_advertises(self) -> None:
        """Only what a build can actually run gets named, and the pack lists exactly that.

        ``phone_screen_read`` hangs off the same pack but needs a reader *and* a runtime,
        so it appears only in the second call below: a route naming a tool no build ever
        registers is the silent dead end this assertion exists to catch.
        """
        from jarvis.app.phone_screen import PhoneScreenReader

        names = {spec.name for spec, _ in mirror_tools.build(StubMirror())}
        assert names == set(PACKS["mirror"]) - {"phone_screen_read"}
        reader = PhoneScreenReader(_FakeReaderMirror(), prefs=None)
        with_reader = {spec.name for spec, _ in mirror_tools.build(StubMirror(), reader)}
        assert with_reader == set(PACKS["mirror"])

    def test_the_devices_tool_says_which_row_is_the_problem(
        self,
    ) -> None:
        rows = (
            sm.Device(serial="R5CT9", state="device", model="Pixel_6_Pro"),
            sm.Device(serial="ZZ9A", state="unauthorized", model="SM_A515F"),
        )
        text = _handler(mirror_tools.build(StubMirror(rows=rows)), "mirror_devices")({})
        assert "Pixel 6 Pro" in text
        assert "可投屏" in text and "投不了" in text
        assert "允许 USB 调试" in text, "第二行才是用户要处理的"

    def test_an_empty_device_list_offers_both_ways_in(self) -> None:
        text = _handler(mirror_tools.build(StubMirror()), "mirror_devices")({})
        assert "USB 线" in text and "无线调试" in text

    def test_a_wedged_adb_is_answered_as_a_failure_not_as_an_empty_room(self) -> None:
        text = _handler(mirror_tools.build(StubMirror(error="adb 起不来")), "mirror_devices")({})
        assert "问不到手机列表" in text and "adb 起不来" in text

    def test_start_reports_the_phase_and_scrcpy_s_own_line(self) -> None:
        tools = mirror_tools.build(StubMirror(status=_RUNNING))
        text = _handler(tools, "mirror_start")({"serial": "R5CT9"})
        assert "正在投屏" in text and "R5CT90QVZGE" in text
        assert "57.4" in text and "sharp" in text

    def test_a_refused_start_reaches_the_model_as_text(self) -> None:
        refused: MirrorStatus = {
            **_RUNNING,
            "phase": "failed",
            "detail": sm.explain("ERROR: ADB push failed"),
        }
        tools = mirror_tools.build(StubMirror(status=refused))
        text = _handler(tools, "mirror_start")({})
        assert "失败" in text and "只能充电" in text

    def test_the_serial_is_passed_through_not_invented(self) -> None:
        stub = StubMirror(status=_RUNNING)
        _handler(mirror_tools.build(stub), "mirror_start")({"serial": " 192.168.1.9:5555 "})
        assert stub.started == ["192.168.1.9:5555"]


class TestMirrorRouting:
    """The 4K model only gets these three tools when the sentence is about mirroring."""

    @pytest.mark.parametrize(
        "text",
        [
            "把手机投屏到电脑上",
            "我想把手机画面投放到显示器",
            "scrcpy 能用吗",
            "手机屏幕能不能共享给电脑",
        ],
    )
    def test_a_mirror_sentence_pulls_the_mirror_pack(self, text: str) -> None:
        assert "mirror" in classify_tool_intent(text)

    @pytest.mark.parametrize(
        "text",
        [
            "这个 docker 镜像怎么构建",
            "帮我做个系统镜像备份",
            "把这张图投到显存里了吗",
        ],
    )
    def test_a_bare_jing_xiang_does_not_pull_the_pack(self, text: str) -> None:
        assert "mirror" not in classify_tool_intent(text)

    def test_the_pack_only_offers_what_is_registered(self) -> None:
        picked = select_tool_names(("mirror",), ["mirror_start", "current_time"])
        assert picked == ("current_time", "mirror_start")


class TestBridgeDoors:
    """The panel's seven doors, including the one that reads the real service."""

    def test_every_door_answers_for_a_bridge_with_no_mirror_service(self) -> None:
        from jarvis.ui.desktop import HudBridge

        bridge = HudBridge.__new__(HudBridge)
        state = bridge.mirror_state()
        assert state["phase"] == "off" and state["available"] is False
        listed = bridge.mirror_devices()
        assert listed["devices"] == [] and listed["error"] == "这个进程没有接投屏服务"
        assert bridge.mirror_start("")["phase"] == "failed"
        assert bridge.mirror_stop()["phase"] == "off"
        assert bridge.mirror_connect("192.168.1.9:5555")["phase"] == "failed"
        assert bridge.mirror_pair("192.168.1.9:37123", "551392")["phase"] == "failed"
        assert bridge.mirror_tune("smooth", True, False)["phase"] == "failed"

    def test_the_panel_door_answers_with_the_service_own_status(self) -> None:
        from jarvis.ui.desktop import HudBridge

        bridge = HudBridge.__new__(HudBridge)
        bridge._mirror = cast("Any", StubMirror(status=_RUNNING))
        assert bridge.mirror_state()["phase"] == "running"
        assert bridge.mirror_start("R5CT9")["serial"] == "R5CT90QVZGE"
        rows = bridge.mirror_devices()["devices"]
        assert bridge.mirror_devices()["error"] == ""
        assert rows == []
        assert bridge.mirror_tune("ultra", True, False)["error"] == "没有「ultra」这一档"

    def test_a_tier_picked_on_the_panel_is_the_tier_scrcpy_is_started_with(
        self, hub: Harness
    ) -> None:
        """The real join: bridge door -> service -> argv. Nothing here is a copy."""
        from jarvis.ui.desktop import HudBridge

        bridge = HudBridge.__new__(HudBridge)
        bridge._mirror = hub.service

        tuned = bridge.mirror_tune("smooth", False, False)
        assert tuned["tier"] == "smooth" and tuned["error"] == ""
        assert tuned["audio"] is False and tuned["keep_screen"] is False

        started = bridge.mirror_start("R5CT90QVZGE")
        assert started["phase"] == "starting"
        argv = " ".join(hub.spawned[-1])
        assert "--max-fps=30" in argv, "面板上选的档位要真的落到那一发命令上"
        assert "--turn-screen-off" in argv

        assert bridge.mirror_stop()["phase"] == "off"
        assert hub.procs[-1].terminated

    def test_the_device_door_carries_the_per_row_instruction(self, hub: Harness) -> None:
        from jarvis.ui.desktop import HudBridge

        hub.adb_output = DEVICES_UNAUTHORIZED
        bridge = HudBridge.__new__(HudBridge)
        bridge._mirror = hub.service
        rows, _ = hub.service.devices()
        listed = bridge.mirror_devices()
        assert listed["devices"][0]["serial"] == rows[0].serial
        assert "允许 USB 调试" in str(listed["devices"][0]["hint"])


class TestShippedInTheFrozenBuild:
    """The runtime has to be in the package, or the panel is a button that cannot work."""

    def test_the_spec_collects_the_vendored_scrcpy(self) -> None:
        spec = (Path(__file__).resolve().parents[1] / "packaging" / "jarvis.spec").read_text(
            encoding="utf-8"
        )
        assert '"jarvis", "vendor", "scrcpy"' in spec
        assert 'datas.append((_scrcpy, "jarvis/vendor/scrcpy"))' in spec

    def test_the_runtime_dir_the_service_reads_is_the_one_the_spec_ships(self) -> None:
        """``runtime_dir()`` looks under the package; the spec lands it under ``_internal``.

        These two have to agree or the frozen build answers 「没带 scrcpy」 while the
        files sit somewhere the code never looks -- which is how the llama gap was found.
        """
        where = sm.runtime_dir()
        assert where is not None, "开发检出里 jarvis/vendor/scrcpy 应该在，投屏才不是空谈"
        assert (where / sm.SCRCPY_EXE).is_file() and (where / sm.ADB_EXE).is_file()
        assert (where / "scrcpy-server").is_file(), "scrcpy 要把它推到手机上"
        assert (where / "LICENSE.txt").is_file(), "随包分发的第三方代码要带许可文本"


DEVICES_ONE_OF_EACH = """List of devices attached
R5CT90QVZGE              device product:a52q model:SM_A515F device:a52q transport_id:1
192.168.1.9:5555         device model:Redmi_K60 device:marble transport_id:2
"""

DEVICES_WIRELESS_ONLY = """List of devices attached
192.168.1.9:5555         device model:Redmi_K60 device:marble transport_id:2
"""

DEVICES_CABLED_ONLY = """List of devices attached
R5CT90QVZGE              device product:a52q model:SM_A515F device:a52q transport_id:1
"""

WLAN0 = "14: wlan0    inet 192.168.1.23/24 brd 192.168.1.255 scope global wlan0\n"

LOOPBACK_ANSWER = (
    "1: lo    inet 127.0.0.1/8 scope host lo\n" "14: wlan0    inet 127.0.0.1/8 scope host lo\n"
)


class TestConnectionMode:
    """The dropdown is a filter over ``adb devices`` -- so test what it filters out."""

    def test_both_options_say_what_they_cost(self) -> None:
        assert [mode.id for mode in sm.MODES] == ["usb", "wifi", "scan"]
        for mode in sm.MODES:
            assert mode.label and mode.pros and mode.cons and mode.hint
        assert "延迟最低" in sm.mode_by_id("usb").pros
        assert "AP 隔离" in sm.mode_by_id("wifi").cons, "公共 WiFi 连不上是这条路的硬前提"

    def test_usb_mode_picks_the_cabled_phone_and_not_the_network_one(self, hub: Harness) -> None:
        hub.adb_output = DEVICES_ONE_OF_EACH
        hub.expect(["Device ready"])
        hub.service.start_mirror()
        assert hub.spawned and "R5CT90QVZGE" in hub.spawned[0]
        assert not any("192.168" in row for row in hub.spawned[0])

    def test_wifi_mode_picks_the_network_phone(self, hub: Harness) -> None:
        hub.adb_output = DEVICES_ONE_OF_EACH
        hub.service.set_mode("wifi")
        hub.expect(["Device ready"])
        hub.service.start_mirror()
        assert hub.spawned and "192.168.1.9:5555" in hub.spawned[0]

    def test_a_mode_with_no_such_phone_names_the_one_that_is_there(self, hub: Harness) -> None:
        """'没有可投屏的手机' would be a lie here -- the phone is right there, in the other mode."""
        hub.adb_output = DEVICES_WIRELESS_ONLY
        status = hub.service.start_mirror()
        assert status["phase"] == "failed"
        assert "当前方式是" in status["detail"]
        assert "同一个 WiFi" in status["detail"], "要指名切到哪一格，不能只说没有手机"
        assert "192.168.1.9:5555" in status["detail"]
        assert hub.spawned == []

    def test_the_cable_side_says_so_too(self, hub: Harness) -> None:
        hub.adb_output = DEVICES_CABLED_ONLY
        hub.service.set_mode("wifi")
        status = hub.service.start_mirror()
        assert "USB 线" in status["detail"]

    def test_two_phones_in_the_same_mode_are_still_ambiguous(self, hub: Harness) -> None:
        hub.adb_output = DEVICES_ONE_OF_EACH + "ZZ9A                   device model:Pixel_6\n"
        hub.service.set_mode("usb")
        status = hub.service.start_mirror()
        assert "2 台" in status["detail"] and "192.168" not in status["detail"]

    def test_an_explicit_serial_bypasses_the_filter(self, hub: Harness) -> None:
        """点那一行 = 我要这台，方式那一格不该拦人。"""
        hub.adb_output = DEVICES_WIRELESS_ONLY
        hub.expect(["Device ready"])
        hub.service.start_mirror("192.168.1.9:5555")
        assert hub.spawned and "192.168.1.9:5555" in hub.spawned[0]

    def test_the_mode_survives_a_restart(self, hub: Harness) -> None:
        assert hub.service.set_mode("wifi") == ""
        again = sm.ScreenMirrorService(Preferences(hub.prefs.path))
        assert again.mode().id == "wifi"

    def test_an_unknown_mode_is_refused_not_silently_defaulted(self, hub: Harness) -> None:
        assert "没有" in hub.service.set_mode("miracast")
        assert hub.service.mode().id == "usb"

    def test_status_carries_the_options_the_dropdown_draws(self, hub: Harness) -> None:
        status = hub.service.status()
        assert status["mode"] == "usb"
        assert [row["id"] for row in status["modes"]] == ["usb", "wifi", "scan"]
        assert all({"id", "label", "pros", "cons"} <= set(row) for row in status["modes"])

    def test_every_mode_names_both_ways_in(self) -> None:
        """The two hints must not send the same person to the same dead end."""
        usb = sm.mode_by_id("usb").hint
        wifi = sm.mode_by_id("wifi").hint
        assert "线" in usb and "配对" in wifi


class TestWirelessPortHandoff:
    """``adb tcpip`` is the only wireless door Android 10 and older have."""

    def test_the_cabled_phone_is_told_to_listen_on_the_fixed_port(self, hub: Harness) -> None:
        hub.script("restarting in TCP mode port: 5555\n", "error: no interface wlan0\n")
        hub.service.open_wireless_port("R5CT90QVZGE")
        assert hub.adb_calls[0][1:] == ["-s", "R5CT90QVZGE", "tcpip", "5555"]
        assert hub.adb_calls[1][1:] == [
            "-s",
            "R5CT90QVZGE",
            "shell",
            "ip",
            "-f",
            "inet",
            "addr",
            "show",
            "wlan0",
        ]

    def test_the_address_is_read_off_the_phone_and_connected_in_one_click(
        self, hub: Harness
    ) -> None:
        hub.script(
            "restarting in TCP mode port: 5555\n",
            WLAN0,
            "connected to 192.168.1.23:5555\n",
        )
        status = hub.service.open_wireless_port("R5CT90QVZGE")
        assert "已连上 192.168.1.23:5555" in status["detail"]
        assert hub.adb_calls[-1][1:] == ["connect", "192.168.1.23:5555"]

    def test_the_handoff_tells_you_the_dropdown_is_still_on_the_cable(self, hub: Harness) -> None:
        hub.script("restarting in TCP mode port: 5555\n", WLAN0, "connected to 192.168.1.23:5555\n")
        status = hub.service.open_wireless_port("R5CT90QVZGE")
        assert "切到" in status["detail"] and "同一个 WiFi" in status["detail"]

    def test_a_loopback_answer_is_not_the_phone_s_address(self, hub: Harness) -> None:
        """Some ROMs ignore the interface argument and print ``lo`` too."""
        hub.script("restarting in TCP mode port: 5555\n", LOOPBACK_ANSWER)
        status = hub.service.open_wireless_port("R5CT90QVZGE")
        assert "读不到它的 WiFi 地址" in status["detail"]
        assert "127.0.0.1" not in status["detail"]

    def test_a_phone_without_wlan0_still_gets_a_way_forward(self, hub: Harness) -> None:
        hub.script("restarting in TCP mode port: 5555\n", "RTNETLINK answers: No such device\n")
        status = hub.service.open_wireless_port("R5CT90QVZGE")
        assert "读不到它的 WiFi 地址" in status["detail"]
        assert "IP:5555" in status["detail"], "要给出下一步能照抄的格式，而不是只说失败"

    def test_a_refused_tcpip_keeps_adb_s_own_words(self, hub: Harness) -> None:
        hub.script("error: device 'R5CT90QVZGE' not found\n")
        status = hub.service.open_wireless_port("R5CT90QVZGE")
        assert status["phase"] == "failed"
        assert "正插着线" in status["detail"] and "not found" in status["detail"]

    def test_no_cabled_phone_explains_the_prerequisite(self, hub: Harness) -> None:
        hub.adb_output = DEVICES_WIRELESS_ONLY
        status = hub.service.open_wireless_port()
        assert "USB 线" in status["detail"]
        assert not any("tcpip" in call for call in hub.adb_calls), "不该对手机下 tcpip 命令"

    def test_two_cabled_phones_refuse_to_guess(self, hub: Harness) -> None:
        hub.adb_output = DEVICES_ONE_OF_EACH + "ZZ9A                   device model:Pixel_6\n"
        status = hub.service.open_wireless_port()
        assert "2 台" in status["detail"]


class TestModeDoors:
    def test_the_panel_writes_the_mode_through_the_same_door_as_the_tier(
        self, hub: Harness
    ) -> None:
        from jarvis.ui.desktop import HudBridge

        bridge = HudBridge.__new__(HudBridge)
        bridge._mirror = hub.service
        out = bridge.mirror_tune("smooth", False, True, "wifi")
        assert out["mode"] == "wifi" and out["tier"] == "smooth" and out["error"] == ""
        assert hub.service.mode().id == "wifi"

    def test_a_bad_mode_comes_back_as_a_sentence_not_a_silent_default(self, hub: Harness) -> None:
        from jarvis.ui.desktop import HudBridge

        bridge = HudBridge.__new__(HudBridge)
        bridge._mirror = hub.service
        out = bridge.mirror_tune("", None, None, "miracast")
        assert "miracast" in out["error"] and out["mode"] == "usb"

    def test_the_tcpip_door_reaches_the_service(self, hub: Harness) -> None:
        from jarvis.ui.desktop import HudBridge

        bridge = HudBridge.__new__(HudBridge)
        bridge._mirror = hub.service
        hub.script(
            "restarting in TCP mode port: 5555\n",
            WLAN0,
            "connected to 192.168.1.23:5555\n",
        )
        status = bridge.mirror_tcpip("R5CT90QVZGE")
        assert "192.168.1.23:5555" in status["detail"]
        assert hub.adb_calls[0][1:] == ["-s", "R5CT90QVZGE", "tcpip", "5555"]

    def test_the_bridge_answers_for_a_half_built_bridge(self) -> None:
        from jarvis.ui.desktop import HudBridge

        bridge = HudBridge.__new__(HudBridge)
        assert bridge.mirror_tcpip("X")["phase"] == "failed"
        assert bridge.mirror_state()["available"] is False

    def test_the_tool_answer_names_the_mode_in_force(self) -> None:
        tools = mirror_tools.build(
            StubMirror(
                rows=(sm.Device(serial="R5CT9", state="device", model="SM_A515F"),),
                status=cast(
                    "MirrorStatus",
                    {
                        "phase": "running",
                        "detail": "current fps: 60.0",
                        "serial": "R5CT9",
                        "available": True,
                        "tier": "balanced",
                        "tiers": [],
                        "mode": "usb",
                        "window": "window",
                        "windows": [],
                        "stretch": False,
                        "keys": [],
                        "swipes": [],
                        "modes": [
                            {"id": "usb", "label": "USB 线（开 USB 调试）"},
                            {"id": "wifi", "label": "同一个 WiFi（无线调试）"},
                        ],
                        "audio": False,
                        "keep_screen": True,
                        "seconds": 3,
                        "tail": [],
                    },
                ),
            )
        )
        devices = _handler(tools, "mirror_devices")({})
        assert "USB 线" in devices
        started = _handler(tools, "mirror_start")({})
        assert "方式：USB 线" in started


def install_interfaces(
    monkeypatch: pytest.MonkeyPatch, rows: list[tuple[str, str, str, bool]]
) -> None:
    """Pretend this machine has exactly these adapters (name, address, netmask, up)."""

    def fake() -> list[sm.Interface]:
        return [
            sm.Interface(name=name, address=address, netmask=netmask, active=active)
            for name, address, netmask, active in rows
        ]

    monkeypatch.setattr(sm, "interfaces", fake)


def _open_port() -> tuple[int, Any]:
    """A real listening socket, so the probe is tested against a real TCP answer."""
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    probe.listen(1)
    return int(probe.getsockname()[1]), probe


def _closed_port() -> int:
    dead = socket.socket()
    dead.bind(("127.0.0.1", 0))
    port = int(dead.getsockname()[1])
    dead.close()
    return port


class TestScanMode:
    """The third option: same WiFi, no 无线调试, found by scanning the fixed port."""

    def test_three_options_and_each_says_what_it_costs(self) -> None:
        assert [mode.id for mode in sm.MODES] == ["usb", "wifi", "scan"]
        for mode in sm.MODES:
            assert mode.label and mode.pros and mode.cons and mode.hint

    def test_the_scan_option_says_the_cable_is_still_needed_once(self) -> None:
        """The honest part of the promise: no software can skip the first cable."""
        cons = sm.mode_by_id("scan").cons
        assert "第一次那根线省不掉" in cons
        assert "AP 隔离" in cons

    def test_scan_cares_about_the_port_and_the_other_two_do_not(self) -> None:
        cabled = sm.Device(serial="R5CT9", state="device")
        tcpip = sm.Device(serial="192.168.1.9:5555", state="device")
        wireless_debug = sm.Device(serial="192.168.1.9:37123", state="device")
        assert sm.in_mode("usb", cabled)
        assert not sm.in_mode("usb", tcpip)
        assert sm.in_mode("wifi", tcpip) and sm.in_mode("wifi", wireless_debug)
        assert sm.in_mode("scan", tcpip), "tcpip 之后端口固定在 5555"
        assert not sm.in_mode("scan", wireless_debug), "无线调试的随机端口不是这一档要找的"

    def test_scan_mode_picks_the_phone_on_the_fixed_port(self, hub: Harness) -> None:
        hub.adb_output = (
            "List of devices attached\n"
            "R5CT90QVZGE              device model:SM_A515F\n"
            "192.168.1.9:37123        device model:Redmi_K60\n"
            "192.168.1.23:5555        device model:PHU61\n"
        )
        hub.service.set_mode("scan")
        hub.expect(["Device ready"])
        hub.service.start_mirror()
        assert hub.spawned and "192.168.1.23:5555" in hub.spawned[0]

    def test_a_random_port_phone_is_pointed_at_the_other_option(self, hub: Harness) -> None:
        hub.adb_output = (
            "List of devices attached\n192.168.1.9:37123        device model:Redmi_K60\n"
        )
        hub.service.set_mode("scan")
        status = hub.service.start_mirror()
        # 真正要钉的是「把上面切到」后面那个目标，不是整句里出现了什么字：
        # 当前方式自己的标签也含"不开无线调试"，用"不含某词"判据会误伤。
        target = status["detail"].split("把上面切到「", 1)[1].split("」", 1)[0]
        assert target == "同一个 WiFi（无线调试）", "别让人切到他已经在的那一档"

    def test_an_unknown_mode_still_refuses(self, hub: Harness) -> None:
        assert "没有" in hub.service.set_mode("miracast")


class TestPortProbe:
    def test_a_listening_socket_answers(self) -> None:
        port, listener = _open_port()
        try:
            assert sm.scan_port("127.0.0.1", port, 0.5) is True
        finally:
            listener.close()

    def test_a_closed_port_does_not(self) -> None:
        assert sm.scan_port("127.0.0.1", _closed_port(), 0.4) is False

    def test_the_host_list_follows_the_real_mask(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The old rule ("cut off the last octet") is what OPT-06 is about: it is wrong
        on a /23 and half-wrong on a /25, and both are things home routers hand out."""
        install_interfaces(monkeypatch, [("Wi-Fi", "192.168.1.50", "255.255.255.0", True)])
        hosts = sm.ScreenMirrorService._scan_hosts()
        assert len(hosts) == 254, "一个 /24，去掉网络位和广播位"
        assert hosts[0] == "192.168.1.1" and "192.168.1.255" not in hosts
        assert "192.168.2.1" not in hosts

    def test_a_23_sweeps_both_halves_and_a_25_only_one(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        install_interfaces(monkeypatch, [("Wi-Fi", "192.168.40.10", "255.255.254.0", True)])
        wide = sm.ScreenMirrorService._scan_hosts()
        assert "192.168.41.77" in wide
        install_interfaces(monkeypatch, [("Wi-Fi", "192.168.40.10", "255.255.255.128", True)])
        narrow = sm.ScreenMirrorService._scan_hosts()
        assert "192.168.40.200" not in narrow and "192.168.40.30" in narrow

    def test_a_down_adapter_adds_nothing_to_the_sweep(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        install_interfaces(
            monkeypatch,
            [
                ("Wi-Fi", "192.168.40.10", "255.255.255.0", True),
                ("VPN", "10.88.0.2", "255.255.255.0", False),
            ],
        )
        hosts = sm.ScreenMirrorService._scan_hosts()
        assert not any(host.startswith("10.88.0.") for host in hosts)

    def test_an_overgrown_network_is_skipped_not_swept(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """/16 is 65534 addresses: a button must not turn into an afternoon."""
        install_interfaces(monkeypatch, [("Wi-Fi", "10.20.0.5", "255.0.0.0", True)])
        assert sm.ScreenMirrorService._scan_hosts() == []


class TestAutoDiscovery:
    def test_an_answering_host_is_connected_by_adb(self, hub: Harness) -> None:
        port, listener = _open_port()
        try:
            hub.script(f"connected to 127.0.0.1:{port}\n")
            status = hub.service.discover_wireless(port=port, hosts=["127.0.0.1"])
        finally:
            listener.close()
        assert f"连上 1 台：127.0.0.1:{port}" in status["detail"]
        assert hub.adb_calls[-1][1:] == ["connect", f"127.0.0.1:{port}"]

    def test_this_machine_itself_is_never_treated_as_a_phone(
        self, hub: Harness, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        port, listener = _open_port()
        monkeypatch.setattr(sm, "local_ipv4s", lambda: ["127.0.0.1"])
        try:
            hub.script("connected\n")
            status = hub.service.discover_wireless(port=port, hosts=["127.0.0.1"])
        finally:
            listener.close()
        assert "没有设备应答" in status["detail"]
        assert not any(call[1:2] == ["connect"] for call in hub.adb_calls)

    def test_no_answer_explains_the_cable_and_the_isolated_network(self, hub: Harness) -> None:
        status = hub.service.discover_wireless(port=_closed_port(), hosts=["127.0.0.1"])
        assert "没有设备应答" in status["detail"]
        assert "开无线端口" in status["detail"] and "AP 隔离" in status["detail"]

    def test_a_host_that_opens_the_port_but_refuses_adb_says_so(self, hub: Harness) -> None:
        port, listener = _open_port()
        try:
            hub.script("unable to connect to 127.0.0.1:5555: connection refused\n")
            status = hub.service.discover_wireless(port=port, hosts=["127.0.0.1"])
        finally:
            listener.close()
        assert "adb 一台都没连上" in status["detail"]
        assert "connection refused" in status["detail"], "adb 自己的话要留在屏上"

    def test_no_network_at_all_is_reported_rather_than_scanning_anything(
        self, hub: Harness, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``_scan_hosts`` reads ``interfaces()`` now, so that is the seam to close --
        patching the wrong one here would sweep this machine's real LAN from a test."""
        monkeypatch.setattr(sm, "interfaces", lambda: [])
        monkeypatch.setattr(sm, "local_ipv4s", lambda: [])
        status = hub.service.discover_wireless()
        assert "问不到可扫的网段" in status["detail"]

    def test_the_discovery_door_on_a_half_built_bridge(self) -> None:
        from jarvis.ui.desktop import HudBridge

        bridge = HudBridge.__new__(HudBridge)
        assert bridge.mirror_discover()["phase"] == "failed"

    def test_discovery_while_running_keeps_the_stop_button_alive(
        self, hub: Harness, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """OPT-04, the user-visible half: the window is still open, so 停止 must still work."""
        hub.service.set_mode("scan")
        hub.adb_output = DEVICES_WIRELESS_ONLY
        hub.expect(["Device ready", "current fps: 60.0"])
        hub.service.start_mirror("192.168.1.9:5555")
        before = _wait_phase(hub, "running")
        assert (before["phase"], before["serial"]) == ("running", "192.168.1.9:5555")
        monkeypatch.setattr(
            sm.ScreenMirrorService,
            "_sweep",
            staticmethod(lambda hosts, port, timeout: ["192.168.1.77"]),
        )
        monkeypatch.setattr(sm, "local_ipv4s", lambda: ["192.168.1.10"])
        hub.script("connected to 192.168.1.77:5555\n")
        after = hub.service.discover_wireless(hosts=["192.168.1.77"])
        assert (after["phase"], after["serial"]) == ("running", "192.168.1.9:5555")
        assert "连上 1 台" in after["discovery"], "发现结果要说，但不能顶掉运行态"
        assert hub.procs[0].poll() is None, "scrcpy 还活着"
        hub.service.stop_mirror()
        assert hub.procs[0].terminated, "停止仍然管得住那个窗口"

    def test_a_pump_from_an_older_generation_writes_nothing(self, hub: Harness) -> None:
        """代次号：一个已经结束的开始过程的收尾，不许改写现在这次投屏的状态。"""
        hub.adb_output = DEVICES_CABLED_ONLY
        hub.expect(["Device ready"])
        hub.service.start_mirror()
        _wait_phase(hub, "running")
        stale = hub.procs[0]
        stale._code = 1
        stale.stdout.closed = True
        hub.service._generation += 1  # 另一次 start/stop 已经接管了状态
        hub.service._pump(cast("Any", stale), hub.service._generation - 1)
        status = hub.service.status()
        assert status["phase"] == "running", "旧代次的收尾把在跑的写成了 off"
        assert status["serial"] == "R5CT90QVZGE"

    def test_the_discovery_door_reaches_the_service(
        self, hub: Harness, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The door delegates -- and the scan stays inside an injected host list.

        Without the patch this would sweep a real /24 from a test suite.
        """
        from jarvis.ui.desktop import HudBridge

        monkeypatch.setattr(
            sm.ScreenMirrorService, "_scan_hosts", staticmethod(lambda: ["127.0.0.1"])
        )
        bridge = HudBridge.__new__(HudBridge)
        bridge._mirror = hub.service
        status = bridge.mirror_discover()
        assert "扫了本网段 1 个地址的 5555 端口" in status["detail"]


class TestDisplayMode:
    """铺满 is done to the live window; 全屏 is a launch flag. The code says which."""

    def test_three_display_options_each_say_what_they_cost(self) -> None:
        assert [mode.id for mode in sm.WINDOW_MODES] == ["window", "maximized", "fullscreen"]
        for mode in sm.WINDOW_MODES:
            assert mode.label and mode.note
        assert "F11" in sm.window_mode_by_id("fullscreen").note
        assert "变形" in sm.window_mode_by_id("maximized").note

    def test_only_fullscreen_adds_the_launch_flag(self, hub: Harness) -> None:
        tier = sm.tier_by_id("balanced")
        joined = " ".join(hub.service.build_command("Z", tier, audio=False))
        assert "--fullscreen" not in joined
        hub.service.set_window_mode("fullscreen")
        joined = " ".join(hub.service.build_command("Z", tier, audio=False))
        assert "--fullscreen" in joined

    def test_stretch_is_opt_in_and_unlocks_the_aspect(self, hub: Harness) -> None:
        tier = sm.tier_by_id("balanced")
        joined = " ".join(hub.service.build_command("Z", tier, audio=False))
        assert "stretched" not in joined and "aspect-ratio-lock" not in joined
        hub.service.set_stretch(True)
        joined = " ".join(hub.service.build_command("Z", tier, audio=False))
        assert "--no-window-aspect-ratio-lock" in joined and "--render-fit=stretched" in joined

    def test_the_stretch_choice_survives_a_restart(self, hub: Harness) -> None:
        hub.service.set_stretch(True)
        again = sm.ScreenMirrorService(Preferences(hub.prefs.path))
        assert again.stretch_enabled() is True, "拉伸这一勾要记住，不然每次重开都要再勾一遍"

    def test_maximize_reaches_the_open_window_without_a_restart(self, hub: Harness) -> None:
        hub.adb_output = DEVICES_CABLED_ONLY
        hub.expect(["Device ready"])
        hub.service.start_mirror()
        _wait_phase(hub, "running")
        hub.window_calls.clear()
        assert hub.service.apply_window_mode("maximized") == ""
        assert hub.window_calls == [(hub.window_handle, "maximized")]
        assert hub.service.status()["phase"] == "running", "铺满不该把投屏重启"

    def test_fullscreen_while_running_says_a_restart_is_left(self, hub: Harness) -> None:
        hub.adb_output = DEVICES_CABLED_ONLY
        hub.expect(["Device ready"])
        hub.service.start_mirror()
        _wait_phase(hub, "running")
        note = hub.service.apply_window_mode("fullscreen")
        assert "重开" in note and "F11" in note
        assert hub.service.window_mode().id == "fullscreen", "还是要记住，下次启动就全屏"
        assert hub.window_calls == [], "全屏不去动别人的窗口"

    def test_a_closed_window_is_reported_not_assumed(self, hub: Harness) -> None:
        hub.adb_output = DEVICES_CABLED_ONLY
        hub.expect(["Device ready"])
        hub.service.start_mirror()
        _wait_phase(hub, "running")
        hub.window_handle = 0
        assert "没找到投屏窗口" in hub.service.apply_window_mode("maximized")

    def test_an_unknown_display_mode_is_refused(self, hub: Harness) -> None:
        assert "没有" in hub.service.apply_window_mode("kiosk")
        assert hub.service.window_mode().id == "window"

    def test_a_choice_made_while_idle_is_remembered(self, hub: Harness) -> None:
        assert hub.service.apply_window_mode("maximized") == ""
        again = sm.ScreenMirrorService(Preferences(hub.prefs.path))
        assert again.window_mode().id == "maximized"

    def test_starting_in_maximized_waits_for_the_window_then_maximizes(self, hub: Harness) -> None:
        hub.service.set_window_mode("maximized")
        hub.adb_output = DEVICES_CABLED_ONLY
        hub.expect(["Device ready"])
        hub.service.start_mirror()
        _wait_phase(hub, "running")
        deadline = time.time() + 2.0
        while not hub.window_calls and time.time() < deadline:
            time.sleep(0.05)
        assert hub.window_calls == [(hub.window_handle, "maximized")]

    def test_the_panel_gets_the_display_options_in_its_snapshot(self, hub: Harness) -> None:
        hub.service.apply_window_mode("maximized")
        hub.service.set_stretch(True)
        status = hub.service.status()
        assert status["window"] == "maximized" and status["stretch"] is True
        assert [row["id"] for row in status["windows"]] == ["window", "maximized", "fullscreen"]
        assert all({"id", "label", "note"} <= set(row) for row in status["windows"])

    def test_the_note_survives_the_next_poll(self, hub: Harness) -> None:
        """面板每 1.5 秒轮询一次：只在返回值里给一次的提示会被下一次轮询擦掉。"""
        hub.adb_output = DEVICES_CABLED_ONLY
        hub.expect(["Device ready"])
        hub.service.start_mirror()
        _wait_phase(hub, "running")
        hub.service.apply_window_mode("fullscreen")
        assert "重开" in hub.service.status()["error"]
        assert "重开" in hub.service.status()["error"], "第二次阅读（就是那次轮询）还在"

    def test_a_new_session_clears_the_old_note(self, hub: Harness) -> None:
        hub.adb_output = DEVICES_CABLED_ONLY
        hub.expect(["Device ready"])
        hub.service.start_mirror()
        _wait_phase(hub, "running")
        hub.service.apply_window_mode("fullscreen")
        hub.service.stop_mirror()
        assert hub.service.status()["error"] == "", "停下之后不该还挂着「要重开一次」"
        hub.service.start_mirror()
        _wait_phase(hub, "running")
        assert hub.service.status()["error"] == ""

    def test_a_window_that_never_appears_says_so(self, hub: Harness) -> None:
        hub.adb_output = DEVICES_CABLED_ONLY
        hub.expect(["Device ready"])
        hub.service.start_mirror()
        _wait_phase(hub, "running")
        hub.window_handle = 0
        hub.service._nudge_maximized()
        assert hub.window_calls == [], "没有窗口就不该假装点过了"
        assert "没等到投屏窗口" in hub.service.status()["error"]

    def test_a_window_that_did_appear_leaves_no_complaint(self, hub: Harness) -> None:
        hub.adb_output = DEVICES_CABLED_ONLY
        hub.expect(["Device ready"])
        hub.service.start_mirror()
        _wait_phase(hub, "running")
        hub.window_calls.clear()
        hub.service._nudge_maximized()
        assert hub.window_calls == [(hub.window_handle, "maximized")]
        assert hub.service.status()["error"] == ""


class TestDisplayDoor:
    def test_the_door_remembers_and_reports_what_is_left(self, hub: Harness) -> None:
        from jarvis.ui.desktop import HudBridge

        bridge = HudBridge.__new__(HudBridge)
        bridge._mirror = hub.service
        out = bridge.mirror_display("maximized", True)
        assert out["window"] == "maximized" and out["stretch"] is True and out["error"] == ""
        assert hub.procs == [], "改显示方式不该自己起进程"

    def test_the_door_answers_for_a_bridge_with_no_mirror_service(self) -> None:
        from jarvis.ui.desktop import HudBridge

        bridge = HudBridge.__new__(HudBridge)
        out = bridge.mirror_display("fullscreen", False)
        assert out["phase"] == "failed" and out["available"] is False
        assert out["window"] == "window" and out["windows"] == []
        assert out["stretch"] is False, "没有服务时不能说填满开着"


class TestPhoneInjection:
    """那七颗按钮走 adb 注入：发给谁、失败怎么说，都得有据。"""

    def _running(self, hub: Harness) -> None:
        hub.adb_output = DEVICES_CABLED_ONLY
        hub.expect(["Device ready"])
        hub.service.start_mirror()
        _wait_phase(hub, "running")
        hub.adb_calls.clear()
        hub.adb_script = []
        hub.adb_output = ""

    def test_back_is_the_keyevent_on_this_phone(self, hub: Harness) -> None:
        self._running(hub)
        status = hub.service.press_key("back")
        assert status["error"] == ""
        argv = hub.adb_calls[-1]
        assert argv[argv.index("-s") + 1] == "R5CT90QVZGE"
        assert argv[-4:] == ["shell", "input", "keyevent", "KEYCODE_BACK"]

    def test_home_and_recents_are_the_other_two(self, hub: Harness) -> None:
        self._running(hub)
        hub.service.press_key("home")
        assert hub.adb_calls[-1][-1] == "KEYCODE_HOME"
        hub.service.press_key("recents")
        assert hub.adb_calls[-1][-1] == "KEYCODE_APP_SWITCH"

    def test_an_unknown_button_never_reaches_adb(self, hub: Harness) -> None:
        self._running(hub)
        status = hub.service.press_key("power")
        assert "没有「power」" in status["error"]
        assert hub.adb_calls == [], "不认识的名字不能瞎发给手机"

    def test_pressing_while_idle_names_the_reason(self, hub: Harness) -> None:
        status = hub.service.press_key("back")
        assert "没在投屏" in status["error"] and "开始投屏" in status["error"]
        assert hub.adb_calls == []

    def test_the_idle_refusal_still_shows_on_the_next_poll(self, hub: Harness) -> None:
        """拒的那一句也要活过轮询：面板 1.5 秒后会再读一次状态。"""
        status = hub.service.press_key("back")
        assert "没在投屏" in status["error"]
        assert "没在投屏" in hub.service.status()["error"]

    def test_a_securityexception_becomes_a_sentence_about_the_rom(self, hub: Harness) -> None:
        self._running(hub)
        hub.adb_output = (
            "java.lang.SecurityException: Injecting input events requires "
            "INJECT_EVENTS permission"
        )
        status = hub.service.press_key("back")
        assert "SecurityException" in status["error"], "原文要带上，不然没人知道为什么"
        assert "USB 调试（安全设置）" in status["error"]

    def test_a_nonzero_exit_is_not_swallowed(self, hub: Harness) -> None:
        self._running(hub)
        hub.adb_returncode = 1
        hub.adb_output = "error: device offline"
        status = hub.service.press_key("home")
        assert "device offline" in status["error"]
        hub.adb_returncode = 0

    def test_the_refusal_survives_the_next_poll(self, hub: Harness) -> None:
        self._running(hub)
        hub.adb_output = "SecurityException: denied"
        hub.service.press_key("back")
        assert "denied" in hub.service.status()["error"]

    def test_a_fling_is_measured_on_this_phone(self, hub: Harness) -> None:
        self._running(hub)
        hub.script("Physical size: 1080x2400\n", "")
        status = hub.service.swipe("next")
        assert status["error"] == ""
        argv = hub.adb_calls[-1]
        assert argv[-8:] == ["input", "touchscreen", "swipe", "540", "1872", "540", "624", "320"]

    def test_the_down_direction_is_the_mirror_image(self, hub: Harness) -> None:
        self._running(hub)
        hub.script("Physical size: 1080x2400\n", "")
        hub.service.swipe("prev")
        argv = hub.adb_calls[-1][-8:]
        assert argv == ["input", "touchscreen", "swipe", "540", "624", "540", "1872", "320"]

    def test_an_override_resolution_wins_over_the_physical_one(self, hub: Harness) -> None:
        self._running(hub)
        hub.script("Physical size: 1080x2400\nOverride size: 720x1600\n", "")
        hub.service.swipe("next")
        assert hub.adb_calls[-1][-5:] == ["360", "1248", "360", "416", "320"]

    def test_the_screen_size_is_asked_once_per_phone(self, hub: Harness) -> None:
        self._running(hub)
        hub.script("Physical size: 1080x2400\n", "", "", "")
        hub.service.swipe("next")
        hub.service.swipe("prev")
        asked = [c for c in hub.adb_calls if c[-2:] == ["wm", "size"]]
        assert len(asked) == 1, "每按一次都问一遍 wm size 是白等一趟 adb"

    def test_a_screen_it_cannot_measure_refuses_to_guess(self, hub: Harness) -> None:
        self._running(hub)
        hub.script("adb: no permissions for 'wm'\n")
        status = hub.service.swipe("next")
        assert "读不到这台手机的屏幕尺寸" in status["error"]
        assert [
            c for c in hub.adb_calls if "swipe" in c
        ] == [], "猜一个分辨率就是往另一台手机上乱划"

    def test_the_snapshot_carries_the_seven_buttons(self, hub: Harness) -> None:
        status = hub.service.status()
        assert [row["id"] for row in status["keys"]] == [
            "back",
            "home",
            "recents",
            "vol_up",
            "vol_down",
        ]
        assert [row["id"] for row in status["swipes"]] == ["next", "prev"]
        for row in [*status["keys"], *status["swipes"]]:
            assert row["label"] and row["note"]

    def test_the_keys_come_from_the_service_not_the_page(self) -> None:
        """界面上那颗「返回」背后是什么，只有一份定义。"""
        assert [key.label for key in sm.PHONE_KEYS] == [
            "返回",
            "主页",
            "多任务",
            "音量 +",
            "音量 −",
        ]
        assert sm.phone_key_by_id("nope") is None
        assert sm.swipe_by_id("nope") is None


class TestOnePhoneSeveralTransports:
    """``adb devices`` 的行数不等于手机台数。

    实测那台 vivo：一根 USB 线 + 两个 ``adb tcpip`` 端口 = **三行**，三行的
    ``ro.serialno`` 全等于 ``10AE1F1RQE003N3``。按行数判"连着几台"会让这台手机永远
    落在"不猜"那条规则里 —— 表现代界面上一句「没有连着能读的手机」，而线就插着。
    """

    USB = "10AE1F1RQE003N3"
    WIFI_A = "192.168.1.6:5555"
    WIFI_B = "192.168.1.8:5555"

    def _rows(self, monkeypatch: pytest.MonkeyPatch, hub: Harness, serials: list[str]) -> None:
        from typing import cast

        rows = [sm.Device(serial=item, state="device", model="V2309A") for item in serials]
        monkeypatch.setattr(hub.service, "devices", cast("Any", lambda: (rows, "")))

    def _shot_targets(self, hub: Harness) -> str:
        shots = [call for call in hub.adb_calls if "screencap" in " ".join(call)]
        return shots[0][shots[0].index("-s") + 1] if shots else ""

    def _serial_asks(self, hub: Harness) -> int:
        return len([call for call in hub.adb_calls if "ro.serialno" in " ".join(call)])

    def test_three_transports_of_one_phone_pick_the_cable(
        self, hub: Harness, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # 顺序是故意的：WiFi 排在前面，"拿第一个"和"挑 USB"才分得出来。
        self._rows(monkeypatch, hub, [self.WIFI_A, self.USB, self.WIFI_B])
        hub.script(f"{self.USB}\n", f"{self.USB}\n", f"{self.USB}\n", b"")
        hub.service.capture_frame()
        assert (
            self._shot_targets(hub) == self.USB
        ), "同一台手机的几个通道里挑 USB：不占 WiFi、不掉线"

    def test_two_real_phones_still_refuse(
        self, hub: Harness, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._rows(monkeypatch, hub, [self.USB, "R5CT90QVZGE"])
        hub.script(f"{self.USB}\n", "R5CT90QVZGE\n")
        data, error = hub.service.capture_frame()
        assert data == b"" and "没有连着能读的手机" in error
        assert [call for call in hub.adb_calls if "screencap" in " ".join(call)] == []

    def test_a_phone_that_will_not_tell_its_serial_is_not_merged(
        self, hub: Harness, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """getprop 空答时宁可当成几台多问一句 —— 合成一台就可能按错手机。"""
        self._rows(monkeypatch, hub, [self.USB, self.WIFI_A, self.WIFI_B])
        hub.script("\n", "\n", "\n")
        _, error = hub.service.capture_frame()
        assert "没有连着能读的手机" in error

    def test_the_serial_is_asked_once_per_transport(
        self, hub: Harness, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._rows(monkeypatch, hub, [self.USB, self.WIFI_A])
        hub.script(f"{self.USB}\n", f"{self.USB}\n", b"", b"")
        hub.service.capture_frame()
        asked = self._serial_asks(hub)
        hub.service.capture_frame()
        assert asked == 2, f"两台手机该问两次，实际问了 {asked} 次"
        assert self._serial_asks(hub) == 2, "第二次不许把同一批 getprop 再问一遍"

    def test_one_transport_never_asks_for_a_serial(
        self, hub: Harness, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """只有一行时没有歧义，别为不存在的问题多花一次 adb。"""
        self._rows(monkeypatch, hub, [self.USB])
        hub.script(b"")
        hub.service.capture_frame()
        assert self._serial_asks(hub) == 0


#: 用户实拍那一台 vivo：一根 USB 线 + 两个 ``adb tcpip`` 端口，``adb devices`` 给三行，
#: 三行的 ``ro.serialno`` 全等于 ``10AE1F1RQE003N3``。顺序是故意的 —— 无线的排在前面，
#: 「按档位挑通道」和「拿第一行」才分得出来。
ONE_PHONE_THREE_CHANNELS = """List of devices attached
192.168.1.6:5555         device product:V2309A model:V2309A device:V2309A transport_id:3
10AE1F1RQE003N3          device product:V2309A model:V2309A device:V2309A transport_id:1
192.168.1.8:5555         device product:V2309A model:V2309A device:V2309A transport_id:2
"""


#: 同一台手机、同三个通道，只是 ``adb`` 这次先报了插线那一行。和上面那份配对用：
#: 一份挑得出「拿第一行」的错，另一份才挑得出「永远偏向插线」的错。
ONE_PHONE_CABLE_FIRST = """List of devices attached
10AE1F1RQE003N3          device product:V2309A model:V2309A device:V2309A transport_id:1
192.168.1.6:5555         device product:V2309A model:V2309A device:V2309A transport_id:3
192.168.1.8:5555         device product:V2309A model:V2309A device:V2309A transport_id:2
"""


class TestThePanelListsOneRowPerPhone:
    """连着**一台**手机，界面上不许出现三行。

    ``adb`` 按通道列设备：同一台手机插着线又开过两个无线端口就是三行。三行原样摆到
    「连着这台电脑的手机」下面，用户读到的就是"我连了三台一模一样的手机"（第 96 轮实拍）。
    并成一行还不够 —— 被并掉的那两个通道得写在行上，不然用户会以为它们消失了。
    """

    USB = "10AE1F1RQE003N3"
    WIFI_A = "192.168.1.6:5555"
    WIFI_B = "192.168.1.8:5555"

    def _three_lines(
        self,
        hub: Harness,
        answers: tuple[str, ...],
        listing: str = ONE_PHONE_THREE_CHANNELS,
    ) -> None:
        hub.script(listing, *answers)

    def _serials(self, rows: tuple[sm.Device, ...]) -> list[str]:
        return [row.serial for row in rows]

    def test_three_adb_lines_become_one_row(self, hub: Harness) -> None:
        self._three_lines(hub, (f"{self.USB}\n", f"{self.USB}\n", f"{self.USB}\n"))
        rows, error = hub.service.phones()
        assert error == ""
        assert self._serials(rows) == [self.USB], "并成一行，摆的是插线那一台（默认 USB 档）"
        assert rows[0].channels == (self.WIFI_A, self.USB, self.WIFI_B), "并掉的通道要留在行上"

    def test_the_channel_it_picked_follows_the_mode(self, hub: Harness) -> None:
        """切到 WiFi 档，那一行代表的还是同一台手机，但摆出来的是无线那个通道。

        档位是用户已经有的那一格，所以「这一行投起来走哪条路」跟着它走；另设一套挑选规则
        只会让界面和「开始投屏」各说各话。
        """
        hub.service.set_mode("wifi")
        # 这一份 adb 先把插线那一行报出来：拿第一行、或者永远偏向 USB，都会在这条上露馅。
        self._three_lines(
            hub, (f"{self.USB}\n", f"{self.USB}\n", f"{self.USB}\n"), ONE_PHONE_CABLE_FIRST
        )
        rows, _ = hub.service.phones()
        assert self._serials(rows) == [self.WIFI_A], "WiFi 档就该摆那条无线通道，但仍是一台"
        assert rows[0].channels == (self.USB, self.WIFI_A, self.WIFI_B)

    def test_two_different_phones_are_still_two_rows(self, hub: Harness) -> None:
        self._three_lines(hub, (f"{self.USB}\n", "R5CT90QVZGE\n", f"{self.USB}\n"))
        rows, _ = hub.service.phones()
        assert self._serials(rows) == [self.WIFI_A, self.USB], "第二行是另一台手机，不能并掉"
        assert len(rows) == 2

    def test_a_phone_that_will_not_name_itself_is_not_merged(self, hub: Harness) -> None:
        """``ro.serialno`` 答 ``unknown``（ROM 答不出时就给这个）就当两台多问一句。

        三行都答 ``unknown`` 时，认了它就是把三台并成一台 —— 而界面只显示一行，用户根本
        不知道少的是什么。并错一台是往别人手机上按键，比多列两行糟得多。
        """
        self._three_lines(hub, ("unknown\n", "unknown\n", "unknown\n"))
        rows, _ = hub.service.phones()
        assert self._serials(rows) == [self.WIFI_A, self.USB, self.WIFI_B]

    def test_adb_prose_is_not_a_serial(self, hub: Harness) -> None:
        """答回来的是一整句话（adb 自己的提示）也不能当成"这两行是同一台"的证据。"""
        self._three_lines(hub, ("List of devices attached\n",) * 3)
        rows, _ = hub.service.phones()
        assert self._serials(rows) == [self.WIFI_A, self.USB, self.WIFI_B]

    def test_a_row_that_has_not_granted_debugging_keeps_its_own_line(self, hub: Harness) -> None:
        """没允许调试的那一行合不来序列号，也不许被并进旁边那行。

        并进「可投屏」那一行会把「手机上弹了允许 USB 调试吗」这句要命的话藏掉 —— 而那正是
        用户唯一还该做的事。
        """
        hub.script(
            "List of devices attached\n"
            f"{self.USB}                  unauthorized model:V2309A transport_id:1\n"
            f"{self.WIFI_A}         device model:V2309A transport_id:2\n",
            f"{self.USB}\n",
        )
        rows, _ = hub.service.phones()
        assert self._serials(rows) == [self.USB, self.WIFI_A]
        unauthorized = [row for row in rows if not row.ready]
        assert len(unauthorized) == 1 and "允许 USB 调试" in unauthorized[0].how_to_fix
        asked = [" ".join(call) for call in hub.adb_calls if "ro.serialno" in " ".join(call)]
        assert len(asked) == 1 and self.WIFI_A in asked[0], f"只有打通的那条通道配被问一遍：{asked}"

    def test_one_line_never_asks_for_a_serial(self, hub: Harness) -> None:
        """一行没有歧义，别为不存在的问题多花一次 adb。"""
        hub.adb_output = DEVICES_CABLED_ONLY
        rows, _ = hub.service.phones()
        assert self._serials(rows) == ["R5CT90QVZGE"]
        assert all("ro.serialno" not in " ".join(call) for call in hub.adb_calls)

    def test_the_wifi_tier_starts_the_phone_instead_of_refusing_two(self, hub: Harness) -> None:
        """WiFi 档按「开始投屏」：以前数的是行，于是回「这一档同时连着 2 台」。"""
        hub.service.set_mode("wifi")
        self._three_lines(hub, (f"{self.USB}\n", f"{self.USB}\n"))
        status = hub.service.start_mirror()
        assert "同时连着" not in status["detail"]
        assert status["phase"] == "starting"
        argv = " ".join(hub.spawned[-1])
        assert f"--serial {self.WIFI_A}" in argv, f"起在了别的通道上：{argv}"

    def test_two_real_phones_still_refuse_to_be_guessed(self, hub: Harness) -> None:
        hub.service.set_mode("wifi")
        hub.script(
            "List of devices attached\n"
            f"{self.WIFI_A}         device model:V2309A transport_id:2\n"
            "192.168.1.9:5555       device model:Redmi_K60 transport_id:3\n",
            f"{self.USB}\n",
            "R5CT90QVZGE\n",
        )
        status = hub.service.start_mirror()
        assert "同时连着 2 台" in status["detail"]
        assert hub.spawned == []

    def test_the_bridge_door_hands_the_page_one_row_per_phone(self, hub: Harness) -> None:
        from jarvis.ui.desktop import HudBridge

        self._three_lines(hub, (f"{self.USB}\n", f"{self.USB}\n", f"{self.USB}\n"))
        bridge = HudBridge.__new__(HudBridge)
        bridge._mirror = hub.service
        listed = bridge.mirror_devices()
        assert len(listed["devices"]) == 1
        row = listed["devices"][0]
        assert row["serial"] == self.USB
        assert row["channels"] == [self.WIFI_A, self.USB, self.WIFI_B]

    def test_she_says_one_phone_not_three_devices(self, hub: Harness) -> None:
        """她念的设备清单和用户看的是同一份：行数当台数，报出去就是错的。"""
        self._three_lines(hub, (f"{self.USB}\n", f"{self.USB}\n", f"{self.USB}\n"))
        tools = mirror_tools.build(hub.service)
        text = _handler(tools, "mirror_devices")({})
        assert "连着 1 台手机" in text, text
        assert text.count("V2309A") == 1
        assert self.WIFI_B in text, "别的通道得说出来，不然她以为只有一条路"


class TestInjectionDoors:
    def test_the_doors_pass_straight_through(self, hub: Harness) -> None:
        from jarvis.ui.desktop import HudBridge

        bridge = HudBridge.__new__(HudBridge)
        bridge._mirror = hub.service
        hub.adb_output = DEVICES_CABLED_ONLY
        hub.expect(["Device ready"])
        hub.service.start_mirror()
        _wait_phase(hub, "running")
        hub.adb_calls.clear()
        out = bridge.mirror_key("recents")
        assert out["error"] == "" and hub.adb_calls[-1][-1] == "KEYCODE_APP_SWITCH"
        hub.script("Physical size: 1080x2400\n", "")
        out = bridge.mirror_swipe("prev")
        assert out["error"] == ""
        # 要认得出"传的是哪一个方向"，不然门里写死一个方向也测不出来。
        assert hub.adb_calls[-1][-8:] == [
            "input",
            "touchscreen",
            "swipe",
            "540",
            "624",
            "540",
            "1872",
            "320",
        ]

    def test_a_bridge_without_the_service_still_has_the_full_shape(self) -> None:
        from jarvis.ui.desktop import HudBridge

        bridge = HudBridge.__new__(HudBridge)
        out = bridge.mirror_key("back")
        assert out["available"] is False and out["phase"] == "failed"
        assert out["keys"] == [] and out["swipes"] == []
        assert out["error"] == "这个进程没有接投屏服务"
