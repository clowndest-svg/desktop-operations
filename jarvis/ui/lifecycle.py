"""What happens when the operator closes the window -- and what happens instead.

The rule this module exists to hold: **the X is not the exit.** Clicking it hides
the HUD and leaves the process running, because the microphone, the wake word and
the scheduled tasks are the reason the app is open, and none of them want the window
to be visible. Only the tray menu's 「退出小夜」 ends the process, and it ends it
through pywebview's own close path so the ``finally`` in
:func:`jarvis.ui.desktop.run` still releases the microphone.

Two Windows-specific traps shaped the code below, and both are silent failures if
you get them wrong:

* ``window.events.closing`` handlers run **on the WinForms UI thread, inline**, so
  the hide has to be deferred to another thread -- calling ``evaluate_js`` from here
  would block waiting for a JavaScript answer that only the UI thread can deliver.
* A handler's return value decides whether the close proceeds, and pywebview reads
  ``False`` as "cancel" (``webview/event.py`` collects the values that ``is False``
  and sets ``args.Cancel``). Returning ``True`` means "let it close". That is the
  opposite of what every other framework does, so it is spelled out at
  :meth:`ShellLifecycle.on_closing` rather than left to be rediscovered.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from jarvis.core.events import PIPELINE_STATE_KIND, VOICE_STATUS_KIND
from jarvis.ui.tray import (
    STATUS_HEARING,
    STATUS_OFF,
    STATUS_WAITING,
    STATUS_WORKING,
)

logger = logging.getLogger("jarvis.ui.lifecycle")

BACKGROUND_SCRIPT = "window.__jarvisBackground && window.__jarvisBackground({value});"
"""Tells the page it is no longer on screen.

WebView2 keeps a hidden window's page *visible* as far as the Page Visibility API
is concerned -- ``document.hidden`` stays ``false`` because pywebview never touches
``CoreWebView2Controller.IsVisible``. So the HUD's own "slow down when nobody is
looking" rule would never fire, and the window would keep walking the whole process
table every 1.5 seconds for hours. This is the missing signal, and it is said out
loud rather than inferred.
"""

_HIDING_HINT = "小夜已收进托盘 · 麦克风仍在听，说「你好小夜」就能唤醒"
"""The balloon shown once, the first time the window disappears.

Not on every hide: a person who has done it twice knows where the app went, and a
notification that keeps arriving trains them to ignore the one that matters."""

_PHASE_RUNNING = "running"
"""``VoicePhase.RUNNING``: the microphone is open. Every other phase means it is
not, and the icon has to say that rather than keep glowing."""

#: The ``state`` events the pipeline actually emits, mapped to what the icon claims.
#: Written from ``jarvis/orchestration/voice_pipeline.py``'s own ``_emit`` calls, not
#: from the docstring's wish list: there is no ``reply`` event, so nothing here
#: pretends to know when the assistant started talking.
_STATE_TO_STATUS: dict[str, str] = {
    "listening": STATUS_HEARING,
    "processing": STATUS_WORKING,
    "idle": STATUS_WAITING,
}

#: Milestone events and what they mean for the icon, for the same reason.
_KIND_TO_STATUS: dict[str, str] = {
    "speech_start": STATUS_HEARING,
    "wake": STATUS_HEARING,
    "barge_in": STATUS_HEARING,
    "speech_end": STATUS_WORKING,
}


class _Window(Protocol):
    """The slice of ``webview.Window`` this module calls."""

    def show(self) -> None: ...

    def hide(self) -> None: ...

    def restore(self) -> None: ...

    def destroy(self) -> None: ...

    def evaluate_js(self, script: str) -> Any: ...


class _Pet(Protocol):
    """The pet window, as far as the lifecycle is concerned."""

    @property
    def shown(self) -> bool: ...

    @property
    def audio_window(self) -> Any | None: ...

    def toggle(self) -> bool: ...

    def summon(self) -> bool: ...

    def close(self) -> None: ...


class _Tray(Protocol):
    """What the lifecycle needs from :class:`jarvis.ui.tray.TrayIcon`.

    A protocol rather than the class so the tests can state the promise -- "the icon
    is told the truth, and it is the last thing stopped on the way out" -- without a
    notification area, and so a future tray implementation can be swapped in without
    touching the policy.
    """

    @property
    def running(self) -> bool: ...

    def set_status(self, status: str) -> None: ...

    def notify(self, message: str, title: str = "小夜") -> bool: ...

    def stop(self) -> None: ...


@dataclass(frozen=True)
class ShellState:
    """What the shell can currently do, and what it is currently doing."""

    visible: bool
    tray: bool
    status: str
    pet: bool

    def to_mapping(self) -> dict[str, object]:
        return {
            "visible": self.visible,
            "tray": self.tray,
            "status": self.status,
            "pet": self.pet,
        }


class ShellLifecycle:
    """Hide-vs-quit decisions for the desktop shell, with no GUI imports.

    The shell hands in the window it wants governed and the tray that governs it;
    everything else is a callback, which is what makes the whole policy testable
    without starting WinForms.
    """

    def __init__(
        self,
        window: _Window | None = None,
        *,
        tray: _Tray | None = None,
        pet: _Pet | None = None,
        hide_to_tray: bool = True,
        on_audio_window: Callable[[Any], None] | None = None,
    ) -> None:
        self._window = window
        self._tray: _Tray | None = tray
        self._pet = pet
        # The policy: whether the operator asked for hide-instead-of-quit. Whether
        # it is *possible* is a second question, answered by :attr:`hides_to_tray`.
        self._hide_to_tray = hide_to_tray
        self._on_audio_window = on_audio_window
        self._exiting = False
        self._visible = True
        self._minimized = False
        self._hinted = False
        self._status = STATUS_OFF

    # ------------------------------------------------------------------
    # Attaching
    # ------------------------------------------------------------------

    def attach_window(self, window: _Window) -> None:
        self._window = window

    def attach_tray(self, tray: _Tray | None) -> None:
        self._tray = tray

    def attach_pet(self, pet: _Pet) -> None:
        self._pet = pet

    @property
    def hides_to_tray(self) -> bool:
        """Whether the X should hide the window.

        Asked of the tray every time, not latched at startup: an icon that failed to
        appear -- pystray not installed, a shell that refused the notification area
        -- must send the X back to meaning "quit". The alternative is an app that
        vanishes with no visible way back, and the only exit left is Task Manager.
        """
        return bool(self._hide_to_tray and self._tray is not None and self._tray.running)

    @property
    def tray_status(self) -> str:
        return self._status

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    @property
    def visible(self) -> bool:
        """Whether the HUD is on screen, as far as this object knows.

        Read from the flag rather than ``window.is_visible()`` because the page
        cannot see the tray's hide and the window object's answer costs a UI-thread
        round trip; the flag is set on every path that can change it.
        """
        return self._visible

    @property
    def exiting(self) -> bool:
        return self._exiting

    def state(self) -> ShellState:
        return ShellState(
            visible=self._visible,
            tray=self.hides_to_tray,
            status=self._status,
            pet=bool(self._pet is not None and self._pet.shown),
        )

    def to_mapping(self) -> dict[str, object]:
        return self.state().to_mapping()

    # ------------------------------------------------------------------
    # The X
    # ------------------------------------------------------------------

    def on_closing(self) -> bool:
        """Register on ``window.events.closing``. **Return ``False`` to cancel.**

        pywebview cancels the close when a handler returns exactly ``False``, so
        that is what "hide instead of quit" looks like here. When we are already
        exiting -- or there is no tray to hide into -- the answer is ``True`` and
        the window really does close, which is what ends the message loop and lets
        the microphone go.
        """
        if self._exiting or not self.hides_to_tray:
            return True
        self._defer(self.hide)
        return False

    def _defer(self, action: Callable[[], object]) -> None:
        """Run ``action`` just after the closing handler returns.

        A one-shot thread, not a timer: the UI thread has to be out of
        ``FormClosing`` before anything here calls back into it, and this is the
        only moment the hide is certain to be safe. A failure is logged rather than
        raised, because an exception on this path would be an exception inside a
        GUI event handler on the way out of the app.
        """

        def run() -> None:
            try:
                action()
            except Exception:  # pragma: no cover - a GUI that will not hide
                logger.exception("deferred window action failed")

        threading.Thread(target=run, name="jarvis-window-hide", daemon=True).start()

    # ------------------------------------------------------------------
    # Show / hide
    # ------------------------------------------------------------------

    def hide(self) -> bool:
        """Put the window away without touching the process or the microphone."""
        window = self._window
        if window is None:
            return False
        try:
            window.hide()
        except Exception:  # pragma: no cover - a GUI that will not hide
            logger.exception("could not hide the window")
            return False
        self._visible = False
        self._announce_background(False)
        self.set_audio_owner()
        self._tray_hint()
        logger.info("window hidden to tray (voice keeps running)")
        return True

    def show(self) -> bool:
        """Bring the window back, and un-minimise it if that is where it was left."""
        window = self._window
        if window is None:
            return False
        try:
            window.show()
            # ``show()`` un-hides; it does not un-minimise, and an operator who sent
            # the window to the taskbar before clicking the tray icon wants it back,
            # not a rectangle that is still down there. ``restore()`` is asked for
            # from the tracked flag rather than "always", because restoring a
            # maximised window is how 「铺满」 gets silently undone.
            if self._minimized:
                restore = getattr(window, "restore", None)
                if callable(restore):
                    restore()
        except Exception:  # pragma: no cover - a GUI that will not show
            logger.exception("could not show the window")
            return False
        self._visible = True
        self._announce_background(True)
        self.set_audio_owner()
        return True

    def on_minimized(self) -> None:
        """Register on ``window.events.minimized``."""
        self._minimized = True

    def on_restored(self) -> None:
        """Register on ``window.events.restored`` (fires for minimise *and* maximise)."""
        self._minimized = False

    def toggle(self) -> bool:
        """Left-click on the tray icon. Returns the visibility after the click."""
        if self._visible:
            self.hide()
        else:
            self.show()
        return self._visible

    def activate(self) -> bool:
        """Menu entry 「显示主界面」: always ends with a window in front."""
        return self.show()

    def set_audio_owner(self) -> None:
        """Hand the speech channel to whichever window is actually on screen.

        Two WebView2 windows cannot both play the answer -- the operator would hear
        it twice, slightly out of time. So exactly one of them is fed the PCM, and
        the rule is the visible one: the HUD while it is up, the pet when the HUD is
        in the tray and the figure is on the desktop. A page that is neither is left
        in charge, which is how the assistant still talks out loud when both windows
        are hidden.
        """
        handler = self._on_audio_window
        if handler is None:
            return
        window: Any = self._window
        pet = self._pet
        if not self._visible and pet is not None and pet.shown:
            window = pet.audio_window
        handler(window)

    def _announce_background(self, foreground: bool) -> None:
        window = self._window
        if window is None:
            return
        script = BACKGROUND_SCRIPT.format(value="true" if foreground else "false")
        try:
            window.evaluate_js(script)
        except Exception:  # pragma: no cover - the page may be mid-navigation
            logger.debug("background notice did not reach the page", exc_info=True)

    def _tray_hint(self) -> None:
        """Tell the operator once where the app went."""
        if self._hinted:
            return
        self._hinted = True
        tray = self._tray
        if tray is not None:
            tray.notify(_HIDING_HINT)

    # ------------------------------------------------------------------
    # Voice state -> what the icon claims
    # ------------------------------------------------------------------

    def on_event(self, event: object) -> None:
        """Feed one pipeline event to the tray icon. Never raises.

        The icon is the only place the microphone's status is visible once the
        window is hidden, so it has to change when the microphone changes. Unknown
        kinds are ignored rather than guessed at: an unrecognised event is not
        evidence that the assistant stopped listening.
        """
        kind = getattr(event, "kind", None)
        if not isinstance(kind, str):
            return
        text = str(getattr(event, "text", "") or "")
        if kind == VOICE_STATUS_KIND:
            self._publish(STATUS_WAITING if text == _PHASE_RUNNING else STATUS_OFF)
            return
        if kind == PIPELINE_STATE_KIND:
            mapped = _STATE_TO_STATUS.get(text)
            if mapped is not None:
                # A pipeline that reports "idle" with the microphone released is
                # still not listening; only a voice_status event may say otherwise.
                self._publish(mapped if self._status != STATUS_OFF else STATUS_OFF)
            return
        mapped = _KIND_TO_STATUS.get(kind)
        if mapped is not None:
            self._publish(mapped if self._status != STATUS_OFF else STATUS_OFF)
        if kind == "wake":
            self.summon_pet()

    def _publish(self, status: str) -> None:
        self._status = status
        tray = self._tray
        if tray is not None:
            tray.set_status(status)

    def set_voice_running(self, running: bool) -> None:
        """Set the icon from the service's own answer, not from an event stream.

        Used at startup and after 释放麦克风, where there may never have been an
        event to observe.
        """
        self._publish(STATUS_WAITING if running else STATUS_OFF)

    # ------------------------------------------------------------------
    # The pet
    # ------------------------------------------------------------------

    def summon_pet(self) -> bool:
        """The wake word while the HUD is in the tray: the figure answers, not a window.

        Only ever *re-plays* the arrival -- it does not switch the pet on by itself.
        A figure appearing on the desktop that nobody asked for is the desktop-pet
        equivalent of the assistant talking unprompted.
        """
        pet = self._pet
        if pet is None or self._visible or not pet.shown:
            return False
        return bool(pet.summon())

    def toggle_pet(self) -> bool:
        """Tray menu 「桌面宠物」. Returns whether the pet ended up shown."""
        pet = self._pet
        if pet is None:
            logger.info("no pet window wired into this shell")
            return False
        shown = bool(pet.toggle())
        self.set_audio_owner()
        return shown

    def pet_shown(self) -> bool:
        """Read by the tray menu to tick itself."""
        pet = self._pet
        return bool(pet is not None and pet.shown)

    # ------------------------------------------------------------------
    # The only real exit
    # ------------------------------------------------------------------

    def quit(self) -> None:
        """End the process: tray first, then every window.

        Destroying the windows is what makes pywebview's message loop return, and
        the teardown in :func:`jarvis.ui.desktop.run` -- which unsubscribes from the
        voice service, stops the audio channel and stops ``voice`` -- is what
        releases the microphone. Setting ``_exiting`` first is what lets those
        destroys through the handler above instead of being bounced back as a hide.
        """
        if self._exiting:
            return
        self._exiting = True
        logger.info("tray quit requested: closing windows and releasing the microphone")
        tray = self._tray
        if tray is not None:
            tray.stop()
        pet = self._pet
        if pet is not None:
            try:
                pet.close()
            except Exception:  # pragma: no cover - a window already going away
                logger.debug("pet close failed", exc_info=True)
        window = self._window
        if window is not None:
            try:
                window.destroy()
            except Exception:  # pragma: no cover - the loop ends either way
                logger.exception("could not destroy the main window")

    def shutdown(self) -> None:
        """Belt-and-braces teardown on the way out of ``webview.start()``.

        Reaching this normally means the loop already ended, and a window that is
        gone cannot hold an icon. It is here because a tray icon that outlives its
        process is exactly the kind of leak nobody sees until the third launch has
        three of them.
        """
        self._exiting = True
        tray = self._tray
        if tray is not None:
            tray.stop()


__all__ = ["BACKGROUND_SCRIPT", "ShellLifecycle", "ShellState"]
