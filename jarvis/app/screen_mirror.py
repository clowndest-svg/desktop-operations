"""Phone screen mirroring, driven through the shipped ``scrcpy`` + ``adb``.

Why scrcpy rather than a home-grown capture pipeline: the requirement is "must not
stutter", and that is a property of the encoder, the transport and the decoder working
together at 60fps over a link the buyer's WiFi controls. scrcpy is that pipeline,
already tuned over a decade of use -- hardware H.264/H.265 on the phone, a TCP stream
on the same ADB connection the phone already trusts, and an SDL window with a
hardware-accelerated renderer on this side. Writing my own would have traded a
proven number for an unproven one, and this program has no test phone to discover the
difference on.

What lives here instead is the part scrcpy does not do: turning ``adb``'s output into
a device list a person can read, mapping scrcpy's stderr into a sentence that says
what to do next, keeping one child process and noticing when its window was closed,
and remembering the quality tier per machine.

Two facts worth keeping in mind while editing:

* **``adb`` is a singleton server on port 5037.** If the operator also has Android
  Studio's adb running, the two versions negotiate by killing each other's server --
  the classic "adb server version doesn't match this client" message. That is a real
  failure a buyer will hit, so it is detected and named rather than left as a hang.
* **Nothing in this module has been run against a real phone.** The device-side
  behaviour is scrcpy's, which is well established; the parsing, state machine and
  error text here are covered by tests against a stubbed ``adb``, not by a live
  device. See ``docs/桌面运维手册.md`` (第 85 轮) for exactly what is and is not verified.
"""

from __future__ import annotations

import concurrent.futures
import dataclasses
import ipaddress
import logging
import os
import re
import socket
import subprocess
import threading
import time
from collections import deque
from collections.abc import Callable, Sequence
from enum import StrEnum
from pathlib import Path
from typing import Final, TypedDict

from jarvis.app.preferences import (
    MIRROR_AUDIO,
    MIRROR_KEEP_PHONE_SCREEN,
    MIRROR_MODE,
    MIRROR_STRETCH,
    MIRROR_TIER,
    MIRROR_WINDOW,
    Preferences,
)

logger = logging.getLogger("jarvis.app.screen_mirror")

VENDOR_DIRNAME: Final = "scrcpy"
SCRCPY_EXE: Final = "scrcpy.exe"
ADB_EXE: Final = "adb.exe"

_WINDOW_TITLE: Final = "小夜 · 手机投屏"


def scan_port(address: str, port: int, timeout: float = 0.25) -> bool:
    """Whether something answers on this TCP port right now.

    A connect attempt is the only honest probe here: a ping can be filtered while the
    port is wide open, and a full adb handshake belongs to the caller that follows.
    """
    try:
        with socket.create_connection((address, port), timeout=timeout):
            return True
    except OSError:
        return False


@dataclasses.dataclass(frozen=True, slots=True)
class Interface:
    """One IPv4 on one adapter, with the two facts the scan actually needs."""

    name: str
    address: str
    netmask: str
    active: bool


def interfaces() -> list[Interface]:
    """Every IPv4 this machine has, and whether its adapter is up.

    Read at call time -- a cable can be plugged in between two clicks -- and the netmask
    is kept because "first three octets plus 1..254" is wrong on a /23 (misses half the
    phones) and wasteful on a /25 (probes 128 addresses nobody is on). Both are common on
    home routers.
    """
    rows: list[Interface] = []
    try:
        import psutil

        stats = psutil.net_if_stats()
        for name, addresses in psutil.net_if_addrs().items():
            active = bool(getattr(stats.get(name), "isup", True))
            for row in addresses:
                if getattr(row, "family", None) != socket.AF_INET or not row.address:
                    continue
                rows.append(
                    Interface(
                        name=name,
                        address=str(row.address),
                        netmask=str(getattr(row, "netmask", "") or "255.255.255.0"),
                        active=active,
                    )
                )
    except Exception:  # pragma: no cover - psutil is optional on this path
        logger.debug("psutil 读不到网卡，改用主机名解析")
    return rows


def local_ipv4s() -> list[str]:
    """This machine's own usable IPv4s -- what to scan, and what is definitely not a phone."""
    found = [
        row.address for row in interfaces() if row.active and not row.address.startswith("127.")
    ]
    if found:
        return found
    try:
        raw = socket.gethostbyname_ex(socket.gethostname())[2]
    except OSError:
        logger.info("这台电脑连自己的 IPv4 都问不到，自动找没法用")
        return []
    return [str(item) for item in raw if not str(item).startswith("127.")]


_MAX_SCAN_NETWORK: Final = 1024
"""Largest network (host count) 「自动找」 will sweep. Bigger means fill in the address."""

_DISCOVERY_BUDGET: Final = 12.0
"""Whole-sweep ceiling in seconds, checked between targets. A button must not spin for a minute."""

_DISCOVERY_CONNECT_TIMEOUT: Final = 4.0
"""Per ``adb connect`` ceiling: a phone that accepts the port and then says nothing is
skipped, not waited on."""

_MAX_CONNECT_TARGETS: Final = 8
"""How many answering hosts to try connecting per click; the rest are reported as skipped."""

_TCPIP_PORT: Final = 5555
"""The port ``adb tcpip`` leaves adbd listening on. Fixed, unlike the random port the
Android 11+ 无线调试 dialog shows, which is why the two paths need separate buttons.
"""


class MirrorTierRow(TypedDict):
    """One quality option as the panel draws it."""

    id: str
    label: str
    note: str


class WindowRow(TypedDict):
    """One display option, with what it costs on the screen."""

    id: str
    label: str
    note: str


class MirrorModeRow(TypedDict):
    """One connection option, with the tradeoff the dropdown shows under it."""

    id: str
    label: str
    pros: str
    cons: str


class MirrorActionRow(TypedDict):
    """One button the panel can draw: what it is called and what that one does."""

    id: str
    label: str
    note: str


class ScreenRead(TypedDict):
    """What the phone itself says is on its screen.

    ``widgets`` is the accessibility tree's text, top-to-bottom. It is empty for a
    game, a video frame or a canvas-drawn page -- that is a fact about the screen,
    not a failure, and ``note`` says which.
    """

    app: str
    activity: str
    widgets: list[str]
    note: str
    error: str


class MirrorDeviceList(TypedDict):
    """The device door's payload: the rows, plus why there are none."""

    devices: list[MirrorDeviceRow]
    error: str


class MirrorDeviceRow(TypedDict):
    """One phone, already explained.

    Not one ``adb`` line -- :func:`group_by_phone` folds a phone's several transports
    into the one row the panel shows, and ``channels`` is what it folded.
    """

    serial: str
    state: str
    label: str
    model: str
    wireless: bool
    ready: bool
    hint: str
    channels: list[str]


class MirrorStatus(TypedDict):
    """The panel's whole snapshot, typed.

    This used to be ``dict[str, object]``, which is accurate about the JSON wire and
    useless on both sides of it: every reader had to cast, and the 48 type errors in
    ``tests/test_screen_mirror.py`` were exactly that -- ``"x" in status["detail"]``
    against an ``object``. The bridge still hands the page a plain dict.
    """

    phase: str
    detail: str
    serial: str
    available: bool
    tier: str
    tiers: list[MirrorTierRow]
    mode: str
    modes: list[MirrorModeRow]
    window: str
    windows: list[WindowRow]
    stretch: bool
    keys: list[MirrorActionRow]
    swipes: list[MirrorActionRow]
    audio: bool
    keep_screen: bool
    discovery: str
    seconds: int
    tail: list[str]
    error: str
    """The last thing the person asked for that could not be done, in one sentence.

    The service holds it in ``_note`` (so a poll 1.5 seconds later still shows it) and
    the bridge may overwrite it with a refusal it produced itself.
    """


@dataclasses.dataclass(frozen=True, slots=True)
class QualityTier:
    """One set of scrcpy flags, with the reason it is shaped that way."""

    id: str
    label: str
    max_size: int
    max_fps: int
    bit_rate: str
    video_codec: str
    note: str


TIERS: Final[tuple[QualityTier, ...]] = (
    QualityTier(
        id="smooth",
        label="流畅优先",
        max_size=800,
        max_fps=30,
        bit_rate="4M",
        video_codec="h264",
        note="画面缩到长边 800、30 帧、4Mbps。弱 WiFi / 老手机选这个，最不容易卡。",
    ),
    QualityTier(
        id="balanced",
        label="均衡（默认）",
        max_size=1024,
        max_fps=60,
        bit_rate="6M",
        video_codec="h264",
        note="长边 1024、60 帧、6Mbps，H.264 各家手机都支持，延迟和清晰度的折中。",
    ),
    QualityTier(
        id="sharp",
        label="清晰优先",
        max_size=1440,
        max_fps=60,
        bit_rate="12M",
        video_codec="h265",
        note="长边 1440、H.265。同带宽下更清楚，但手机要支持 H.265 编码，"
        "老机器上编码延迟反而更高 —— 卡了就退回均衡。",
    ),
    QualityTier(
        id="native",
        label="手机原分辨率",
        max_size=0,
        max_fps=120,
        bit_rate="16M",
        video_codec="h264",
        note="不缩放，直接推手机的全分辨率。只适合 5GHz/6GHz WiFi 或 USB 线，"
        "2.4GHz 上基本一定会卡 —— 这一档是给你对比用的。",
    ),
)

DEFAULT_TIER: Final = "balanced"


def tier_by_id(name: str) -> QualityTier:
    for tier in TIERS:
        if tier.id == name:
            return tier
    return next(t for t in TIERS if t.id == DEFAULT_TIER)


@dataclasses.dataclass(frozen=True, slots=True)
class ConnectionMode:
    """One way the phone can reach this computer, with the tradeoff spelled out.

    The panel shows ``pros`` and ``cons`` verbatim, so these two strings are the whole
    argument a person has before choosing. They are kept next to the code that acts on
    the choice so a change to the behaviour cannot drift away from the explanation.
    """

    id: str
    label: str
    pros: str
    cons: str
    hint: str
    """What to tell somebody who picked this mode and has no such device."""


USB_MODE: Final = "usb"
WIFI_MODE: Final = "wifi"
SCAN_MODE: Final = "scan"
"""Same WiFi, found by scanning port 5555 -- no 无线调试 dialog involved.

The name says what it is: after 「开无线端口」 the phone listens on a *fixed* port, so the
PC can go looking for it instead of making the person read an address off the phone.
"""

MODES: Final[tuple[ConnectionMode, ...]] = (
    ConnectionMode(
        id=USB_MODE,
        label="USB 线（开 USB 调试）",
        pros="最稳的一档：不占 WiFi、延迟最低、不受路由器和邻居信道影响，插上就能投，"
        "反手用鼠标操作手机也最跟手。",
        cons="要一根**能传数据**的线（有些线只能充电）；人离电脑就得拔线；"
        "手机被线占着，部分老手机换个 USB 口还要重新点一次允许调试。",
        hint="这一格只算插着 USB 线的那台：拿根能传数据的线连上，"
        "手机上勾「一律允许」点「允许 USB 调试」。",
    ),
    ConnectionMode(
        id=WIFI_MODE,
        label="同一个 WiFi（无线调试）",
        pros="不用线，手机拿在手里也能投；配过一次之后，重连只要点一下「连接」。"
        "笔记本在沙发上、主机在桌上的时候只有这一档能用。",
        cons="吃路由器：2.4GHz 或隔两堵墙就会卡，画质档要往下退；"
        "手机重启或换网络后**端口会变**，要重新连一次；"
        "Android 11 以上要先用「无线调试」的配对码配一次，10 及以下必须先插一次线"
        "点「开无线端口」；公司/公共 WiFi 开了 AP 隔离的连不上。",
        hint="这一格只算无线连着的那台：Android 11+ 在「开发者选项 → 无线调试」里"
        "配对一次；10 及以下先插着线点那一行的「开无线端口」。",
    ),
    ConnectionMode(
        id=SCAN_MODE,
        label="同一个 WiFi（不开无线调试，自动找）",
        pros="不用碰「无线调试」那个页面，也不用填 IP 和 6 位配对码：手机插一次线点"
        "「开无线端口」之后，端口就固定在 5555，以后每次点「自动找」由电脑把本网段的手机"
        "找出来并连上。老手机（Android 10 及以下）只有这一条无线路可走。",
        cons="**第一次那根线省不掉**：不开 USB 调试也不开无线调试，电脑上没有任何软件"
        "能让手机把屏幕交出来，这是安卓的安全模型，不是本软件的限制（不想插线的硬件路子"
        "是 USB-C 采集卡，另一件事）。5555 端口在同一网段里对所有人开放，"
        "公司/公共 WiFi 别用这一档；开了 AP 隔离的网段设备互相看不见，扫不到；"
        "手机重启后 tcpip 模式会失效，要再插一次线。",
        hint="这一格只算开着 5555 端口的那台：先插一次线点那一行的「开无线端口」，"
        "再点「自动找」。",
    ),
)


def in_mode(mode_id: str, row: Device) -> bool:
    """Whether this device is the kind the given 方式 is looking for.

    One function so the picker, the refusal text and the panel's buttons cannot each
    invent their own idea of "a wireless phone" -- ``scan`` cares about the *port*,
    which the other two do not.
    """
    if mode_id == USB_MODE:
        return not row.wireless
    if mode_id == SCAN_MODE:
        return row.wireless and row.serial.endswith(f":{_TCPIP_PORT}")
    return row.wireless


DEFAULT_MODE: Final = USB_MODE


def mode_by_id(name: str) -> ConnectionMode:
    for mode in MODES:
        if mode.id == name:
            return mode
    return next(mode for mode in MODES if mode.id == DEFAULT_MODE)


@dataclasses.dataclass(frozen=True, slots=True)
class WindowMode:
    """One way the scrcpy window should sit on the screen."""

    id: str
    label: str
    note: str


WINDOW_NORMAL: Final = "window"
WINDOW_MAXIMIZED: Final = "maximized"
WINDOW_FULLSCREEN: Final = "fullscreen"

WINDOW_MODES: Final[tuple[WindowMode, ...]] = (
    WindowMode(
        id=WINDOW_NORMAL,
        label="窗口",
        note="普通窗口，自己拖大小。竖屏手机在横屏显示器上左右会有黑边，这是比例问题不是坏了。",
    ),
    WindowMode(
        id=WINDOW_MAXIMIZED,
        label="铺满（最大化窗口）",
        note="把窗口撑到整个屏幕。画面仍按手机比例居中，所以竖屏手机左右依旧有黑边 —— "
        "要连黑边一起填上，勾下面的「拉伸填满」（会变形）。",
    ),
    WindowMode(
        id=WINDOW_FULLSCREEN,
        label="全屏",
        note="scrcpy 自己进全屏（等同窗口里按 F11）。这一档要重开一次投屏才生效，"
        "因为全屏状态是 SDL 持有的，从外面改不可靠。",
    ),
)

DEFAULT_WINDOW: Final = WINDOW_NORMAL


def window_mode_by_id(name: str) -> WindowMode:
    for mode in WINDOW_MODES:
        if mode.id == name:
            return mode
    return next(mode for mode in WINDOW_MODES if mode.id == DEFAULT_WINDOW)


@dataclasses.dataclass(frozen=True, slots=True)
class PhoneKey:
    """One of the three navigation buttons the panel offers."""

    id: str
    label: str
    keycode: str
    note: str


@dataclasses.dataclass(frozen=True, slots=True)
class SwipeAction:
    """One fling, given as ratios of the phone's own height.

    Ratios rather than pixels because no two screens match, and the gesture that always
    reads as a fling is "从下面那段划到上面那段" through the middle of the screen.
    """

    id: str
    label: str
    note: str
    from_ratio: float
    to_ratio: float
    duration_ms: int


PHONE_KEYS: Final[tuple[PhoneKey, ...]] = (
    PhoneKey(id="back", label="返回", keycode="KEYCODE_BACK", note="等于手机上那下返回键。"),
    PhoneKey(
        id="home",
        label="主页",
        keycode="KEYCODE_HOME",
        note="回桌面。投屏不会断，画面跟着变。",
    ),
    PhoneKey(id="recents", label="多任务", keycode="KEYCODE_APP_SWITCH", note="最近任务那个键。"),
    PhoneKey(
        id="vol_up",
        label="音量 +",
        keycode="KEYCODE_VOLUME_UP",
        note="媒体音量大一格。投屏放视频时不用伸手去按手机侧键。",
    ),
    PhoneKey(
        id="vol_down",
        label="音量 −",
        keycode="KEYCODE_VOLUME_DOWN",
        note="小一格。手机躺在一边够不着的时候用它。",
    ),
)

SWIPES: Final[tuple[SwipeAction, ...]] = (
    SwipeAction(
        id="next",
        label="下一个（往上滑）",
        note="就是手指从下往上划那么一下 —— 抖音、快手翻到下一条视频用它。",
        from_ratio=0.78,
        to_ratio=0.26,
        duration_ms=320,
    ),
    SwipeAction(
        id="prev",
        label="上一个（往下滑）",
        note="反着划那么一下，回到上一条。",
        from_ratio=0.26,
        to_ratio=0.78,
        duration_ms=320,
    ),
)


def phone_key_by_id(name: str) -> PhoneKey | None:
    return next((key for key in PHONE_KEYS if key.id == name), None)


def swipe_by_id(name: str) -> SwipeAction | None:
    return next((row for row in SWIPES if row.id == name), None)


_FOCUS = re.compile(r"mCurrentFocus=Window\{[^ ]+ u\d+ ([^ }/]+)(?:/([^ }]+))?")
"""``adb shell dumpsys window`` 里那一行，实测长这样：

    mCurrentFocus=Window{2f37dc2 u0 com.xingin.xhs/com.xingin.matrix.NoteDetailActivity type=1 }

``/`` 后面那半段有些窗口（桌面、输入法、系统层 Surface）根本没有，所以两个组都是可选的。
"""

_TEXT_ATTR = re.compile(r'\b(?:text|content-desc)="([^"]*)"')


def parse_focused_app(text: str) -> tuple[str, str, str]:
    """当前 App 的包名与 Activity；读不到时给一句能看的原因。"""
    match = _FOCUS.search(text)
    if not match:
        if "mCurrentFocus=null" in text or "mCurrentFocus=Surface" in text:
            return "", "", "这一屏没有可辨认的前台 App（可能在锁屏或系统层画面）"
        return "", "", "读不出当前 App（这台机器的 dumpsys 换了写法）"
    return match.group(1), (match.group(2) or "").strip(), ""


def parse_widget_texts(xml: str) -> list[str]:
    """从 uiautomator 的 XML 里按出现顺序取文字，去掉空串和重复。

    ``text`` 和 ``content-desc`` 都要：很多按钮只写 content-desc，而无障碍标签往往正是
    屏幕上看得见的那句。截断长度是防着有些 App 把一个段落塞进一个节点。
    """
    seen: set[str] = set()
    out: list[str] = []
    for raw in _TEXT_ATTR.findall(xml):
        value = raw.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">").strip()
        if not value or value in seen:
            continue
        seen.add(value)
        out.append(value[:120])
        if len(out) >= 60:
            break
    return out


def parse_screen_size(text: str) -> tuple[int, int] | None:
    """``adb shell wm size`` 里的宽高。有 Override（开了分辨率调整）就用那个。"""
    found: dict[str, tuple[int, int]] = {}
    for line in text.splitlines():
        label, _, raw = line.partition(":")
        match = re.fullmatch(r"\s*(\d+)\s*[xX×]\s*(\d+)\s*", raw)
        if not match:
            continue
        name = label.strip().lower()
        if name in ("override size", "physical size"):
            found[name] = (int(match.group(1)), int(match.group(2)))
    return found.get("override size") or found.get("physical size")


@dataclasses.dataclass(frozen=True, slots=True)
class Device:
    """One row of ``adb devices -l``, said in Chinese where the state matters.

    :func:`group_by_phone` also hands back a ``Device`` that stands for a whole phone:
    the serial of the channel it picked, with the other channels in ``channels``.
    """

    serial: str
    state: str
    model: str = ""
    product: str = ""
    device: str = ""
    transport_id: str = ""
    #: 这台手机的其余通道（``adb`` 会把一台列成好几行，见 :func:`group_by_phone`）。
    #: 只有 :meth:`ScreenMirrorService.phones` 会填；单行的时候留空，出口退回它自己。
    channels: tuple[str, ...] = ()

    @property
    def ready(self) -> bool:
        return self.state == "device"

    @property
    def wireless(self) -> bool:
        """``192.168.1.23:5555`` is a network device; a bare serial is USB."""
        return ":" in self.serial

    @property
    def label(self) -> str:
        """The name a person recognises. ``adb`` spells models with underscores."""
        raw = self.model or self.product or self.device or self.serial
        return raw.replace("_", " ")

    @property
    def how_to_fix(self) -> str:
        """What a person has to do about this row, or ``""`` when nothing is wrong.

        The states are not errors the user made -- ``unauthorized`` in particular is
        one tap on the phone -- so each one gets its own instruction rather than a
        generic "device unavailable".
        """
        if self.ready:
            return ""
        if self.state == "unauthorized":
            return "手机上应该弹了「允许 USB 调试吗？」，勾「一律允许」再点允许"
        if self.state == "no":
            return "权限不够，通常是同时开着 Android Studio 或某个手机助手，先关掉它们再重插一次线"
        if self.state == "offline":
            return (
                "设备掉线了。拔插一次线（或重连同一 WiFi），"
                "还不行就是这台机器上的 adb 和手机上的版本对不上，见下方「端口冲突」"
            )
        if self.state == "recovery" or self.state == "sideload":
            return "手机在恢复模式/侧载模式里，正常开机后再连"
        return f"设备状态是 {self.state or '未知'}，这个状态投不了屏"

    def to_dict(self) -> MirrorDeviceRow:
        return {
            "serial": self.serial,
            "state": self.state,
            "label": self.label,
            "model": self.model,
            "wireless": self.wireless,
            "ready": self.ready,
            "hint": self.how_to_fix,
            "channels": list(self.channels or (self.serial,)),
        }


_ROW = re.compile(r"^(\S+)\s+(\w+)(?:\s+(.*))?$")
_FIELD = re.compile(r"(\w+):(\S+)")

#: The words ``adb`` puts in column two. A row whose second column is not one of these
#: is prose from a wedged or differently-worded adb, and reading it as a phone would
#: invent a device -- worse than missing one, because somebody who plugged in a single
#: phone then gets "同时连着 2 台，告诉我要哪一台".
_STATES: Final[frozenset[str]] = frozenset(
    {"device", "offline", "unauthorized", "bootloaded", "recovery", "sideload", "no"}
)


def parse_devices(output: str) -> tuple[Device, ...]:
    """Read ``adb devices -l`` output into rows.

    Lenient about extra fields, strict about what counts as a row: an unrecognised line
    is skipped and logged rather than raising, because this runs against whatever adb
    the operator happens to have.
    """
    devices: list[Device] = []
    for line in output.splitlines():
        text = line.strip()
        if not text or text.startswith("List of devices") or text.startswith("*"):
            continue
        match = _ROW.match(text)
        if match is None:
            continue
        serial, state, rest = match.group(1), match.group(2), match.group(3) or ""
        if state not in _STATES:
            logger.debug("skipping unrecognised adb line: %s", text[:200])
            continue
        fields = dict(_FIELD.findall(rest))
        devices.append(
            Device(
                serial=serial,
                state=state,
                model=fields.get("model", ""),
                product=fields.get("product", ""),
                device=fields.get("device", ""),
                transport_id=fields.get("transport_id", ""),
            )
        )
    return tuple(devices)


def group_by_phone(
    rows: Sequence[Device], key_of: Callable[[Device], str], mode_id: str
) -> tuple[Device, ...]:
    """一台手机一行，而不是 ``adb`` 列了几行。

    ``adb devices`` 按**通道**列：一台手机插着线又开过两个 ``adb tcpip`` 端口就是三行，
    三行的 ``ro.serialno`` 全等于同一个串（那台 vivo 实测如此，见 :meth:`_physical_id`）。
    三行直接摆到界面上，用户读到的是「连着三台一模一样的手机」；WiFi 档按「开始投屏」时
    ``_pick_by_mode`` 数的也是行，于是回一句「这一档同时连着 2 台，告诉我要哪一台」——
    可他手上就一台手机。

    每一行代表哪台由 ``key_of`` 说（就是那个序列号），行里摆哪个通道交给 :func:`in_mode`
    —— 和「这一档要哪一类设备」同一条规则，这里不另设第二套判断。其余通道留在
    ``channels`` 上，一个都不丢。

    不答话的行（``unauthorized``/``offline``）永远自己一组：它的 ``ro.serialno`` 正是问不出
    来的那一个，而它自己的通道名又和别的通道问出来的序列号长得一样 —— 并进去就把「在手机上
    点允许 USB 调试」这句要命的话藏掉了，那恰恰是用户唯一还该做的事。
    """
    buckets: dict[str, list[Device]] = {}
    for row in rows:
        buckets.setdefault(f"!{row.serial}" if not row.ready else key_of(row), []).append(row)
    phones: list[Device] = []
    for members in buckets.values():
        if len(members) == 1:
            phones.append(members[0])
            continue
        head = next(
            (row for row in members if in_mode(mode_id, row)),
            next((row for row in members if not row.wireless), members[0]),
        )
        phones.append(dataclasses.replace(head, channels=tuple(row.serial for row in members)))
    return tuple(phones)


#: scrcpy/adb stderr -> what to tell the person. Matched in order; first hit wins.
#: The raw line is always appended to the message, so an unmapped failure still shows
#: up in full rather than being replaced by a confident guess.
_WIRELESS_HINT: Final = (
    "连不上手机上的 adb 端口（5555）。无线调试每次重启手机端口都会变，"
    "重新按「无线调试」里的配对码连一次"
)

_ERRORS: Final[tuple[tuple[str, str], ...]] = (
    (
        "Could not find any ADB device",
        "没有可投屏的手机。USB 线插上后在手机里点「允许 USB 调试」，"
        "或者先在下面「无线调试」里连一次",
    ),
    (
        "not found",
        "刚才那台手机已经不在了（拔了线、断了 WiFi，或者息屏后掉了网络）",
    ),
    (
        "unauthorized",
        "手机上还没点「允许 USB 调试」，点完再试一次",
    ),
    (
        "offline",
        "设备处于 offline。拔插一次，或在手机上关掉再打开「USB 调试」",
    ),
    (
        "adb server version",
        "这台机器上还有另一个版本的 adb 在跑（常见于装过 Android Studio）。"
        "它会和小程序自带的抢 5037 端口：先退出那个 adb 服务，或重启电脑后再试",
    ),
    (
        "ARDEADB",
        "推 scrcpy-server 到手机被拒了。手机里把「USB 调试」和「撤销安全设置」都确认一下，"
        "部分国产 ROM 还要单独开「USB 调试（安全设置）」才允许投屏",
    ),
    (
        "ADB push failed",
        "推 scrcpy-server 到手机失败，通常是手机存储或权限的问题；"
        "换一根能传数据的线（有些线只能充电）再试",
    ),
    # scrcpy says "could not connect to", adb says "Could not create socket" -- the
    # same wireless failure from the two sides of the call.
    ("could not connect to", _WIRELESS_HINT),
    ("Could not create socket", _WIRELESS_HINT),
    (
        "Codec not supported",
        "这台手机不支持刚才选的编码器，换一档再试",
    ),
    (
        "Cannot find custom video encoder",
        "这台手机没有那个硬件编码器，换「均衡」档",
    ),
    (
        "The connection was dropped",
        "连接被手机断掉了。息屏、切到后台、或者省电策略杀了 adb 都会这样",
    ),
    (
        "ERROR: ",
        "投屏启动失败",
    ),
)


def explain(output: str) -> str:
    """Turn scrcpy/adb output into one Chinese sentence, keeping the raw line."""
    for needle, message in _ERRORS:
        if needle in output:
            tail = _last_error_line(output)
            return message if not tail or tail in message else f"{message}（原始：{tail}）"
    tail = _last_error_line(output)
    return f"投屏没起来（原始：{tail}）" if tail else "投屏没起来，也没有留下任何输出"


def _last_error_line(output: str) -> str:
    for line in reversed([item for item in output.splitlines() if item.strip()]):
        if line.startswith("ERROR:") or line.startswith("adb:"):
            return line[:300]
    return ""


class Phase(StrEnum):
    """What the mirroring is doing, in the words the panel shows."""

    OFF = "off"
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"
    FAILED = "failed"


def runtime_dir() -> Path | None:
    """Where ``scrcpy.exe`` and ``adb.exe`` live, or ``None`` when they were not shipped.

    Same convention as :func:`jarvis.llm.local_server.runtime_dir`: a ``JARVIS_SCRCPY_DIR``
    override for development, then ``vendor/scrcpy`` inside the package (which is where
    the frozen build finds it, under ``_internal``).
    """
    override = os.environ.get("JARVIS_SCRCPY_DIR", "")
    root = Path(__file__).resolve().parent.parent
    candidates = [Path(part) for part in override.split(os.pathsep) if part]
    candidates += [root / "vendor" / VENDOR_DIRNAME, root / "_vendor" / VENDOR_DIRNAME]
    for candidate in candidates:
        if (candidate / SCRCPY_EXE).is_file() and (candidate / ADB_EXE).is_file():
            return candidate
    return None


_SW_RESTORE: Final = 9
_SW_MAXIMIZE: Final = 3


def find_scrcpy_window(title: str = _WINDOW_TITLE) -> int:
    """The open scrcpy window's handle, found by its title. ``0`` when there is none.

    Matching the title rather than the window class is deliberate: scrcpy's SDL class
    name is shared with anything else built on SDL, while ``_WINDOW_TITLE`` is ours and
    is what the panel tells the person to look for.
    """
    if os.name != "nt":  # pragma: no cover - the desktop is Windows
        return 0
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32")
    found = ctypes.c_long(0)

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)  # type: ignore[untyped-decorator]
    def each(handle: int, _param: int) -> bool:
        if not user32.IsWindowVisible(handle):
            return True
        length = user32.GetWindowTextLengthW(handle)
        if not length:
            return True
        buffer = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(handle, buffer, length + 1)
        if buffer.value == title:
            found.value = handle
            return False
        return True

    user32.EnumWindows(each, 0)
    return int(found.value)


def show_scrcpy_window(handle: int, how: str) -> bool:
    """Maximize or restore an existing window. No restart, no relaunch."""
    if os.name != "nt" or not handle:  # pragma: no cover - the desktop is Windows
        return False
    import ctypes

    user32 = ctypes.WinDLL("user32")
    code = _SW_MAXIMIZE if how == WINDOW_MAXIMIZED else _SW_RESTORE
    return bool(user32.ShowWindow(handle, code))


class ScreenMirrorService:
    """Owns the ``scrcpy`` child process and the device list the panel reads."""

    name = "screen_mirror"

    def __init__(
        self,
        prefs: Preferences,
        *,
        spawn: Callable[..., subprocess.Popen[str]] | None = None,
        find_window: Callable[[], int] = find_scrcpy_window,
        show_window: Callable[[int, str], bool] = show_scrcpy_window,
    ):
        """Create the service.

        Args:
            prefs: Where the quality tier and the audio choice are remembered.
            spawn: Test seam for ``subprocess.Popen``. Injected because the thing
                being tested is the state machine around the process, and a test
                that shells out to a real scrcpy cannot assert anything about a
                phone that is not plugged in.
        """
        self._prefs = prefs
        self._spawn = spawn or subprocess.Popen
        # The window is scrcpy's, not ours: these two are the only things we do to it,
        # and they are seams because a test cannot open an SDL window.
        self._find_window = find_window
        self._show_window = show_window
        # Reentrant because the read path is reached from inside the write path:
        # ``start_mirror`` reports "已经在了" by calling ``status()``, and ``status()``
        # itself re-checks the process. A plain Lock deadlocked the first call and the
        # panel simply stopped answering.
        self._lock = threading.RLock()
        self._proc: subprocess.Popen[str] | None = None
        self._phase = Phase.OFF
        self._detail = ""
        self._serial = ""
        self._started_at = 0.0
        self._tail: deque[str] = deque(maxlen=40)
        self._reader: threading.Thread | None = None
        self._started = False
        self._discovery = ""
        self._generation = 0
        # ``adb shell wm size`` 每按一次滑动都问一遍太浪费；按序列号记住。
        self._sizes: dict[str, tuple[int, int]] = {}
        # 同一件事对 ``ro.serialno``：一个通道问一次就够（见 :meth:`_physical_id`）。
        self._physical: dict[str, str] = {}
        # 「这一格没能照着做」的那句话。它必须活过一次轮询：面板每 1.5 秒读一次状态，
        # 只在返回值里给一次的提示会被下一次轮询擦掉，用户看到的是"点了没反应"。
        self._note = ""
        """Which start/stop owns the lifecycle state right now.

        The output pump writes the final phase when its process exits. Without a
        generation, a pump that lost the race could write 「投屏窗口已关闭」 over a
        mirror that has since been started again -- or clear the serial of the new one.
        """

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        self._started = True
        where = runtime_dir()
        if where is None:
            logger.warning("scrcpy 运行时不在包里（jarvis/vendor/scrcpy 缺失），投屏功能不可用")
        else:
            logger.info("screen mirroring runtime: %s", where)

    def stop(self) -> None:
        """Take the child down with the app. Never leaves a window behind."""
        try:
            self._terminate(reason="应用退出")
        except Exception:  # pragma: no cover - shutdown must not raise
            logger.exception("stopping the mirror on shutdown failed")
        self._started = False

    # -- reads -------------------------------------------------------------

    def available(self) -> bool:
        return runtime_dir() is not None

    def devices(self) -> tuple[tuple[Device, ...], str]:
        """The connected phones, plus a message when even the question failed.

        Returns the error text rather than raising: the panel shows a device list on
        open, and an empty list with no explanation is indistinguishable from "you
        have no phone" -- which is precisely the wrong thing to tell somebody whose
        adb is merely wedged.
        """
        where = runtime_dir()
        if where is None:
            return (), "这个包里没有带 scrcpy 运行时，投屏功能不可用"
        try:
            completed = subprocess.run(
                [str(where / ADB_EXE), "devices", "-l"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=12.0,
                shell=False,
                creationflags=_no_window_flags(),
            )
        except (OSError, subprocess.SubprocessError) as exc:
            logger.warning("adb devices failed: %s", exc)
            return (), f"问不到设备列表：{type(exc).__name__}: {exc}"
        if completed.returncode != 0:
            return (), explain(completed.stdout + completed.stderr)
        return parse_devices(completed.stdout), ""

    def phones(self) -> tuple[tuple[Device, ...], str]:
        """连着几台**手机**（一行一台），不是 ``adb`` 列了几行。见 :func:`group_by_phone`。

        代价是每行一次 ``adb shell getprop ro.serialno``，问过的通道记在 ``self._physical``
        里不再重问；只有一行时没有歧义，一次都不问。所以这一调用走的是「刷新设备」「开始
        投屏」和她念设备清单那三个按钮，**绝不放进面板那 1.5 秒一次的轮询** —— 轮询读的是
        :meth:`status`，那里一个 adb 都不碰。
        """
        rows, error = self.devices()
        if error or len(rows) < 2:
            return rows, error
        return group_by_phone(rows, self._physical_id, self.mode().id), ""

    def status(self) -> MirrorStatus:
        """One JSON-ready snapshot for the HUD. Poll-safe."""
        self._reap_if_gone()
        with self._lock:
            return {
                "phase": self._phase.value,
                "detail": self._detail,
                "serial": self._serial,
                "available": runtime_dir() is not None,
                "tier": self.tier().id,
                "tiers": [
                    {"id": item.id, "label": item.label, "note": item.note} for item in TIERS
                ],
                "window": self.window_mode().id,
                "windows": [
                    {"id": item.id, "label": item.label, "note": item.note} for item in WINDOW_MODES
                ],
                "stretch": self.stretch_enabled(),
                "keys": [
                    {"id": item.id, "label": item.label, "note": item.note} for item in PHONE_KEYS
                ],
                "swipes": [
                    {"id": item.id, "label": item.label, "note": item.note} for item in SWIPES
                ],
                "mode": self.mode().id,
                "modes": [
                    {
                        "id": item.id,
                        "label": item.label,
                        "pros": item.pros,
                        "cons": item.cons,
                    }
                    for item in MODES
                ],
                "audio": self.audio_enabled(),
                "keep_screen": self._prefs.flag(MIRROR_KEEP_PHONE_SCREEN, default=True),
                "discovery": self._discovery,
                "seconds": (
                    round(time.time() - self._started_at) if self._phase is Phase.RUNNING else 0
                ),
                "tail": list(self._tail)[-6:],
                "error": self._note,
            }

    def tier(self) -> QualityTier:
        return tier_by_id(self._prefs.text(MIRROR_TIER) or DEFAULT_TIER)

    def mode(self) -> ConnectionMode:
        return mode_by_id(self._prefs.text(MIRROR_MODE) or DEFAULT_MODE)

    def window_mode(self) -> WindowMode:
        return window_mode_by_id(self._prefs.text(MIRROR_WINDOW) or DEFAULT_WINDOW)

    def stretch_enabled(self) -> bool:
        return self._prefs.flag(MIRROR_STRETCH, default=False)

    def audio_enabled(self) -> bool:
        """Off by default: audio capture is a real stutter source on 2.4GHz WiFi."""
        return self._prefs.flag(MIRROR_AUDIO, default=False)

    def build_command(self, serial: str, tier: QualityTier, *, audio: bool) -> list[str]:
        """The scrcpy argv. Split out so a test can pin the flags without a phone."""
        where = runtime_dir()
        assert where is not None
        command = [
            str(where / SCRCPY_EXE),
            "--serial",
            serial,
            f"--window-title={_WINDOW_TITLE}",
            f"--max-fps={tier.max_fps}",
            f"--video-bit-rate={tier.bit_rate}",
            f"--video-codec={tier.video_codec}",
            # direct3d is the Windows hardware path; the default SDL choice has been
            # observed to fall back to a software renderer on machines with odd GPU
            # drivers, which is exactly the "一卡一卡" this feature has to avoid.
            "--render-driver=direct3d",
            # A dropped frame is better than a queue of them: without this, a slow
            # stretch of WiFi shows up as the picture catching up all at once.
            "--video-buffer=0",
        ]
        if tier.max_size:
            command.append(f"--max-size={tier.max_size}")
        if audio:
            command += ["--audio-codec=opus", "--audio-bit-rate=96K"]
        else:
            command.append("--no-audio")
        if self.window_mode().id == WINDOW_FULLSCREEN:
            # scrcpy's own fullscreen: SDL holds that state, so it is a launch flag
            # rather than something to be poked at from outside.
            command.append("--fullscreen")
        if self.stretch_enabled():
            # Fill the window even though the phone's shape does not match it.
            command += ["--no-window-aspect-ratio-lock", "--render-fit=stretched"]
        if not self._prefs.flag(MIRROR_KEEP_PHONE_SCREEN, default=True):
            # Turning the phone's own screen off saves battery and GPU on the phone,
            # which measurably helps the frame rate on weaker devices.
            command.append("--turn-screen-off")
        return command

    # -- writes ------------------------------------------------------------

    def set_tier(self, value: object) -> str:
        name = str(value or "").strip()
        if not any(tier.id == name for tier in TIERS):
            return f"没有「{name}」这一档"
        self._prefs.set(MIRROR_TIER, name)
        return ""

    def set_window_mode(self, value: object) -> str:
        name = str(value or "").strip()
        if not any(mode.id == name for mode in WINDOW_MODES):
            return f"没有「{name}」这种显示方式"
        self._prefs.set(MIRROR_WINDOW, name)
        return ""

    def set_stretch(self, value: object) -> str:
        self._prefs.set(MIRROR_STRETCH, bool(value))
        return ""

    def apply_window_mode(self, value: object = "") -> str:
        """Remember the display mode and do live whatever can be done live.

        Returns the sentence for the panel -- including the honest one about 全屏
        needing a restart. A control that silently does nothing to an open window is
        worse than one that says it cannot.
        """
        raw = str(value or "").strip()
        if raw and not any(mode.id == raw for mode in WINDOW_MODES):
            return f"没有「{raw}」这种显示方式"
        name = raw or self.window_mode().id
        self._prefs.set(MIRROR_WINDOW, name)
        if self._phase not in (Phase.STARTING, Phase.RUNNING):
            self._note = ""
            return ""
        if name == WINDOW_FULLSCREEN:
            self._note = "全屏要重开一次投屏（或者现在这个窗口里按 F11）"
            return self._note
        handle = self._find_window()
        if not handle:
            self._note = "没找到投屏窗口，可能已经被你关掉了"
            return self._note
        if not self._show_window(handle, name):
            self._note = "窗口没答应被改成那样（可能被系统或别的程序占着）"
            return self._note
        self._note = ""
        return ""

    def _nudge_maximized(self) -> None:
        """Maximize the window once scrcpy has actually created it.

        The window does not exist the instant the process starts, so this waits a
        couple of seconds for it rather than pretending the launch already worked.
        """
        deadline = time.monotonic() + 4.0
        while time.monotonic() < deadline:
            handle = self._find_window()
            if handle:
                self._show_window(handle, WINDOW_MAXIMIZED)
                return
            time.sleep(0.1)
        with self._lock:
            if self._phase in (Phase.STARTING, Phase.RUNNING):
                self._note = "铺满没做成：4 秒内没等到投屏窗口（画面还是窗口那个大小）"
        logger.debug("投屏窗口 4 秒内没出现，铺满没做成")

    # -- 手机上的按键与滑动 ------------------------------------------------

    def press_key(self, key_id: str = "") -> MirrorStatus:
        """把手机上那三个键之一发给**正在投屏的那台**。

        走 ``adb shell input keyevent`` 而不是 scrcpy：scrcpy 没有"从外部按一个键"的接口，
        而 adb 这条路和它用的是同一条已信任连接。返回的是状态快照，成败写在 ``error`` 里。
        """
        key = phone_key_by_id(str(key_id or "").strip())
        if key is None:
            return self._refuse(f"没有「{key_id}」这个按键")
        busy = self._mirror_target()
        if busy is not None:
            return busy
        serial = self._serial
        code, out = self._adb_shell(serial, ["input", "keyevent", key.keycode])
        return self._injection_report(code, out, f"{key.label}（{key.keycode}）")

    def swipe(self, direction_id: str = "") -> MirrorStatus:
        """在屏幕中间那一段划一下：下一个（往上）、上一个（往下）。

        坐标按这台自己的宽高算（``wm size``，记住不重复问），所以竖屏横屏、分辨率多少都不用写死。
        """
        row = swipe_by_id(str(direction_id or "").strip())
        if row is None:
            return self._refuse(f"没有「{direction_id}」这个滑动方向")
        busy = self._mirror_target()
        if busy is not None:
            return busy
        serial = self._serial
        size = self._screen_size(serial)
        if size is None:
            return self.status()
        width, height = size
        middle = width // 2
        code, out = self._adb_shell(
            serial,
            [
                "input",
                "touchscreen",
                "swipe",
                str(middle),
                str(int(height * row.from_ratio)),
                str(middle),
                str(int(height * row.to_ratio)),
                str(row.duration_ms),
            ],
        )
        return self._injection_report(code, out, row.label)

    def capture_frame(self) -> tuple[bytes, str]:
        """这张屏的 PNG。手机自己截（``screencap``），不去抓 scrcpy 那个窗口。

        两条理由：窗口可能被别的程序盖住、被最小化，或被裁掉一半；``screencap`` 拿的是
        设备真实的像素，分辨率也是原生的（实测 1260x2800，约 0.8 秒）。代价是被系统判为
        受保护的画面（银行 App、部分播放器）会给你一张全黑或一张空图 —— 那种情况
        ``note`` 里说清楚，不假装读到了内容。

        Returns:
            ``(png_bytes, error)``；``error`` 非空时前面的字节不可信。
        """
        target = self._any_adb_target()
        if not target:
            return b"", "没有连着能读的手机（投屏没开、或者 adb 里没设备）"
        code, data, err = self._adb_exec_out(target, ["screencap", "-p"], timeout=12.0)
        if code != 0 or not data:
            return b"", err or f"手机没给出截图（adb 退出码 {code}）"
        if data[:8] != b"\x89PNG\r\n\x1a\n":
            return b"", "拿回来的不是 PNG（这台机器的 screencap 被改了？）"
        return data, ""

    def read_screen(self) -> ScreenRead:
        """问手机自己：这一屏是什么 App、上面有哪些文字。

        零依赖、离线、不经过任何模型 —— 无障碍树里有的文字，这里就有。游戏、视频帧、
        自己画字的 App 读不到文字，那不是坏了，``note`` 会这么说。
        """
        target = self._any_adb_target()
        if not target:
            return ScreenRead(
                app="",
                activity="",
                widgets=[],
                note="",
                error="没有连着能读的手机：投屏没开，adb 里也没剩下唯一那台",
            )
        app, activity, focus_note = self._focused_app(target)
        xml, dump_note = self._ui_dump(target)
        widgets = parse_widget_texts(xml) if xml else []
        note = "、".join(x for x in (focus_note, dump_note) if x)
        if not widgets and not note:
            note = "这一屏没吐出文字：可能是视频、游戏或自己画的界面"
        return ScreenRead(app=app, activity=activity, widgets=widgets, note=note, error="")

    def _any_adb_target(self) -> str:
        """读屏幕要对哪台：正在投的那台优先，否则 adb 里那**一台**手机。

        连着两台不同的手机才返回空字符串 —— 那时候猜一台是错的，让她问一句"你要看哪台"。

        "两台"要看清是什么算两台：一根 USB 线 + 两个 ``adb tcpip`` 端口，``adb devices``
        会列成**三行**，其实是同一台手机（实测那台 vivo 三行的 ``ro.serialno`` 全等于
        ``10AE1F1RQE003N3``）。按行数判"几台"会把这台手机永远挡在"不猜"那条规则外面 ——
        表现代界面上一句「没有连着能读的手机」，而线就插着。所以数的是 :meth:`phones`。
        """
        self._reap_if_gone()
        with self._lock:
            if self._phase in (Phase.STARTING, Phase.RUNNING) and self._serial:
                return self._serial
        rows, error = self.phones()
        if error:
            return ""
        ready = [row for row in rows if row.ready]
        if len(ready) != 1:
            # 两台不同的手机：猜一台是错的，让她问一句"你要看哪台"。
            return ""
        phone = ready[0]
        # 读屏要的是最低延迟那一头，所以在这台手机的几个通道里先挑插线的：不占 WiFi、
        # 不会因为路由器开了隔离而掉。列表上那一行摆的是按档位挑的通道，两回事。
        return next((item for item in phone.channels if ":" not in item), phone.serial)

    def _physical_id(self, row: Device) -> str:
        """这台设备自己报的序列号 —— 一条 ``adb devices`` 的键可能对应多个通道。

        读不到就退回那个通道本身（宁可当成两台多问一句，也不要把两台不同的手机
        合成一台 —— 那样按错手机是改那台机器上的东西）。

        只认「一个不带空格的串」：``adb shell`` 在没打通的通道上吐的是提示语，而
        ``unknown`` 是 ROM 答不出时的值、两台手机一模一样 —— 照那种答案分组会把两台
        并成一台，界面就变成"少了两台手机"，比多列两行糟得多。
        """
        cached = self._physical.get(row.serial)
        if cached is not None:
            return cached
        code, out = self._adb_shell(row.serial, ["getprop", "ro.serialno"])
        answer = out.strip().splitlines()[0].strip() if code == 0 and out.strip() else ""
        usable = bool(answer) and " " not in answer and answer.lower() != "unknown"
        resolved = answer if usable else row.serial
        with self._lock:
            self._physical[row.serial] = resolved
        return resolved

    def _focused_app(self, serial: str) -> tuple[str, str, str]:
        code, out = self._adb_shell(serial, ["dumpsys", "window"], timeout=10.0)
        if code != 0:
            return "", "", f"读当前 App 失败（{out[:120] or 'adb 没答'}）"
        return parse_focused_app(out)

    def _ui_dump(self, serial: str) -> tuple[str, str]:
        code, data, err = self._adb_exec_out(
            serial, ["uiautomator", "dump", "/dev/tty"], timeout=20.0
        )
        xml = data.decode("utf-8", errors="replace")
        if code != 0:
            return "", f"读屏幕文字失败（{err or 'adb 没答'}）"
        if "<hierarchy" not in xml:
            return "", "这台手机不让读屏幕文字（ROM 挡了 uiautomator）"
        return xml, ""

    def _mirror_target(self) -> MirrorStatus | None:
        """None 表示"有一台正在投的手机"；否则直接回一个说清原因的快照。

        不猜发给谁：连着两台的时候，按错手机是改那台机器上的东西，不是我们这边的一个坏状态。
        """
        self._reap_if_gone()
        with self._lock:
            if self._phase in (Phase.STARTING, Phase.RUNNING) and self._serial:
                return None
        return self._refuse("现在没在投屏，不知道这一下要发给哪台手机；先点「开始投屏」")

    def _refuse(self, message: str) -> MirrorStatus:
        with self._lock:
            self._note = message[:400]
        logger.info("投屏这一下被拒：%s", message[:200])
        return self.status()

    def _injection_report(self, code: int, out: str, what: str) -> MirrorStatus:
        """成败要落在屏上，不能只写日志。

        ``adb`` 有些 ROM 退出码还是 0 而把 SecurityException 打在文字里（MIUI 那条
        「USB 调试（安全设置）」），所以退出码和文字两个都要判。
        """
        lowered = out.lower()
        if code == 0 and "exception" not in lowered and "denied" not in lowered:
            with self._lock:
                self._note = ""
            logger.debug("投屏按键发出去了：%s", what)
            return self.status()
        reason = out[:220] or f"adb 退出码 {code}"
        with self._lock:
            self._note = (
                f"这一下手机没接（{what}）：{reason}。"
                "有些国产 ROM 还要把「USB 调试（安全设置）」/「模拟点击」打开才让注入按键。"
            )
        logger.info("投屏注入失败：%s / %s", what, out[:200])
        return self.status()

    def _adb_exec_out(
        self, serial: str, words: Sequence[str], *, timeout: float = 12.0
    ) -> tuple[int, bytes, str]:
        """``adb -s <serial> exec-out ...`` with the raw bytes kept raw.

        ``exec-out`` (not ``shell``) because ``shell`` translates LF to CRLF on the
        way out and that corrupts a PNG. ``text=False`` for the same reason.
        """
        where = runtime_dir()
        if where is None:
            return 127, b"", "这个包里没有带 scrcpy 运行时，投屏功能不可用"
        argv = [str(where / ADB_EXE), "-s", serial, "exec-out", *words]
        try:
            completed = subprocess.run(
                argv,
                capture_output=True,
                timeout=timeout,
                text=False,
                shell=False,
                creationflags=_no_window_flags(),
            )
        except (OSError, subprocess.SubprocessError) as exc:
            logger.warning("adb exec-out %s 失败：%s", " ".join(words), exc)
            return 124, b"", f"{type(exc).__name__}: {exc}"
        return (
            completed.returncode,
            completed.stdout or b"",
            (completed.stderr or b"").decode("utf-8", errors="replace").strip(),
        )

    def _adb_shell(
        self, serial: str, words: Sequence[str], *, timeout: float = 8.0
    ) -> tuple[int, str]:
        where = runtime_dir()
        if where is None:
            return 127, "这个包里没有带 scrcpy 运行时，投屏功能不可用"
        argv = [str(where / ADB_EXE), "-s", serial, "shell", *words]
        try:
            completed = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                shell=False,
                creationflags=_no_window_flags(),
            )
        except (OSError, subprocess.SubprocessError) as exc:
            logger.warning("adb shell %s 失败：%s", " ".join(words), exc)
            return 124, f"{type(exc).__name__}: {exc}"
        return completed.returncode, ((completed.stdout or "") + (completed.stderr or "")).strip()

    def _screen_size(self, serial: str) -> tuple[int, int] | None:
        cached = self._sizes.get(serial)
        if cached is not None:
            return cached
        code, out = self._adb_shell(serial, ["wm", "size"])
        size = parse_screen_size(out) if code == 0 else None
        if size is None:
            reason = out[:160] or f"adb 退出码 {code}"
            with self._lock:
                self._note = f"读不到这台手机的屏幕尺寸（{reason}），滑动没法算坐标"
            logger.info("wm size 解析失败：%s", out[:160])
            return None
        with self._lock:
            self._sizes[serial] = size
        return size

    def set_mode(self, value: object) -> str:
        name = str(value or "").strip()
        if not any(mode.id == name for mode in MODES):
            return f"没有「{name}」这种连接方式"
        self._prefs.set(MIRROR_MODE, name)
        return ""

    def set_audio(self, value: object) -> str:
        self._prefs.set(MIRROR_AUDIO, bool(value))
        return ""

    def set_keep_screen(self, value: object) -> str:
        self._prefs.set(MIRROR_KEEP_PHONE_SCREEN, bool(value))
        return ""

    def start_mirror(self, serial: str = "", *, audio: bool | None = None) -> MirrorStatus:
        """Bring the mirror up on one phone. Never raises; the panel reads ``detail``."""
        with self._lock:
            if self._proc is not None and self._proc.poll() is None:
                return self._locked_status(f"已经在了（{self._serial}）。要先关掉再换设备")
        if runtime_dir() is None:
            self._fail("这个包里没有带 scrcpy 运行时，投屏功能不可用")
            return self.status()
        chosen = serial.strip()
        if not chosen:
            rows, error = self.devices()
            if error:
                self._fail(error)
                return self.status()
            picked, refusal = self._pick_by_mode(rows)
            if refusal:
                self._fail(refusal)
                return self.status()
            chosen = picked
        tier = self.tier()
        want_audio = self.audio_enabled() if audio is None else bool(audio)
        command = self.build_command(chosen, tier, audio=want_audio)
        logger.info("starting screen mirror: %s", " ".join(command[1:]))
        try:
            proc = self._spawn(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                creationflags=_no_window_flags(),
            )
        except OSError as exc:
            self._fail(f"启动 scrcpy 失败：{type(exc).__name__}: {exc}")
            return self.status()
        with self._lock:
            self._proc = proc
            self._serial = chosen
            self._phase = Phase.STARTING
            self._detail = ""
            self._note = ""
            self._started_at = time.time()
            self._note = ""
            self._tail.clear()
            self._generation += 1
            generation = self._generation
        self._reader = threading.Thread(
            target=self._pump, args=(proc, generation), name="scrcpy-output", daemon=True
        )
        self._reader.start()
        if self.window_mode().id == WINDOW_MAXIMIZED:
            threading.Thread(
                target=self._nudge_maximized, name="scrcpy-maximize", daemon=True
            ).start()
        return self.status()

    def stop_mirror(self) -> MirrorStatus:
        return self._terminate(reason="手动停止")

    def _pick_by_mode(self, rows: Sequence[Device]) -> tuple[str, str]:
        """Which phone the current 方式 means, or why it cannot say.

        ``rows`` come from :meth:`phones`, so one row is one physical phone: without that
        the count below is a count of ``adb`` lines, and one phone reached over a cable
        and two tcpip ports refuses to start itself with 「同时连着 2 台」.

        A cable phone and a WiFi phone are the same thing to scrcpy once adb holds them,
        so this is only a filter over ``adb devices`` -- but it is the filter that decides
        whether 「投屏」 picks the phone you meant or refuses with a sentence you can act
        on. Returning ``(serial, refusal)`` instead of raising is what lets the caller
        show the reason in the same place it shows every other failure.
        """
        wanted = self.mode().id
        ready = [row for row in rows if row.ready]
        picked = [row for row in ready if in_mode(wanted, row)]
        if len(picked) > 1:
            # 真要拒之前先问一次「这几行是不是同一台手机」。只在这一种情形花那次 adb：
            # 一行设备的常见路径一次都不问，而「同时连着 2 台」这句谎话本来就是这样骗人
            # 的 —— 一台插着线又开过两个 tcpip 端口的手机，``adb devices`` 给两行。
            picked = list(group_by_phone(picked, self._physical_id, wanted))
        if len(picked) == 1:
            return picked[0].serial, ""
        if len(picked) > 1:
            listing = "、".join(f"{row.label}（{row.serial}）" for row in picked)
            return "", f"这一档同时连着 {len(picked)} 台：{listing}。告诉我要哪一台"
        if ready:
            listing = "、".join(f"{row.label}（{row.serial}）" for row in ready)
            targets = "、".join(
                mode.label for mode in MODES if any(in_mode(mode.id, row) for row in ready)
            )
            return "", (
                f"当前方式是「{self.mode().label}」，没有这一类的设备；"
                f"连着的是：{listing}。把上面切到「{targets or '另一种方式'}」，或者直接点那一行"
            )
        hint = rows[0].how_to_fix if rows else self.mode().hint
        return "", f"没有可投屏的手机。{hint}"

    def connect_wireless(self, address: str) -> MirrorStatus:
        """``adb connect`` for wireless debugging (Android 11+ pairs separately)."""
        target = address.strip()
        if not target:
            self._fail("要填手机「无线调试」页面上显示的那个 IP:端口")
            return self.status()
        where = runtime_dir()
        if where is None:
            self._fail("这个包里没有带 adb，连不了")
            return self.status()
        out = self._run([str(where / ADB_EXE), "connect", target])
        lowered = out.lower()
        if "connected" in lowered:
            with self._lock:
                self._phase = Phase.OFF
                self._detail = f"已连上 {target}，现在可以开始投屏"
                if self.mode().id != WIFI_MODE:
                    self._detail += (
                        f"。它是无线设备，把上面切到「{mode_by_id(WIFI_MODE).label}」这一档"
                    )
            return self.status()
        if "already connected" in lowered:
            with self._lock:
                self._detail = f"{target} 本来就连着"
            return self.status()
        self._fail(
            f"连不上 {target}：{out.strip()[:200] or 'adb 没有回话'}。"
            "注意 Android 11+ 要先在「无线调试」里用配对码配过一次，"
            "而且手机和电脑必须在同一个 WiFi、不能开 AP 隔离"
        )
        return self.status()

    def open_wireless_port(self, serial: str = "") -> MirrorStatus:
        """``adb tcpip 5555`` + ``adb connect``, for phones whose 无线调试 has no pairing.

        Android 10 and older have no wireless-debugging dialog at all: the only way onto
        WiFi is to be plugged in once and told to listen on a TCP port. This is that one
        click. The address is read off ``wlan0`` and connected in the same call, because
        making somebody go find their IP in 设置 is how this feature ends up unused.
        """
        where = runtime_dir()
        if where is None:
            self._fail("这个包里没有带 adb，开不了无线端口")
            return self.status()
        chosen = serial.strip()
        if not chosen:
            rows, error = self.devices()
            if error:
                self._fail(error)
                return self.status()
            wired = [row for row in rows if row.ready and not row.wireless]
            if len(wired) != 1:
                self._fail(
                    "这一招要手机**正插着 USB 线**并且已经允许 USB 调试"
                    f"（现在找到 {len(wired)} 台插线的，没法确定给哪台开）"
                )
                return self.status()
            chosen = wired[0].serial
        out = self._run([str(where / ADB_EXE), "-s", chosen, "tcpip", str(_TCPIP_PORT)])
        if "error" in out.lower():
            self._fail(
                f"开无线端口没成功：{out.strip()[:200] or 'adb 没有回话'}。"
                "手机要正插着线、已允许 USB 调试，这一招才使得出"
            )
            return self.status()
        address = self._wlan0_address(where, chosen)
        if address is None:
            with self._lock:
                self._phase = Phase.OFF
                self._detail = (
                    f"{chosen} 已经改听 {_TCPIP_PORT} 端口了，但我读不到它的 WiFi 地址。"
                    f"在手机上（设置 → WLAN → 已连的网络）看到 IP 后，"
                    f"填成 IP:{_TCPIP_PORT} 再点「连接」"
                )
            return self.status()
        return self.connect_wireless(f"{address}:{_TCPIP_PORT}")

    def _wlan0_address(self, where: Path, serial: str) -> str | None:
        """The phone's own IPv4 on wlan0, or ``None`` when ``ip`` did not answer.

        ``ip`` is not on every ROM's PATH and some print the address under a different
        interface name, so a miss is expected often enough to be worth a second sentence
        in the panel rather than a stack trace.
        """
        out = self._run(
            [
                str(where / ADB_EXE),
                "-s",
                serial,
                "shell",
                "ip",
                "-f",
                "inet",
                "addr",
                "show",
                "wlan0",
            ]
        )
        for match in re.finditer(r"inet (\d{1,3}(?:\.\d{1,3}){3})", out):
            found = match.group(1)
            if not found.startswith("127."):
                return found
        logger.info("no usable wlan0 address from %s: %s", serial, out.strip()[:160])
        return None

    def discover_wireless(
        self,
        *,
        port: int = _TCPIP_PORT,
        hosts: Sequence[str] | None = None,
        timeout: float = 0.25,
    ) -> MirrorStatus:
        """Find phones answering on the fixed tcpip port and connect the ones that do.

        This is the "don't make me read an address off the phone" path. The scan is one
        port on one /24, started by a person pressing a button -- it is not a background
        thing, and it never touches anything outside the local network.
        """
        where = runtime_dir()
        if where is None:
            self._fail("这个包里没有带 adb，找不了")
            return self.status()
        targets = list(hosts) if hosts is not None else self._scan_hosts()
        if not targets:
            self._report_discovery(
                "问不到可扫的网段（没联网、网卡被停用，或者网段太大被跳过）。"
                "用「同一个 WiFi」那一档手填 IP:端口"
            )
            return self.status()
        open_hosts = [
            host for host in self._sweep(targets, port, timeout) if host not in set(local_ipv4s())
        ]
        connected, refused, skipped = self._connect_targets(where, open_hosts, port)
        if connected:
            message = (
                f"扫了本网段 {len(targets)} 个地址的 {port} 端口，连上 {len(connected)} 台："
                f"{'、'.join(connected)}。要投它就点那一行的「投这一台」"
            )
        elif refused:
            message = (
                f"扫到 {len(refused)} 台开着 {port} 端口的设备，但 adb 一台都没连上："
                f"{refused[0][1] or 'adb 没有回话'}"
            )
        else:
            message = (
                f"扫了本网段 {len(targets)} 个地址的 {port} 端口，没有设备应答。"
                "这一档要先插一次线点那一行的「开无线端口」（第一次那根线省不掉）；"
                "公司/公共 WiFi 开了 AP 隔离的话设备本来就互相看不见，改用「无线调试」那一档"
            )
        if skipped:
            message += (
                f"；还有 {skipped} 台没来得及试（整次扫描限 {_DISCOVERY_BUDGET:.0f} 秒），"
                "再点一次继续"
            )
        self._report_discovery(message)
        logger.info(
            "screen-mirror discovery on :%s: %d open, %d connected, %d skipped",
            port,
            len(open_hosts),
            len(connected),
            skipped,
        )
        return self.status()

    def _connect_targets(
        self, where: Path, open_hosts: Sequence[str], port: int
    ) -> tuple[list[str], list[tuple[str, str]], int]:
        """``adb connect`` the answering hosts, inside one overall budget.

        Each adb call used to be allowed 12 seconds and the loop was unbounded, so a
        handful of hosts that accept the port and then hang would keep the button spinning
        for a minute. The budget is checked *between* targets: one stuck call can still
        cost its own timeout, which is why that timeout came down too.
        """
        where_adb = str(where / ADB_EXE)
        deadline = time.monotonic() + _DISCOVERY_BUDGET
        connected: list[str] = []
        refused: list[tuple[str, str]] = []
        for index, host in enumerate(open_hosts):
            if index >= _MAX_CONNECT_TARGETS or time.monotonic() > deadline:
                return connected, refused, len(open_hosts) - index
            answer = self._run(
                [where_adb, "connect", f"{host}:{port}"], timeout=_DISCOVERY_CONNECT_TIMEOUT
            )
            if "connected" in answer.lower():
                connected.append(f"{host}:{port}")
            else:
                refused.append((host, answer.strip()[:120]))
        return connected, refused, 0

    def _report_discovery(self, message: str) -> None:
        """Record what the last sweep found, without touching the mirror's own lifecycle.

        Discovery and mirroring are two states: while scrcpy is up, the running phase and
        serial have to stay exactly as they are, or the panel's 停止 goes dead over a
        window that is still open.
        """
        with self._lock:
            self._discovery = message
            if self._phase not in (Phase.STARTING, Phase.RUNNING):
                self._detail = message

    @staticmethod
    def _sweep(hosts: Sequence[str], port: int, timeout: float) -> list[str]:
        """The hosts that answer on ``port``, probed a few at a time.

        A /24 probed one at a time at 0.25 s each would take a minute; thirty-two at a
        time takes about two seconds, which is the difference between a button and a
        freeze.
        """
        found: list[str] = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=32) as pool:
            futures = {pool.submit(scan_port, host, port, timeout): host for host in hosts}
            for done in concurrent.futures.as_completed(futures):
                if done.result():
                    found.append(futures[done])
        return sorted(found)

    @staticmethod
    def _scan_hosts() -> list[str]:
        """Hosts of every *active* network this machine is on, by real netmask.

        ``IPv4Network.hosts()`` already excludes the network and broadcast addresses.
        Down adapters are skipped -- a disabled VPN card would otherwise add a /24 that
        nothing can reach -- and a network bigger than ``_MAX_SCAN_NETWORK`` is skipped
        rather than silently turning a button into a minutes-long sweep.
        """
        hosts: list[str] = []
        seen: set[str] = set()
        for row in interfaces():
            if not row.active:
                logger.debug("跳过没启用的网卡 %s", row.name)
                continue
            try:
                network = ipaddress.IPv4Network(f"{row.address}/{row.netmask}", strict=False)
            except ValueError:
                logger.info("网卡 %s 的地址读不懂：%s/%s", row.name, row.address, row.netmask)
                continue
            if network.num_addresses - 2 > _MAX_SCAN_NETWORK:
                logger.info(
                    "网卡 %s 在 %s，%d 个地址太大，不自动扫（手填 IP:端口）",
                    row.name,
                    network,
                    network.num_addresses - 2,
                )
                continue
            if network.network_address in seen:
                continue
            seen.add(str(network.network_address))
            hosts.extend(str(host) for host in network.hosts())
        return hosts

    def pair_wireless(self, address: str, code: str) -> MirrorStatus:
        """``adb pair`` -- the six-digit code from the phone's 无线调试 dialog."""
        target = address.strip()
        digits = re.sub(r"\D", "", code or "")
        if not target or ":" not in target:
            self._fail("配对要填「使用配对码配对设备」那个页面上的 IP:端口，和下面那行一样")
            return self.status()
        if len(digits) < 6:
            self._fail(f"配对码是 6 位数字，现在只读到 {len(digits)} 位")
            return self.status()
        where = runtime_dir()
        if where is None:
            self._fail("这个包里没有带 adb，配不了对")
            return self.status()
        out = self._run([str(where / ADB_EXE), "pair", target, digits])
        if "successfully paired" in out.lower():
            with self._lock:
                self._detail = "配对成功。回到「无线调试」页面看 IP:端口，再点「连接」"
            return self.status()
        self._fail(
            f"配对没成功：{out.strip()[:200] or 'adb 没有回话'}。"
            "配对码和端口每次重开那个对话框都会变，要用当前屏幕上那一组"
        )
        return self.status()

    # -- internals ---------------------------------------------------------

    def _run(self, argv: list[str], timeout: float = 20.0) -> str:
        try:
            completed = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                shell=False,
                creationflags=_no_window_flags(),
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return f"{type(exc).__name__}: {exc}"
        return (completed.stdout or "") + (completed.stderr or "")

    def _pump(self, proc: subprocess.Popen[str], generation: int) -> None:
        """Copy scrcpy's output into the tail, and notice when it becomes ready."""
        stream = proc.stdout
        if stream is None:  # pragma: no cover - we always ask for a pipe
            return
        for line in stream:
            text = line.rstrip()
            if not text:
                continue
            with self._lock:
                self._tail.append(text[:300])
                if self._phase is Phase.STARTING and _looks_ready(text):
                    self._phase = Phase.RUNNING
                elif self._phase in (Phase.STARTING, Phase.RUNNING):
                    mapped = _map_line(text)
                    if mapped:
                        self._detail = mapped
        code = proc.wait()
        with self._lock:
            if generation != self._generation:
                return
            if self._phase in (Phase.STARTING, Phase.RUNNING):
                if code == 0:
                    self._phase = Phase.OFF
                    self._detail = "投屏窗口已关闭"
                else:
                    self._phase = Phase.FAILED
                    self._detail = explain("\n".join(self._tail))
            self._proc = None

    def _reap_if_gone(self) -> None:
        with self._lock:
            proc, phase = self._proc, self._phase
        if proc is None or phase not in (Phase.STARTING, Phase.RUNNING):
            return
        if proc.poll() is None:
            return
        # The pump thread sets the final state; give it a moment rather than racing it
        # to a wrong "已停止" that then flips back.
        time.sleep(0.05)
        self._reader = None

    def _fail(self, message: str) -> None:
        with self._lock:
            self._phase = Phase.FAILED
            self._detail = message[:400]
        logger.info("screen mirror refused: %s", message[:200])

    def _terminate(self, *, reason: str) -> MirrorStatus:
        with self._lock:
            proc = self._proc
            self._note = ""
            if proc is not None:
                self._phase = Phase.STOPPING
        if proc is None:
            with self._lock:
                self._phase = Phase.OFF
                self._detail = ""
                self._serial = ""
            return self.status()
        try:
            if proc.poll() is None:
                proc.terminate()
                proc.wait(timeout=5.0)
        except subprocess.TimeoutExpired:
            logger.warning("scrcpy ignored terminate; killing")
            proc.kill()
        except OSError as exc:  # pragma: no cover - the process already went away
            logger.debug("waiting for scrcpy failed: %s", exc)
        with self._lock:
            self._proc = None
            self._phase = Phase.OFF
            self._serial = ""
            self._detail = reason
            self._generation += 1
        logger.info("screen mirror stopped (%s)", reason)
        return self.status()

    def _locked_status(self, detail: str) -> MirrorStatus:
        with self._lock:
            self._detail = detail
        return self.status()


_READY_MARKERS: Final[tuple[str, ...]] = (
    "device ready",
    "starting (scid=",
    "current fps:",
)


def _looks_ready(line: str) -> bool:
    lowered = line.lower()
    return any(marker in lowered for marker in _READY_MARKERS)


def _map_line(line: str) -> str:
    """A line worth surfacing live, or ``""`` for chatter.

    scrcpy prints its per-second fps counter on stdout; that is the one number that
    tells the operator whether the link is actually keeping up, so it is passed
    through instead of being treated as log noise.
    """
    if line.startswith("ERROR:"):
        return explain(line)
    lowered = line.lower()
    if "current fps:" in lowered:
        return line.strip()[:120]
    return ""


def _no_window_flags() -> int:
    """No console window flashes for a GUI app that spawns children."""
    return getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0


__all__ = [
    "ADB_EXE",
    "DEFAULT_MODE",
    "DEFAULT_TIER",
    "MODES",
    "SCAN_MODE",
    "TIERS",
    "USB_MODE",
    "WIFI_MODE",
    "WINDOW_FULLSCREEN",
    "WINDOW_MAXIMIZED",
    "WINDOW_MODES",
    "WINDOW_NORMAL",
    "ConnectionMode",
    "Device",
    "Interface",
    "MirrorDeviceList",
    "MirrorDeviceRow",
    "MirrorModeRow",
    "MirrorStatus",
    "MirrorTierRow",
    "Phase",
    "QualityTier",
    "ScreenMirrorService",
    "WindowMode",
    "explain",
    "find_scrcpy_window",
    "in_mode",
    "interfaces",
    "local_ipv4s",
    "mode_by_id",
    "parse_devices",
    "runtime_dir",
    "scan_port",
    "show_scrcpy_window",
    "tier_by_id",
    "window_mode_by_id",
]
