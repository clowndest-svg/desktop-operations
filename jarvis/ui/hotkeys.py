"""Global hotkeys, for the places where saying the wake word is not an option.

Why a hotkey at all, when the whole point is not having to touch anything
------------------------------------------------------------------------
The wake word is right for a desk at home and wrong for an open office, a shared
living room, or 2 a.m. -- and a "hands-free assistant" that cannot be invoked
quietly is only half of one. Two chords cover that: one to start a spoken turn,
one to bring the window back.

Why ``RegisterHotKey`` and not a keyboard hook
---------------------------------------------
This is the privacy line of the whole feature. ``RegisterHotKey`` hands the thread
a ``WM_HOTKEY`` message **only when one of the two registered chords is pressed**;
every other keystroke on the machine never reaches this process. A low-level
keyboard hook (``WH_KEYBOARD_LL``) would see -- and could log -- everything the
operator types, for a benefit that is one fewer chord. That trade is not worth
making, so it is not made, and the comment is here so the next person does not
"improve" it into a keylogger.

Threading
---------
``RegisterHotKey`` delivers to the window/message queue of the thread that
registered it, so the registrations and the message loop live on one dedicated
thread. It is a daemon: a process that is going away should not be held open by a
hotkey.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes
import logging
import os
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger("jarvis.ui.hotkeys")

WM_HOTKEY = 0x0312
MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004

VK_H = 0x48
VK_K = 0x4B

TALK_CHORD = "Ctrl+Alt+K"
SHOW_CHORD = "Ctrl+Alt+H"
"""The two chords, named once so the tray tooltip, the docs and the registration
cannot disagree about what was promised."""


@dataclass(frozen=True, slots=True)
class Binding:
    """One chord and what it does."""

    name: str
    label: str
    modifiers: int
    virtual_key: int
    handler: Callable[[], object]

    def describe(self) -> str:
        return f"{self.label}（{self.name}）"


@dataclass
class Hotkeys:
    """The registered chords, their failures, and the thread that listens.

    ``failures`` is public because a hotkey that could not be registered is a
    feature that silently does not exist -- the operator pressed the chord and
    nothing happened, and the only way they can find out why is if somebody says
    so. The tray tooltip and the log both read it.
    """

    bindings: Sequence[Binding]
    failures: list[str] = field(default_factory=list)
    registered: int = 0
    _stop: threading.Event = field(default_factory=threading.Event, repr=False)
    _ready: threading.Event = field(default_factory=threading.Event, repr=False)
    _thread: threading.Thread | None = field(default=None, repr=False)

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def describe(self) -> str:
        """One line for a tooltip: what works, and what could not be taken."""
        if self.registered == 0:
            return "全局热键未启用" + (f"（{'；'.join(self.failures)}）" if self.failures else "")
        live = [
            binding.describe() for binding in self.bindings if binding.name not in self.failures
        ]
        line = " · ".join(live)
        if self.failures:
            line += f"（未生效：{'；'.join(self.failures)}）"
        return line

    def start(self) -> bool:
        """Register the chords and start the listener. Returns whether any took.

        The registration happens **on the listener thread**, not here. Measured:
        registering with ``hwnd=None`` from the thread that then goes on to block in
        ``webview.start()`` puts every ``WM_HOTKEY`` into a queue nobody pumps, the
        log cheerfully reports the hotkeys are up, and pressing the chord does
        nothing at all. A hotkey is a promise about a keystroke, so that failure mode
        is worth the comment.
        """
        if self._thread is not None:
            return self.registered > 0
        if os.name != "nt":  # pragma: no cover - the desktop shell is Windows-only
            logger.info("global hotkeys need Windows; skipping")
            return False
        self._stop.clear()
        self._ready.clear()
        self._thread = threading.Thread(target=self._run, name="jarvis-hotkeys", daemon=True)
        self._thread.start()
        if not self._ready.wait(3.0):
            self.failures.append("监听线程没有起来")
            logger.warning("the hotkey thread never reported in")
            return False
        if self.registered:
            logger.info(
                "global hotkeys up: %s", " · ".join(item.describe() for item in self.bindings)
            )
        else:
            logger.warning("no global hotkey could be registered: %s", "；".join(self.failures))
        return self.registered > 0

    def _run(self) -> None:
        """Register, then pump. One thread, because the queue belongs to it."""
        user32 = ctypes.windll.user32
        taken: dict[int, Binding] = {}
        for index, binding in enumerate(self.bindings, start=1):
            if _register(user32, index, binding):
                taken[index] = binding
            else:
                self.failures.append(binding.name)
        self.registered = len(taken)
        self._ready.set()

        message = ctypes.wintypes.MSG()
        while not self._stop.is_set():
            # Polling rather than GetMessageW: GetMessage does not return for a
            # threadless window until it is posted to, and there is nothing else on
            # this thread to post from. 5 wakes a second is nothing next to the
            # process-table walk the telemetry poll already does.
            if not user32.PeekMessageW(ctypes.byref(message), 0, 0, 0, 1):  # PM_REMOVE
                self._stop.wait(0.2)
                continue
            if int(message.message) == WM_HOTKEY:
                pressed: Binding | None = taken.get(int(message.wParam))
                if pressed is not None:
                    _invoke(pressed)
            user32.TranslateMessage(ctypes.byref(message))
            user32.DispatchMessageW(ctypes.byref(message))
        for index in taken:
            _unregister(user32, index)
        self.registered = 0

    def stop(self) -> None:
        """Unregister and join. Idempotent, and safe from any thread."""
        self._stop.set()
        thread = self._thread
        self._thread = None
        if thread is not None:
            thread.join(timeout=1.5)


def _register(user32: Any, index: int, binding: Binding) -> bool:
    try:
        return bool(user32.RegisterHotKey(None, index, binding.modifiers, binding.virtual_key))
    except Exception:  # pragma: no cover - a shell that refuses
        logger.debug("RegisterHotKey failed for %s", binding.name, exc_info=True)
        return False


def _unregister(user32: Any, index: int) -> None:
    try:
        user32.UnregisterHotKey(None, index)
    except Exception:  # pragma: no cover - already gone
        logger.debug("UnregisterHotKey failed for index %s", index, exc_info=True)


def _invoke(binding: Binding) -> None:
    """Run a chord handler without letting it kill the loop.

    The handler shows or hides a window and may be called while the GUI thread is
    busy; an exception here would otherwise be the last thing this thread did, and
    every later press would then do nothing at all.
    """
    try:
        binding.handler()
    except Exception:
        logger.exception("hotkey %s handler failed", binding.name)


def default_bindings(
    *, on_talk: Callable[[], object], on_toggle: Callable[[], object]
) -> list[Binding]:
    """The two chords this app ships, with the handlers the shell supplies."""
    return [
        Binding(TALK_CHORD, "说一句话", MOD_CONTROL | MOD_ALT, VK_K, on_talk),
        Binding(SHOW_CHORD, "显示/隐藏主界面", MOD_CONTROL | MOD_ALT, VK_H, on_toggle),
    ]


__all__ = ["SHOW_CHORD", "TALK_CHORD", "Binding", "Hotkeys", "default_bindings"]
