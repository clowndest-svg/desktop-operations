"""投屏那一屏旁边的控制条：跟着画面走，不用回主窗口找按钮。

为什么不是把按钮做进 scrcpy 那个窗口里：那是 scrcpy 自己的 SDL 窗口，我们只能按
标题找到它、最大化它，要往里面画东西得改别人的进程。所以做一张**自己的**分层小窗
贴在它下面 —— 复用气泡那套 :class:`~jarvis.ui.compositor.AlphaWindow`（真置顶必须
``SetWindowPos(HWND_TOPMOST)``、创建时必须可见且不激活，这两条坑都写在那边）。

面板里那排按钮留着：那儿是"看说明、看这屏读到什么"的地方，条子是"手别离开画面"的地方。
"""

from __future__ import annotations

import base64
import ctypes
import io
import logging
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final, Protocol

from jarvis.ui.compositor import (
    DEFAULT_BUBBLE,
    AlphaWindow,
    BubbleButton,
    BubblePalette,
    font_at,
)

logger = logging.getLogger(__name__)

OFF_SCREEN: Final = -32000
"""和宠物窗同一套把戏：先建在屏幕外，第一帧画好了再挪进位，中间不许闪空框。"""

CELL: Final = 54
GAP: Final = 4
PAD: Final = 8
NOTE_HEIGHT: Final = 20
BAR_GAP: Final = 10
POLL_SECONDS: Final = 0.2
RUNNING_PHASES: Final = ("running", "starting")
LAST_SECONDS: Final = 12.0
"""「已经发给这台」在条上停留多久。引擎那边成功会把 note 清空，所以这句得条子自己记。"""

DEFAULT_NOTE: Final = "跟着这块屏 · 按住没字的地方能拖走"


def _skin_neutral() -> BubblePalette:
    """没有外壳可问色时的落点：气泡那套默认墨色。"""
    return DEFAULT_BUBBLE


HINT_FONT = 11
"""条上的中文标签字号。没字体时标签整排不画（形状还在），和气泡同一个处理。"""


@dataclass(frozen=True, slots=True)
class BarAction:
    """条子上的一颗按钮。``name`` 是带前缀的整名，``glyph`` 决定画哪个形状。"""

    name: str
    label: str
    glyph: str


class MirrorBarSurface(Protocol):
    """条子要的那三样。真服务满足它，测试用 ``SimpleNamespace`` 顶它。

    写成协议而不是 import 服务类：``ui`` 拿 ``app`` 的东西按这个方法集拿，
    服务改内部时不至于把这条小窗一起带倒。返回值是 ``Mapping`` 不是 ``dict`` ——
    服务发的是 TypedDict（``MirrorStatus``），它兼容 ``Mapping`` 而不兼容 ``dict``。
    """

    def status(self) -> Mapping[str, Any]: ...

    def press_key(self, key_id: str = "") -> Mapping[str, Any]: ...

    def swipe(self, direction_id: str = "") -> Mapping[str, Any]: ...


class BarWindow(Protocol):
    """这条窗用到的那几样 —— ``AlphaWindow`` 的形状，测试里换成一个记账的假窗。

    参数名必须和 ``AlphaWindow`` 对得上：``start`` 是关键字调用，名字错了 mypy 抓不到、
    运行时才炸。
    """

    @property
    def alive(self) -> bool: ...

    @property
    def rect(self) -> tuple[int, int, int, int]: ...

    def start(self, *, x: int, y: int, width: int, height: int) -> bool: ...

    def present(self, data_url: str) -> bool: ...

    @property
    def painted(self) -> bool:
        """屏上有没有这一块画面。``present`` 说"收下了"，这个说"落下了"。"""
        ...

    def set_hot_spots(self, buttons: Sequence[BubbleButton]) -> None: ...

    def set_visible(self, visible: bool) -> None: ...

    def set_click_through(self, on: bool) -> bool: ...

    def move_to(self, x: int, y: int) -> None: ...

    def keep_topmost(self) -> None: ...

    def close(self) -> None: ...


def short_label(label: str) -> str:
    """「下一个（往上滑）」→「下一个」。括号那半句留给面板，条子上一格只有 54 px。"""
    head = label.split("（", 1)[0].strip()
    return head or label


def bar_actions(
    keys: Sequence[Mapping[str, Any]], swipes: Sequence[Mapping[str, Any]]
) -> tuple[BarAction, ...]:
    """条子上画什么，全从服务发回来的那两份来 —— 不在这里抄第二份文案。"""
    out = [
        BarAction(
            name=f"key:{row.get('id')}",
            label=short_label(str(row.get("label", ""))),
            glyph=str(row.get("id")),
        )
        for row in keys
        if row.get("id")
    ]
    out += [
        BarAction(
            name=f"swipe:{row.get('id')}",
            label=short_label(str(row.get("label", ""))),
            glyph=str(row.get("id")),
        )
        for row in swipes
        if row.get("id")
    ]
    return tuple(out)


def bar_size(count: int) -> tuple[int, int]:
    cells = max(1, count)
    return PAD * 2 + cells * CELL + (cells - 1) * GAP, PAD + CELL + 2 + NOTE_HEIGHT


def hot_spots(actions: Sequence[BarAction]) -> tuple[BubbleButton, ...]:
    """每颗按钮落在哪儿 —— 和 :func:`build_bar` 用的是同一个式子，两处不许各算一遍。"""
    return tuple(
        BubbleButton(name=row.name, x=PAD + index * (CELL + GAP), y=PAD, size=CELL)
        for index, row in enumerate(actions)
    )


def dock_origin(
    mirror: tuple[int, int, int, int],
    width: int,
    height: int,
    work: tuple[int, int, int, int],
    gap: int = BAR_GAP,
) -> tuple[int, int]:
    """贴着投屏画面放：下面 → 上面 → 右边 → 左边，第一个放得下的算。

    顺序是量出来的，不是凑的。第一版只有"下面/上面"，在那台 1080 高的屏上画面占了
    974 px，两头都放不下，条子被夹到 ``y=0`` —— 正好**盖住手机画面的上半截**，
    而它存在的理由就是别挡画面。手机窗口本来就窄（436 px），左右有的是地方。

    ``work`` 是画面**所在那块显示器**的工作区：多屏时按主屏夹会把条子推到另一块屏外。
    四个方向都塞不下（画面几乎占满整块屏）才退回"贴下面 + 夹进屏" ——
    宁可压住一点边，也不要把按钮推到够不着的地方。
    """
    left, top, right, bottom = mirror
    work_left, work_top, work_right, work_bottom = work
    centered_x = left + (right - left - width) // 2
    middle_y = top + (bottom - top - height) // 2
    candidates = (
        (centered_x, bottom + gap),
        (centered_x, top - gap - height),
        (right + gap, middle_y),
        (left - gap - width, middle_y),
    )
    for x, y in candidates:
        if (
            x >= work_left
            and y >= work_top
            and x + width <= work_right
            and y + height <= work_bottom
        ):
            return int(x), int(y)
    x, y = candidates[0]
    x = max(work_left, min(x, max(work_left, work_right - width)))
    y = max(work_top, min(y, max(work_top, work_bottom - height)))
    return int(x), int(y)


def bar_note(status: Mapping[str, Any], last: tuple[str, float], now: float) -> str:
    """条上那一行字。优先级：**被拒的原因** > 刚才那一下 > 用法提示。

    成功时服务会把 note 清空，所以"已经发给这台"得条子自己记着，并且要有期限 ——
    一句十分钟前的话挂在现在这块屏下面，比没有话更容易骗人。
    """
    note = str(status.get("error") or "").strip()
    if note:
        return note
    label, stamp = last
    if label and now - stamp < LAST_SECONDS:
        return f"已经发给这台：{label}"
    return DEFAULT_NOTE


def _rgb(value: int) -> tuple[int, int, int]:
    return value >> 16 & 255, value >> 8 & 255, value & 255


def _chip(palette: BubblePalette) -> tuple[int, int, int, int]:
    base = _rgb(palette.fill[0] * 65536 + palette.fill[1] * 256 + palette.fill[2])
    return base[0] + 26, base[1] + 30, base[2] + 40, 255


def _draw_glyph(paint: Any, glyph: str, box: tuple[int, int, int, int], ink: Any) -> None:
    """一格里的形状。形状不是字形：这台机器的中文字体里有什么不该决定按钮长什么样。"""
    left, top, right, bottom = box
    cx, cy = (left + right) // 2, (top + bottom) // 2
    reach = (right - left) // 2
    if glyph == "back":
        paint.line([(cx - reach + 4, cy), (cx + reach - 4, cy)], fill=ink, width=3)
        paint.polygon(
            [(cx - reach + 2, cy), (cx - reach + 12, cy - 8), (cx - reach + 12, cy + 8)], fill=ink
        )
    elif glyph == "home":
        paint.polygon(
            [(cx, cy - reach + 4), (cx - reach + 5, cy - 1), (cx + reach - 5, cy - 1)], fill=ink
        )
        paint.rectangle((cx - reach + 10, cy, cx + reach - 10, cy + reach - 5), fill=ink)
    elif glyph == "recents":
        paint.rounded_rectangle(
            (cx - reach + 4, cy - reach + 10, cx + reach - 12, cy + reach - 4),
            radius=3,
            outline=ink,
            width=2,
        )
        paint.rounded_rectangle(
            (cx - reach + 12, cy - reach + 4, cx + reach - 4, cy + reach - 12),
            radius=3,
            outline=ink,
            width=2,
        )
    elif glyph in ("next", "prev"):
        step = 8 if glyph == "next" else -8
        paint.line([(cx - reach + 6, cy + step), (cx, cy - step)], fill=ink, width=3)
        paint.line([(cx, cy - step), (cx + reach - 6, cy + step)], fill=ink, width=3)
    elif glyph in ("vol_up", "vol_down"):
        paint.polygon(
            [(cx - reach + 4, cy - 4), (cx - reach + 10, cy - 4), (cx - reach + 16, cy - 10)],
            fill=ink,
        )
        paint.rectangle((cx - reach + 4, cy - 6, cx - reach + 12, cy + 6), fill=ink)
        paint.polygon(
            [
                (cx - reach + 11, cy - 7),
                (cx - reach + 18, cy - 13),
                (cx - reach + 18, cy + 13),
                (cx - reach + 11, cy + 7),
            ],
            fill=ink,
        )
        bar = 6
        paint.rectangle((cx + reach - 2 * bar, cy - 1, cx + reach, cy + 1), fill=ink)
        if glyph == "vol_up":
            paint.rectangle((cx + reach - bar, cy - bar, cx + reach - bar + 2, cy + bar), fill=ink)
    else:
        paint.ellipse((cx - 5, cy - 5, cx + 5, cy + 5), fill=ink)


def build_bar(
    actions: Sequence[BarAction], note: str, palette: BubblePalette = DEFAULT_BUBBLE
) -> tuple[str, int, int]:
    """画一帧：一整条底 + 一格一颗按钮 + 底下一行说明。返回 ``(data URL, 宽, 高)``。"""
    from PIL import Image, ImageDraw

    width, height = bar_size(len(actions))
    tile = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    paint = ImageDraw.Draw(tile)
    paint.rounded_rectangle(
        (0, 0, width - 1, height - 1), radius=10, fill=palette.fill, outline=palette.rim, width=1
    )
    font = font_at(HINT_FONT)
    for action, spot in zip(actions, hot_spots(actions), strict=True):
        box = (spot.x, spot.y, spot.x + spot.size - 1, spot.y + spot.size - 1)
        paint.rounded_rectangle(box, radius=8, fill=_chip(palette), outline=palette.rim, width=1)
        glyph_box = (box[0] + 8, box[1] + 4, box[2] - 8, box[3] - 16)
        _draw_glyph(paint, action.glyph, glyph_box, palette.ink)
        if font is not None:
            paint.text(
                ((box[0] + box[2]) // 2, box[3] - 13),
                text=action.label,
                fill=palette.ink,
                font=font,
                anchor="mm",
            )
    if font is not None and note:
        paint.text(
            (PAD, height - NOTE_HEIGHT // 2 - 1),
            text=_elide(paint, note, width - PAD * 2, font),
            fill=palette.ink,
            font=font,
        )
    buffer = io.BytesIO()
    tile.save(buffer, format="PNG")
    return (
        "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode("ascii"),
        width,
        height,
    )


def _elide(paint: Any, text: str, room: int, font: Any) -> str:
    """一行放不下就截断加省略号 —— 条子的宽度由按钮决定，不能为了一句话变宽。"""
    if paint.textlength(text, font=font) <= room:
        return text
    for cut in range(len(text) - 1, 1, -1):
        head = text[:cut] + "…"
        if paint.textlength(head, font=font) <= room:
            return head
    return text[:1]


def _scrcpy_window_rect() -> tuple[int, int, int, int] | None:
    """投屏那个窗口的矩形（物理像素）。找不到窗口给 ``None``。"""
    from jarvis.app.screen_mirror import find_scrcpy_window

    handle = find_scrcpy_window()
    if not handle:
        return None

    class Rect(ctypes.Structure):
        _fields_ = [
            ("left", ctypes.c_long),
            ("top", ctypes.c_long),
            ("right", ctypes.c_long),
            ("bottom", ctypes.c_long),
        ]

    rect = Rect()
    if not ctypes.windll.user32.GetWindowRect(handle, ctypes.byref(rect)):
        return None
    return rect.left, rect.top, rect.right, rect.bottom


def _work_area(mirror: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
    """画面**所在那块屏**的工作区（扣掉任务栏）。拿不到就退回主屏尺寸。"""
    try:
        user32 = ctypes.windll.user32

        class Rect(ctypes.Structure):
            _fields_ = [
                ("left", ctypes.c_long),
                ("top", ctypes.c_long),
                ("right", ctypes.c_long),
                ("bottom", ctypes.c_long),
            ]

        class MonitorInfo(ctypes.Structure):
            _fields_ = [
                ("cbSize", ctypes.c_long),
                ("rcMonitor", Rect),
                ("rcWork", Rect),
                ("dwFlags", ctypes.c_long),
            ]

        # MONITORINFOF_PRIMARY = 1；先按"含这块画面的那块屏"要，拿不到再要主屏。
        handle = user32.MonitorFromRect(ctypes.byref(Rect(*mirror)), 2)
        info = MonitorInfo()
        info.cbSize = ctypes.sizeof(MonitorInfo)
        if handle and user32.GetMonitorInfoW(handle, ctypes.byref(info)):
            work = info.rcWork
            return work.left, work.top, work.right, work.bottom
    except Exception:  # pragma: no cover - 没有 user32 就不是桌面
        logger.debug("读不到投屏所在屏的工作区，用主屏尺寸兜底", exc_info=True)
    try:
        user32 = ctypes.windll.user32
        return 0, 0, int(user32.GetSystemMetrics(0)), int(user32.GetSystemMetrics(1))
    except Exception:  # pragma: no cover
        return 0, 0, 1920, 1080


class MirrorControlBar:
    """那张跟着投屏画面走的小条。"""

    def __init__(
        self,
        mirror: MirrorBarSurface | None,
        *,
        mirror_rect: Callable[[], tuple[int, int, int, int] | None] | None = None,
        work_area: Callable[[tuple[int, int, int, int]], tuple[int, int, int, int]] | None = None,
        palette: Callable[[], BubblePalette] = _skin_neutral,
        window: BarWindow | None = None,
        poll_seconds: float = POLL_SECONDS,
    ) -> None:
        self._mirror = mirror
        self._mirror_rect = mirror_rect or _scrcpy_window_rect
        self._work_area = work_area or _work_area
        # 皮肤是活的：这条卡片和气泡一样由 Python 画，页面换了色它必须跟着换。
        self._palette = palette
        self._window = window
        self._owns_window = window is None
        self._poll = poll_seconds
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._actions: tuple[BarAction, ...] = ()
        self._painted = ""
        self._docked: tuple[int, int] | None = None
        self._detached = False
        self._was_running = False
        self._shown = False
        self._last = ("", 0.0)

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    def start(self) -> bool:
        """起窗 + 起轮询。起不来只说一次，不在每帧上重试 —— 面板里那排按钮还在。"""
        if self._mirror is None:
            return False
        status = self._mirror.status()
        self._actions = bar_actions(
            list(status.get("keys") or []), list(status.get("swipes") or [])
        )
        if not self._actions:
            logger.warning("投屏服务没给出按钮清单，控制条不起")
            return False
        width, height = bar_size(len(self._actions))
        window = self._window or AlphaWindow(class_name="XiaoYeMirrorBar", on_tap=self._on_tap)
        if not window.start(x=OFF_SCREEN, y=OFF_SCREEN, width=width, height=height):
            logger.warning("投屏控制条起不来（拿不到分层窗口），按钮仍在那块面板里")
            return False
        # 不点穿：这一条上的每一格都是要点的。
        window.set_click_through(False)
        # 这里**不能** set_visible(False)：一帧都没合成过的分层窗被 SW_HIDE 之后，
        # UpdateLayeredWindow 对这扇窗永远返回 0（重上样式位、换 ShowWindow 码都救不回来，
        # 只能重建窗口）—— 屏上就是一条"窗在、置顶、按钮在、一个像素都没有"的幽灵。
        # 起窗时它已经在 OFF_SCREEN 外面了，那才是本项目里"藏起来"的正确做法。
        self._window = window
        self._thread = threading.Thread(target=self._loop, name="jarvis-mirror-bar", daemon=True)
        self._thread.start()
        logger.info("投屏控制条上来了：%s 颗按钮", len(self._actions))
        return True

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(2.0)
        self._thread = None
        window = self._window
        if window is not None and self._owns_window:
            window.close()

    def _loop(self) -> None:
        while not self._stop.wait(self._poll):
            try:
                self.refresh()
            except Exception:  # 一轮失败不许带走这条线程：下一轮还可能成
                logger.exception("投屏控制条这一轮没刷成")

    # ------------------------------------------------------------------
    # 每一轮
    # ------------------------------------------------------------------

    def refresh(self) -> None:
        window = self._window
        if window is None or not window.alive or self._mirror is None:
            return
        status = self._mirror.status()
        running = str(status.get("phase") or "") in RUNNING_PHASES
        if not running:
            self._was_running = False
            self._hide(window)
            return
        if not self._was_running:
            # 新开一次投屏：把"用户拖走了"这个状态清掉，重新贴回画面下面。
            self._detached = False
            self._docked = None
        self._was_running = True
        mirror = self._mirror_rect()
        if mirror is None:
            # 窗口还没出来（或者被关了）。不闪一条没有归属的按钮。
            self._hide(window)
            return
        note = bar_note(status, self._last, time.time())
        inks = self._palette()
        fingerprint = f"{len(self._actions)}|{note}|{inks.fill}"
        if fingerprint != self._painted or not window.painted:
            # 后半句是这条链的命：窗口还没在屏上落下任何一个像素，就得重画一次。
            # 只比指纹的话，"第一次合成失败"会永久定格成一条看不见的窗（r103 实拍）。
            url, _, _ = build_bar(self._actions, note, inks)
            if window.present(url):
                window.set_hot_spots(hot_spots(self._actions))
                self._painted = fingerprint
        if not self._shown:
            # 第一次要露脸：窗口一直在那儿，只是停在屏幕外，挪进来就看得见。
            window.set_visible(True)
            self._shown = True
        if self._docked is not None and window.rect[:2] != self._docked:
            self._detached = True  # 被拖走了：位置归用户，不再按画面去贴
        if not self._detached:
            x, y = dock_origin(mirror, window.rect[2], window.rect[3], self._work_area(mirror))
            if (x, y) != window.rect[:2]:
                window.move_to(x, y)
            self._docked = (x, y)
        window.keep_topmost()

    def _hide(self, window: BarWindow) -> None:
        """收起来 = 挪回屏幕外，不是 ``ShowWindow(SW_HIDE)``（理由见 start 里那段）。"""
        if window.rect[:2] != (OFF_SCREEN, OFF_SCREEN):
            window.move_to(OFF_SCREEN, OFF_SCREEN)
        self._docked = None
        self._shown = False

    # ------------------------------------------------------------------
    # 那一下
    # ------------------------------------------------------------------

    def _on_tap(self, name: str) -> None:
        """pump 线程回调：``key:back`` / ``swipe:next`` 落到服务的两扇门上。"""
        kind, _, ident = str(name).partition(":")
        if self._mirror is None or not ident:
            return
        label = next((row.label for row in self._actions if row.name == f"{kind}:{ident}"), ident)
        try:
            if kind == "key":
                self._mirror.press_key(ident)
            elif kind == "swipe":
                self._mirror.swipe(ident)
            else:
                return
        except Exception:
            logger.exception("控制条这一下没发出去：%s", name)
            return
        self._last = (label, time.time())
        self._painted = ""  # 逼一次重画：被拒的原因要立刻写在条上
        try:
            self.refresh()
        except Exception:
            logger.exception("控制条点完没刷成")


__all__ = [
    "BarAction",
    "MirrorBarSurface",
    "MirrorControlBar",
    "bar_actions",
    "bar_note",
    "bar_size",
    "build_bar",
    "dock_origin",
    "hot_spots",
    "short_label",
]
