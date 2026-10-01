"""How a sentence reaches a person: out loud, on the screen, or both.

Why this is an object and not a function
----------------------------------------
Three callers want to say something and none of them should decide alone how it
lands: the scheduler when a reminder comes due, the model when it is asked to read
a paragraph out, and the desktop shell when it wants a tray balloon. Each of those
exists at a different moment of start-up -- the tray does not exist until the
window does -- so the announcer is built empty and *bound* as the parts arrive.
An unbound announcer still works: it returns the sentence it was given and says
nothing happened, which is what a headless run needs.

Why the tray hook is separate from the voice hook
-------------------------------------------------
They fail differently. No microphone means the reminder is silent; no tray means
nobody sees it while the window is closed. Reporting both in one string ("已播报")
would let a reminder that neither spoke nor popped look delivered, so
:meth:`announce` names which channels actually fired.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

logger = logging.getLogger("jarvis.app.announcer")

_SPEAK_LIMIT = 120
"""How much of a sentence goes into the tool's own answer. The point of the return
value is that the model can tell it said something, not that it re-reads it."""


class Announcer:
    """The one seam between "say this" and the things that can say it."""

    def __init__(
        self,
        *,
        speak: Callable[[str], bool] | None = None,
        notify: Callable[..., object] | None = None,
    ) -> None:
        self._speak = speak
        self._notify = notify

    def bind(
        self,
        *,
        speak: Callable[[str], bool] | None = None,
        notify: Callable[..., object] | None = None,
    ) -> None:
        """Attach whichever channel exists now. ``None`` leaves the other alone."""
        if speak is not None:
            self._speak = speak
        if notify is not None:
            self._notify = notify

    @property
    def channels(self) -> tuple[str, ...]:
        """Which ways out are wired up right now -- read by the HUD's panel."""
        ways: list[str] = []
        if self._speak is not None:
            ways.append("voice")
        if self._notify is not None:
            ways.append("tray")
        return tuple(ways)

    def announce(self, text: str, *, title: str = "小夜") -> str:
        """Deliver one sentence. Returns a line naming what actually happened.

        Never raises: this runs on the scheduler's thread, where an exception would
        be logged by someone else's code and the reminder would be recorded as
        failed for a reason the operator cannot act on.
        """
        body = " ".join(str(text).split())
        if not body:
            return "没有可播报的内容"
        fired: list[str] = []
        if self._speak is not None:
            try:
                if self._speak(body):
                    fired.append("已开口")
                else:
                    fired.append("没出声（麦克风/合成未就绪）")
            except Exception:
                logger.exception("announcing out loud failed")
                fired.append("没出声（播报失败）")
        if self._notify is not None:
            try:
                self._notify(body, title)
                fired.append("已弹托盘")
            except TypeError:
                # A notifier that only takes the message is a perfectly good
                # notifier; do not lose the balloon over a signature.
                try:
                    self._notify(body)
                    fired.append("已弹托盘")
                except Exception:
                    logger.exception("tray notification failed")
            except Exception:
                logger.exception("tray notification failed")
        if not fired:
            logger.info(
                "nothing is bound to the announcer; the sentence was dropped: %s", body[:60]
            )
            return "没有可用的播报通道（语音与托盘都没接上）"
        return f"{body[:_SPEAK_LIMIT]} —— {'，'.join(fired)}"


__all__ = ["Announcer"]
