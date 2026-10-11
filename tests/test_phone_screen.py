"""r99：adb 读屏原语的测试，接在投屏那一套夹具上跑（假 adb，真形状）。

fixtures 的写法照 2026-10-10 在 vivo V2309A 上量到的形状来，内容换成假的：
包名/Activity 的形状、text= 与 content-desc= 都出现、一行一条节点。
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import pytest

from jarvis.app import screen_mirror as sm
from jarvis.app.phone_screen import PhoneMirror, PhoneScreen, PhoneScreenReader
from jarvis.app.preferences import MIRROR_ALLOW_SCREEN_VISION, Preferences
from jarvis.llm import ChatMessage, LlmClient
from jarvis.tools.builtins import mirror_tools
from jarvis.tools.builtins.mirror_tools import PhoneScreenSurface
from jarvis.tools.types import ToolHandler
from jarvis.ui.desktop import HudBridge
from tests.test_screen_mirror import (
    DEVICES_CABLED_ONLY,
    Harness,
    StubMirror,
    _wait_phase,
)

REAL_FOCUS = """  mCurrentFocus=Window{2f37dc2 u0 com.example.notes/com.example.notes.MainActivity
 type=1 }
  mFocusedApp=ActivityRecord{149233594 u0 com.example.notes/.MainActivity t499 d0}
"""

REAL_DUMP = """<?xml version='1.0' encoding='UTF-8' standalone='yes' ?>
<hierarchy rotation="0">
  <node index="0" text="" content-desc="" bounds="[0,0][1260,2800]">
    <node text="关于本机" content-desc="" bounds="[40,120][300,180]" />
    <node text="" content-desc="返回" bounds="[0,0][120,120]" />
    <node text="型号 V2309A" content-desc="" bounds="[40,300][600,360]" />
    <node text="型号 V2309A" content-desc="" bounds="[40,400][600,460]" />
    <node text="版本 &amp; 更新" content-desc="" bounds="[40,500][600,560]" />
  </node>
</hierarchy>
UI hierarchy dumped to: /dev/tty"""

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 40


def _running(hub: Harness) -> None:
    hub.adb_output = DEVICES_CABLED_ONLY
    hub.expect(["Device ready"])
    hub.service.start_mirror()
    _wait_phase(hub, "running")
    hub.adb_calls.clear()
    hub.adb_script = []
    hub.adb_output = ""


class TestScreenParsers:
    def test_the_focus_line_is_read_the_way_the_real_phone_writes_it(self) -> None:
        app, activity, note = sm.parse_focused_app(REAL_FOCUS)
        assert (app, activity, note) == (
            "com.example.notes",
            "com.example.notes.MainActivity",
            "",
        )

    def test_a_window_without_an_activity_half_still_gives_the_package(self) -> None:
        app, _activity, note = sm.parse_focused_app(
            "  mCurrentFocus=Window{a1 u0 SurfaceView - com.a.b }"
        )
        assert app.startswith("SurfaceView") or app == "SurfaceView - com.a.b"
        assert note == ""

    def test_a_lock_screen_says_so_instead_of_returning_nothing_quietly(self) -> None:
        app, activity, note = sm.parse_focused_app("  mCurrentFocus=null")
        assert (app, activity) == ("", "")
        assert "锁屏" in note

    def test_an_unrecognised_dumpsys_is_a_sentence_not_a_crash(self) -> None:
        _app, _activity, note = sm.parse_focused_app("some other android build")
        assert "读不出当前 App" in note

    def test_widget_texts_keep_order_drop_blanks_and_decode_entities(self) -> None:
        texts = sm.parse_widget_texts(REAL_DUMP)
        assert texts[:3] == ["关于本机", "返回", "型号 V2309A"]
        assert texts.count("型号 V2309A") == 1, "重复节点该并成一条"
        assert "版本 & 更新" in texts, "content 里的 &amp; 要还原成 &"

    def test_a_runaway_screen_is_capped(self) -> None:
        xml = "".join(f'<node text="第{i}条" bounds="[0,0][1,1]" />' for i in range(200))
        texts = sm.parse_widget_texts(xml)
        assert len(texts) == 60


class TestScreenPrimitives:
    def test_screencap_goes_out_as_exec_out_not_shell(self, hub: Harness) -> None:
        _running(hub)
        hub.script(PNG)
        frame, error = hub.service.capture_frame()
        assert error == "" and frame == PNG
        argv = hub.adb_calls[-1]
        assert argv[argv.index("exec-out") + 1 :] == [
            "screencap",
            "-p",
        ], "shell 会把 PNG 的换行翻了"

    def test_a_frame_that_is_not_a_png_is_named(self, hub: Harness) -> None:
        _running(hub)
        hub.script(b"not a png at all")
        frame, error = hub.service.capture_frame()
        assert frame == b""
        assert "不是 PNG" in error, "screencap 被改了要说出来，不能当一张空图吞掉"

    def test_reading_the_screen_asks_for_app_then_text(self, hub: Harness) -> None:
        _running(hub)
        hub.script(REAL_FOCUS, REAL_DUMP)
        read = hub.service.read_screen()
        assert read["app"] == "com.example.notes"
        assert read["widgets"][:2] == ["关于本机", "返回"]
        assert read["error"] == ""
        assert hub.adb_calls[0][-2:] == ["dumpsys", "window"]
        assert hub.adb_calls[1][-3:] == ["uiautomator", "dump", "/dev/tty"]

    def test_two_phones_are_refused_rather_than_guessed(
        self, hub: Harness, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """两台**不同**的手机：不许猜。一行一台的假 adb 会被去重逻辑合成一台，
        所以这里得让每台回自己的 ``ro.serialno``。"""
        from typing import cast

        rows = [
            sm.Device(serial="R5CT90QVZGE", state="device"),
            sm.Device(serial="ZZ9A", state="device"),
        ]
        monkeypatch.setattr(hub.service, "devices", cast("Any", lambda: (rows, "")))
        hub.script("R5CT90QVZGE\n", "ZZ9A\n")
        read = hub.service.read_screen()
        assert "没剩下唯一那台" in read["error"]
        assert hub.adb_calls == [] or all("dumpsys" not in c for c in hub.adb_calls)

    def test_the_mirrored_phone_wins_over_the_one_adb_lists(self, hub: Harness) -> None:
        _running(hub)
        hub.script(REAL_FOCUS, REAL_DUMP)
        read = hub.service.read_screen()
        assert read["app"] == "com.example.notes"
        assert all("devices" not in c for c in hub.adb_calls), "在投的那台已经定了，不必再问 adb"

    def test_a_rom_that_blocks_uiautomator_says_that_out_loud(self, hub: Harness) -> None:
        _running(hub)
        hub.script(REAL_FOCUS, "ERROR: could not get idle state.")
        read = hub.service.read_screen()
        assert "ROM" in read["note"] or "挡" in read["note"]
        assert read["widgets"] == []

    def test_a_screen_with_no_text_is_called_a_fact_not_a_failure(self, hub: Harness) -> None:
        _running(hub)
        hub.script(REAL_FOCUS, '<hierarchy rotation="0"></hierarchy>')
        read = hub.service.read_screen()
        assert read["widgets"] == []
        assert "没吐出文字" in read["note"]
        assert read["error"] == ""


class _NoFrameMirror(PhoneMirror):
    def __init__(self, read: dict[str, object], frame: tuple[bytes, str] = (b"", "没该拿")) -> None:
        self._read = read
        self._frame = frame
        self.captured = 0

    def read_screen(self) -> dict[str, object]:
        return self._read

    def capture_frame(self) -> tuple[bytes, str]:
        self.captured += 1
        return self._frame


class _Engine:
    """``OcrService`` as the reader sees it: ``running`` plus a ``recognize`` that answers."""

    def __init__(
        self, running: bool = True, text: str = "识别出来的字", raises: bool = False
    ) -> None:
        self.running = running
        self._text = text
        self._raises = raises

    def recognize(self, image: bytes) -> object:
        if self._raises:
            raise RuntimeError("模型没下来")
        return type("R", (), {"text": self._text})()


class _Client:
    provider_name = "qwen"
    model = "qwen-vl-max"

    def __init__(self, raises: bool = False) -> None:
        self.raises = raises
        self.sent: list[ChatMessage] = []

    def stream(self, messages, **options):  # type: ignore[no-untyped-def]
        del messages, options
        return iter(())

    def complete(self, messages: object, **options: object) -> object:
        del options
        assert isinstance(messages, list)
        self.sent.append(cast("ChatMessage", messages[0]))
        if self.raises:
            raise RuntimeError("400 that model cannot see images")
        return type("Resp", (), {"content": "这是设置页，最上面写着「关于本机」"})()


@pytest.fixture
def hub(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[Harness]:
    harness = Harness(monkeypatch, tmp_path)
    try:
        yield harness
    finally:
        harness.close()


@pytest.fixture()
def reader(tmp_path: Path) -> PhoneScreenReader:
    mirror = _NoFrameMirror(
        {
            "app": "com.example.notes",
            "activity": ".MainActivity",
            "widgets": ["关于本机"],
            "note": "",
            "error": "",
        }
    )
    return PhoneScreenReader(mirror, prefs=Preferences(tmp_path / "preferences.json"))


class TestPhoneScreenReader:
    def test_the_accessibility_tree_alone_answers_without_touching_the_camera(
        self, tmp_path: Path
    ) -> None:
        mirror = _NoFrameMirror(
            {
                "app": "com.example",
                "activity": ".Main",
                "widgets": ["a", "b"],
                "note": "",
                "error": "",
            }
        )
        report = PhoneScreenReader(mirror, prefs=Preferences(tmp_path / "preferences.json")).read()
        assert report.app == "com.example"
        assert report.widgets == ("a", "b")
        assert mirror.captured == 0, "没人要图就别花那 0.8 秒、也别把手机画面变成一张图"
        assert any("截图：没拿" in line for line in report.sources)

    def test_a_local_engine_makes_the_picture_worth_taking(self, tmp_path: Path) -> None:
        mirror = _NoFrameMirror(
            {
                "app": "com.example",
                "activity": "",
                "widgets": [],
                "note": "这一屏没吐出文字",
                "error": "",
            },
            (PNG, ""),
        )
        report = PhoneScreenReader(
            mirror, ocr_provider=lambda: _Engine(), prefs=Preferences(tmp_path / "preferences.json")
        ).read()
        assert mirror.captured == 1
        assert report.ocr == "识别出来的字"
        assert any("本地 OCR：读到" in line for line in report.sources)

    def test_a_missing_engine_is_explained_not_faked(self, tmp_path: Path) -> None:
        mirror = _NoFrameMirror(
            {"app": "com.example", "activity": "", "widgets": [], "note": "", "error": ""},
            (PNG, ""),
        )
        report = PhoneScreenReader(
            mirror,
            ocr_provider=lambda: _Engine(running=False),
            vision_provider=lambda: cast("LlmClient", _Client()),
            vision_ready=lambda: True,
            prefs=Preferences(tmp_path / "preferences.json"),
        ).read()
        assert report.ocr == "" and report.caption == ""
        assert mirror.captured == 0, "引擎没装就别白截那张图"
        assert any("rapidocr" in line for line in report.sources)

    def test_the_picture_only_leaves_when_the_operator_says_so(self, tmp_path: Path) -> None:
        path = tmp_path / "preferences.json"
        prefs = Preferences(path)
        mirror = _NoFrameMirror(
            {"app": "com.example", "activity": "", "widgets": [], "note": "", "error": ""},
            (PNG, ""),
        )
        client = _Client()
        reader = PhoneScreenReader(
            mirror,
            vision_provider=lambda: cast("LlmClient", client),
            vision_ready=lambda: True,
            prefs=prefs,
        )
        off = reader.read()
        assert off.caption == "" and client.sent == [] and mirror.captured == 0
        assert prefs.set(MIRROR_ALLOW_SCREEN_VISION, True)
        on = reader.read("这个订单号是多少")
        assert "这是设置页" in on.caption
        assert client.sent and "这个订单号是多少" in str(client.sent[0].content)
        assert any("qwen/qwen-vl-max" in line for line in on.sources), "谁答的要能认出来"

    def test_a_model_without_eyes_is_not_sent_the_picture(self, tmp_path: Path) -> None:
        prefs = Preferences(tmp_path / "preferences.json")
        prefs.set(MIRROR_ALLOW_SCREEN_VISION, True)
        client = _Client()
        report = PhoneScreenReader(
            _NoFrameMirror(
                {"app": "com.example", "activity": "", "widgets": [], "note": "", "error": ""},
                (PNG, ""),
            ),
            vision_provider=lambda: cast("LlmClient", client),
            vision_ready=lambda: False,
            prefs=prefs,
        ).read()
        assert client.sent == []
        assert any("没被量出有眼睛" in line for line in report.sources)

    def test_a_model_that_fails_leaves_the_tree_answer_intact(self, tmp_path: Path) -> None:
        prefs = Preferences(tmp_path / "preferences.json")
        prefs.set(MIRROR_ALLOW_SCREEN_VISION, True)
        report = PhoneScreenReader(
            _NoFrameMirror(
                {
                    "app": "com.example",
                    "activity": "",
                    "widgets": ["关于本机"],
                    "note": "",
                    "error": "",
                },
                (PNG, ""),
            ),
            vision_provider=lambda: cast("LlmClient", _Client(raises=True)),
            vision_ready=lambda: True,
            prefs=prefs,
        ).read("这是什么")
        assert report.widgets == ("关于本机",)
        assert report.caption == ""
        assert "没答上" in "；".join(report.sources)

    def test_an_oversized_frame_is_not_pushed_at_the_model(self, tmp_path: Path) -> None:
        prefs = Preferences(tmp_path / "preferences.json")
        prefs.set(MIRROR_ALLOW_SCREEN_VISION, True)
        client = _Client()
        report = PhoneScreenReader(
            _NoFrameMirror(
                {"app": "com.example", "activity": "", "widgets": [], "note": "", "error": ""},
                (b"\x89PNG\r\n\x1a\n" + b"0" * 5_000_000, ""),
            ),
            vision_provider=lambda: cast("LlmClient", client),
            vision_ready=lambda: True,
            prefs=prefs,
        ).read()
        assert client.sent == []
        assert "太大" in "；".join(report.sources)

    def test_a_crowded_screen_is_not_dumped_whole_into_the_request(self, tmp_path: Path) -> None:
        """一屏能报 60 条，全塞进上下文只是把它吃掉。读到几条、喂了几条要分开说。"""
        report = PhoneScreenReader(
            _NoFrameMirror(
                {
                    "app": "com.example",
                    "activity": "",
                    "widgets": [f"第 {index} 条" for index in range(60)],
                    "note": "",
                    "error": "",
                }
            ),
            prefs=Preferences(tmp_path / "preferences.json"),
        ).read()
        assert len(report.widgets) == 40
        assert any("读到 60 条文字，只喂了前 40 条" in line for line in report.sources)

    def test_a_dead_read_still_returns_a_sentence(self, tmp_path: Path) -> None:
        report = PhoneScreenReader(
            _NoFrameMirror(
                {"app": "", "activity": "", "widgets": [], "note": "", "error": "没连着能读的手机"}
            ),
            prefs=Preferences(tmp_path / "preferences.json"),
        ).read()
        assert not report.usable
        assert "没连着" in report.to_text()

    def test_the_answer_text_names_the_app_the_tree_and_the_sources(
        self, reader: PhoneScreenReader
    ) -> None:
        text = reader.read().to_text()
        assert "手机上现在是：com.example.notes/.MainActivity" in text
        assert "关于本机" in text
        assert "来源：" in text

    def test_the_switch_reports_a_refused_write_instead_of_lying(self, tmp_path: Path) -> None:
        prefs = Preferences(tmp_path / "preferences.json")
        reader = PhoneScreenReader(
            _NoFrameMirror({"app": "", "activity": "", "widgets": [], "note": "", "error": ""}),
            prefs=prefs,
        )
        assert reader.set_vision_allowed(True) == ""
        assert reader.vision_allowed() is True
        no_prefs = PhoneScreenReader(
            _NoFrameMirror({"app": "", "activity": "", "widgets": [], "note": "", "error": ""}),
            prefs=None,
        )
        assert "改不了" in no_prefs.set_vision_allowed(True)
        assert no_prefs.vision_allowed() is False


def _specs(reader: PhoneScreenSurface | None) -> set[str]:
    return {spec.name for spec, _ in mirror_tools.build(StubMirror(), reader)}


class TestPhoneScreenTool:

    def test_the_tool_is_advertised_only_with_a_reader(self, tmp_path: Path) -> None:
        reader = PhoneScreenReader(
            _NoFrameMirror({"app": "", "activity": "", "widgets": [], "note": "", "error": ""}),
            prefs=Preferences(tmp_path / "preferences.json"),
        )
        assert "phone_screen_read" in _specs(reader)
        assert "phone_screen_read" not in _specs(None)

    def test_the_question_travels_to_the_reader(self, tmp_path: Path) -> None:
        seen: list[str] = []

        class Spy:
            def read(self, question: str = "") -> mirror_tools.PhoneScreenAnswer:
                seen.append(question)
                return _Answer("手机上现在是：com.example")

        out = _handler(Spy())({"question": "验证码是多少"})
        assert seen == ["验证码是多少"]
        assert "com.example" in out

    def test_the_tool_is_marked_caution_and_read_only(self, tmp_path: Path) -> None:
        reader = PhoneScreenReader(
            _NoFrameMirror({"app": "", "activity": "", "widgets": [], "note": "", "error": ""}),
            prefs=Preferences(tmp_path / "preferences.json"),
        )
        specs = [spec for spec, _ in mirror_tools.build(StubMirror(), reader)]
        tool = next(spec for spec in specs if spec.name == "phone_screen_read")
        assert tool.risk.value == "caution"
        assert "只读" in tool.description


class _Answer:
    """The reader's report, as far as the tool cares: it has one method, ``to_text``."""

    def __init__(self, text: str) -> None:
        self._text = text

    def to_text(self) -> str:
        return self._text


def _handler(reader: PhoneScreenSurface) -> ToolHandler:
    specs = mirror_tools.build(StubMirror(), reader)
    return next(handler for spec, handler in specs if spec.name == "phone_screen_read")


class TestScreenDoor:
    def _bridge(self, prefs: Preferences) -> HudBridge:
        bridge = HudBridge.__new__(HudBridge)
        bridge._phone = PhoneScreenReader(
            _NoFrameMirror(
                {
                    "app": "com.example",
                    "activity": ".Main",
                    "widgets": ["关于本机"],
                    "note": "",
                    "error": "",
                }
            ),
            prefs=prefs,
        )
        return bridge

    def test_the_door_carries_the_answer_and_the_sources(self, tmp_path: Path) -> None:
        out = self._bridge(Preferences(tmp_path / "preferences.json")).mirror_screen("这是什么")
        assert "com.example" in str(out["text"])
        assert out["sources"]
        assert out["allow_vision"] is False, "没勾的时候不能说已经允许发图"

    def test_the_screen_door_reports_the_switch_as_it_actually_is(self, tmp_path: Path) -> None:
        """写死成 False 的门：勾了开关、发了图，屏幕上还说「没发给模型」。"""
        prefs = Preferences(tmp_path / "preferences.json")
        prefs.set(MIRROR_ALLOW_SCREEN_VISION, True)
        assert self._bridge(prefs).mirror_screen("")["allow_vision"] is True

    def test_the_vision_door_writes_the_switch_and_shows_it_again(self, tmp_path: Path) -> None:
        bridge = HudBridge.__new__(HudBridge)
        prefs = Preferences(tmp_path / "preferences.json")
        bridge._phone = PhoneScreenReader(
            _NoFrameMirror(
                {"app": "com.example", "activity": "", "widgets": [], "note": "", "error": ""}
            ),
            prefs=prefs,
        )
        assert bridge.mirror_screen("")["allow_vision"] is False
        after = bridge.mirror_screen_vision(True)
        assert after["allow_vision"] is True
        assert prefs.flag(MIRROR_ALLOW_SCREEN_VISION, default=False) is True

    def test_a_bridge_without_the_reader_says_so_completely(self) -> None:
        bridge = HudBridge.__new__(HudBridge)
        out = bridge.mirror_screen("")
        assert out["text"] == "这个进程没接手机读屏。"
        assert out["sources"] == [] and out["allow_vision"] is False
        vision = bridge.mirror_screen_vision(True)
        assert vision["text"] == "这个进程没接手机读屏。"
        assert set(vision) == set(out), "两扇门的回答必须是同一副形状"

    def test_toggling_the_switch_does_not_read_the_screen_again(self, tmp_path: Path) -> None:
        """勾一格开关不该付一次 dumpsys + uiautomator（实测约 3 秒），也不该擦掉刚读的那段。"""

        class Counting(PhoneScreenReader):
            reads = 0

            def read(self, question: str = "") -> PhoneScreen:
                Counting.reads += 1
                return super().read(question)

        bridge = HudBridge.__new__(HudBridge)
        bridge._phone = Counting(
            _NoFrameMirror(
                {
                    "app": "com.example",
                    "activity": "",
                    "widgets": ["关于本机"],
                    "note": "",
                    "error": "",
                }
            ),
            prefs=Preferences(tmp_path / "preferences.json"),
        )
        bridge.mirror_screen("")
        before = Counting.reads
        out = bridge.mirror_screen_vision(True)
        assert Counting.reads == before, "这扇门不许重新读一遍屏"
        assert out["allow_vision"] is True and out["error"] == ""
        assert "可以收这张屏" in str(out["text"])

    def test_a_switch_that_reached_memory_but_not_disk_says_both(self, tmp_path: Path) -> None:
        bridge = HudBridge.__new__(HudBridge)
        prefs = Preferences(tmp_path / "preferences.json")
        bridge._phone = PhoneScreenReader(
            _NoFrameMirror({"app": "", "activity": "", "widgets": [], "note": "", "error": ""}),
            prefs=prefs,
        )
        # 让磁盘写不进去：把 preferences.json 的位置换成一个目录。
        (tmp_path / "preferences.json").mkdir(exist_ok=True)
        out = bridge.mirror_screen_vision(True)
        assert out["allow_vision"] is True, "这一格当场是生效的，不能说没生效"
        assert "没写进磁盘" in str(out["error"]), "但要说清重启会回去"
