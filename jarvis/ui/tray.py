"""A tray icon, so that closing the window is not the same as leaving the app.

Why this exists
---------------
The assistant's whole value is that it is *already running*: the operator says the
wake word and gets an answer without opening anything. Closing the window with the
X used to end the process, which threw that away -- and worse, it looked like a
normal thing to do. So the X now hides the window and the process stays up, and this
module is the only door back out. It has to be discoverable, which is why the icon
shows whether the microphone is armed: an app that keeps listening needs to keep
saying so, even when it has no window.

Why pystray rather than the WinForms ``NotifyIcon``
--------------------------------------------------
pywebview 6.2 ships no tray at all (no ``Shell_NotifyIcon`` anywhere in its
package), and reaching for ``pythonnet`` directly would mean a second CLR bridge
beside the one pywebview already owns. pystray talks to the notification area
through plain ``ctypes`` and runs its own message loop on its own thread, which is
what a window already blocked inside ``webview.start()`` needs.

Both imports here are lazy: the console build and every test that does not touch
the tray must not need Pillow or pystray installed.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

logger = logging.getLogger("jarvis.ui.tray")

ICON_PIXELS = 32
"""The bitmap size pystray is handed.

Windows draws the notification area at 16 px (32 px at 200% scaling), and pystray
scales this down itself. Drawing at 32 and keeping the mark to a ring and a dot is
the same lesson ``scripts/make_icon.py`` learned for the executable icon: fine
detail turns to mud in the taskbar, concentric circles do not.
"""

CYAN = (56, 189, 248, 255)
CYAN_CORE = (186, 240, 255, 255)
AMBER = (255, 181, 71, 255)
GREY = (116, 134, 152, 255)
GREY_CORE = (150, 168, 186, 255)
BACKDROP = (11, 19, 33, 235)
EDGE = (40, 62, 88, 255)

#: Geometry copied from ``scripts/make_icon.py`` rather than imported: that script
#: is deliberately stdlib-only (an icon build must not need Pillow), and ``scripts``
#: is not a package. Two renderers, one mark -- the numbers are the contract.
CORNER_RADIUS = 0.22
RING_RADIUS = 0.33
CORE_RADIUS = 0.105
CORE_RADIUS_HOT = 0.155

STATUS_OFF = "off"
STATUS_WAITING = "waiting"
STATUS_HEARING = "hearing"
STATUS_WORKING = "working"
"""The four things the icon can honestly claim.

``waiting`` is the state that has to be visible: the microphone is open and nothing
is happening. Calling it "idle" and drawing it grey would describe the pipeline's
opinion rather than the operator's privacy situation.
"""

#: ``(ring, core)``, and whether the core is drawn large.
_INK: dict[str, tuple[tuple[int, int, int, int], tuple[int, int, int, int], bool]] = {
    STATUS_OFF: (GREY, GREY_CORE, False),
    STATUS_WAITING: (CYAN, CYAN_CORE, False),
    STATUS_HEARING: (CYAN, CYAN_CORE, True),
    STATUS_WORKING: (AMBER, (255, 226, 168, 255), True),
}

TITLES: dict[str, str] = {
    STATUS_OFF: "小夜 · 麦克风未开启（后台待命）",
    STATUS_WAITING: "小夜 · 正在听，等唤醒词「你好小夜」",
    STATUS_HEARING: "小夜 · 正在收你这句话",
    STATUS_WORKING: "小夜 · 正在识别并思考",
}


def draw_icon(status: str, *, pixels: int = ICON_PIXELS) -> Any:
    """Render the tray bitmap for one state.

    The executable's own mark -- rounded square, ring, core -- with the *colour and
    size of the core* carrying the state, because those are the two things still
    legible after Windows shrinks the image to 16 px. Returns a
    :class:`PIL.Image.Image`; ``Any`` in the signature so this module imports
    without Pillow.
    """
    from PIL import Image, ImageDraw

    ring_ink, core_ink, hot = _INK.get(status, _INK[STATUS_OFF])
    image = Image.new("RGBA", (pixels, pixels), (0, 0, 0, 0))
    pen = ImageDraw.Draw(image)
    pen.rounded_rectangle(
        (1, 1, pixels - 2, pixels - 2),
        radius=max(2, int(pixels * CORNER_RADIUS)),
        fill=BACKDROP,
        outline=EDGE,
        width=1,
    )
    ring = pixels * RING_RADIUS
    width = max(1, pixels // 14)
    pen.ellipse(
        (pixels / 2 - ring, pixels / 2 - ring, pixels / 2 + ring, pixels / 2 + ring),
        outline=ring_ink,
        width=width,
    )
    core = pixels * (CORE_RADIUS_HOT if hot else CORE_RADIUS)
    pen.ellipse(
        (pixels / 2 - core, pixels / 2 - core, pixels / 2 + core, pixels / 2 + core),
        fill=core_ink,
    )
    return image


def _menu_items(
    builder: Any,
    *,
    on_activate: Callable[[], object],
    on_quit: Callable[[], object],
    on_toggle_pet: Callable[[], object] | None,
    pet_shown: Callable[[], bool],
    on_toggle_autostart: Callable[[], object] | None = None,
    autostart_on: Callable[[], bool] = lambda: False,
    on_toggle_mobile: Callable[[], object] | None = None,
    mobile_on: Callable[[], bool] = lambda: False,
) -> tuple[Any, ...]:
    """The right-click menu, as a tuple a test can read without pystray installed.

    「显示主界面」 is the default entry: pystray's documented way to make a left
    click do the obvious thing is to mark one item ``default=True``, and the
    obvious thing is to bring the window back.
    """
    items = [builder.MenuItem("显示主界面", on_activate, default=True)]
    if on_toggle_pet is not None:
        items.append(builder.MenuItem("桌面宠物", on_toggle_pet, checked=lambda _item: pet_shown()))
    if on_toggle_autostart is not None:
        # A tick that is read back from the registry each time the menu opens, not
        # remembered here: two truths about "does this start at logon" is how the
        # menu ends up lying after someone edits the key by hand.
        items.append(
            builder.MenuItem("开机自启", on_toggle_autostart, checked=lambda _item: autostart_on())
        )
    if on_toggle_mobile is not None:
        # The tick is whether the *port is open*, read the same way as 开机自启. A
        # menu that says 手机接入 is on while the bind failed would be the worst lie
        # this app can tell: it would mean a network listener the operator thinks
        # they closed.
        items.append(
            builder.MenuItem("手机接入", on_toggle_mobile, checked=lambda _item: mobile_on())
        )
    items.append(builder.Menu.SEPARATOR)
    items.append(builder.MenuItem("退出小夜", on_quit))
    return tuple(items)


class TrayIcon:
    """The notification-area icon and the only real exit from the app.

    Nothing here knows about pywebview: the shell passes callbacks in and this
    object calls them on pystray's own thread. That keeps the privacy-relevant
    decisions (what the icon claims, when it stops existing) in one testable place.

    Args:
        on_toggle_mobile: Called by 「手机接入」. Optional -- a build without the LAN
            endpoint (no ``mobile`` config) must not show a menu item that opens a
            port it cannot then serve.
        mobile_on: Whether the listener is open right now, read from the gateway
            each time the menu is drawn. The tick is a claim about a socket, so it
            is asked, not remembered: after a bind failure the honest tick is off.
    """

    def __init__(
        self,
        *,
        name: str = "小夜",
        on_activate: Callable[[], object],
        on_quit: Callable[[], object],
        on_toggle_pet: Callable[[], object] | None = None,
        pet_shown: Callable[[], bool] = lambda: False,
        on_toggle_autostart: Callable[[], object] | None = None,
        autostart_on: Callable[[], bool] = lambda: False,
        on_toggle_mobile: Callable[[], object] | None = None,
        mobile_on: Callable[[], bool] = lambda: False,
    ) -> None:
        self._name = name
        self._on_activate = on_activate
        self._on_quit = on_quit
        self._on_toggle_pet = on_toggle_pet
        self._pet_shown = pet_shown
        self._on_toggle_autostart = on_toggle_autostart
        self._autostart_on = autostart_on
        self._on_toggle_mobile = on_toggle_mobile
        self._mobile_on = mobile_on
        self._icon: Any | None = None
        self._status = STATUS_OFF
        self._reason = ""

    @property
    def running(self) -> bool:
        return self._icon is not None

    @property
    def status(self) -> str:
        """What the icon currently claims -- also what the HUD says when it has one."""
        return self._status

    @property
    def detail(self) -> str:
        """Why there is no icon; empty while there is one."""
        return self._reason

    def start(self) -> bool:
        """Put the icon in the notification area. Returns whether it is there.

        ``False`` is not a cosmetic failure. The shell reads this answer and keeps
        the X meaning "quit" when there is no tray to quit from -- an app that hides
        with no visible way back is a process the operator has to kill in Task
        Manager, which is a worse outcome than losing the feature.
        """
        try:
            import pystray
        except Exception as exc:  # pragma: no cover - only on a machine without it
            self._reason = f"托盘不可用：{exc}"
            logger.warning("system tray unavailable: %s", exc)
            return False
        try:
            menu = pystray.Menu(
                *_menu_items(
                    pystray,
                    on_activate=self._call_activate,
                    on_quit=self._call_quit,
                    on_toggle_pet=self._call_toggle_pet,
                    pet_shown=self._pet_shown,
                    on_toggle_autostart=self._call_toggle_autostart,
                    autostart_on=self._autostart_on,
                    on_toggle_mobile=self._call_toggle_mobile,
                    mobile_on=self._mobile_on,
                )
            )
            icon = pystray.Icon(
                self._name,
                icon=draw_icon(self._status),
                title=TITLES[self._status],
                menu=menu,
            )
            icon.run_detached()
        except Exception as exc:  # pragma: no cover - a desktop without a shell
            self._reason = f"托盘启动失败：{exc}"
            logger.exception("could not start the system tray icon")
            return False
        self._icon = icon
        logger.info("tray icon up: %s", TITLES[self._status])
        return True

    def set_status(self, status: str) -> None:
        """Redraw and re-hover. Unknown states fall back to 「正在听」 rather than lying.

        Cheap enough to call on every pipeline event: two attribute assignments, and
        pystray repaints on its own thread.
        """
        if status not in TITLES:
            status = STATUS_WAITING
        if status == self._status and self._icon is not None:
            return
        self._status = status
        icon = self._icon
        if icon is None:
            return
        try:
            # Title first: the hover text is the precise claim, and a bitmap that
            # failed to render must not leave the icon still saying 「未开启」.
            icon.title = TITLES[status]
            icon.icon = draw_icon(status)
        except Exception:  # pragma: no cover - the icon vanished mid-update
            logger.debug("tray repaint skipped", exc_info=True)

    def notify(self, message: str, title: str = "小夜") -> bool:
        """A balloon in the notification area, for the moment the window disappears.

        Returns whether it was shown. The caller does not retry: a person who just
        clicked X does not need three copies of "I am still in the tray".
        """
        icon = self._icon
        if icon is None:
            return False
        try:
            icon.notify(message, title)
        except Exception:  # pragma: no cover - shells that suppress balloons
            logger.debug("tray notification failed", exc_info=True)
            return False
        return True

    def stop(self) -> None:
        """Take the icon out. Idempotent, and safe from any thread."""
        icon = self._icon
        self._icon = None
        if icon is None:
            return
        try:
            icon.visible = False
            icon.stop()
        except Exception:  # pragma: no cover - nothing left to do if this fails
            logger.debug("tray icon stop failed", exc_info=True)

    # The menu hands pystray's own ``icon`` and ``item`` to callbacks; these adapt
    # them back to the zero-argument callables the shell passed in. pystray wraps
    # short-argument callables itself, so naming them here keeps that guess local.
    def _call_activate(self) -> None:
        self._on_activate()

    def _call_quit(self) -> None:
        self._on_quit()

    def _call_toggle_pet(self) -> None:
        handler = self._on_toggle_pet
        if handler is not None:
            handler()

    def _call_toggle_autostart(self) -> None:
        handler = self._on_toggle_autostart
        if handler is not None:
            handler()

    def _call_toggle_mobile(self) -> None:
        handler = self._on_toggle_mobile
        if handler is not None:
            handler()


__all__ = [
    "ICON_PIXELS",
    "STATUS_HEARING",
    "STATUS_OFF",
    "STATUS_WAITING",
    "STATUS_WORKING",
    "TITLES",
    "TrayIcon",
    "draw_icon",
]
