"""The wake greeting: what she says on the wake word, and when she may say it.

Two requirements meet here and neither one owns the other.

*The words* are the operator's: a sentence that is read out on every wake has to be
editable, and an empty string means "say nothing" -- a switch, not a mystery.

*The moment* belongs to the desktop figure. The operator asked that she greet only once
the character is fully on screen, because a voice that starts while the figure is still
materialising out of the wormhole reads as a broken animation. That signal lives in the
page (which owns what the arrival looks like) and is reported through the shell, so this
object is where the two worlds hand the state over: the shell says "an arrival is being
played", the page says "it finished", and the voice pipeline asks whether it may speak.

Why a bounded wait and not a plain gate: the report comes from JavaScript. A stale UI
bundle, an exception in the page, or a pet that was never summoned must not turn the
greeting into something that silently never happens -- so the wait has a deadline, it
logs when it gives up, and then it lets her speak anyway.

Why nothing waits at all most of the time: with the HUD window on screen the shell does
not play an arrival (the operator's own rule: a figure appearing over the window they are
reading is a jump scare), and with the pet switched off there is no window to wait for.
Both cases are "nothing is pending", which is the same answer as "already on screen".
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable

logger = logging.getLogger("jarvis.app.wake_greeting")

DEFAULT_GREETING: str = "您好，主人，我是智能语音助手，小夜"
"""What she says on the wake word until the operator writes something else.

Empty in the settings panel means "say nothing", which is a switch rather than a bug --
the alternative would be a sentence nobody can get rid of.
"""

MAX_GREETING_CHARS: int = 200
"""Ceiling on the stored sentence.

It is spoken in full before the microphone opens again, so a long value is a long wait
rather than a longer greeting; the page enforces the same number from the snapshot.
"""

DEFAULT_TIMEOUT_SECONDS: float = 2.5
"""How long to wait for the figure before greeting anyway.

The arrival itself is 1.7 s on this machine (``PetStage.vue``'s ``EMERGE_SECONDS``) and
the report crosses a JS↔Python bridge, so the bound is the animation plus slack rather
than a guess. Longer would make the wake word feel broken on a machine where nobody ever
reports; shorter would cut the greeting off in front of a figure still half-drawn.
"""

_POLL_SECONDS: float = 0.05
"""How often the gate re-checks for the report. The slack a greeting can be late by."""


class WakeGreeter:
    """The greeting text plus the "may I speak now" gate in front of it."""

    def __init__(
        self,
        text_provider: Callable[[], str],
        *,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Create the gate.

        Args:
            text_provider: The operator's current wording, read per wake so the
                settings panel takes effect on the next one rather than after a
                restart. An empty string means "do not greet".
            timeout_seconds: How long :meth:`wait_for_figure` blocks before giving up.
            sleep: Injectable so the bounded wait is testable without real time.
            clock: Same, for the deadline arithmetic.
        """
        self._text_provider = text_provider
        self._timeout = max(0.0, float(timeout_seconds))
        self._sleep = sleep
        self._clock = clock
        self._lock = threading.Lock()
        self._awaiting = False
        self._started_at = 0.0
        self._skips = 0
        """How many greetings were greeted-without-a-signal. Logged, never silent."""

    # -- the words ---------------------------------------------------------

    def text(self) -> str:
        """The sentence to speak, or ``""`` when the operator turned it off."""
        try:
            raw = self._text_provider()
        except Exception:  # a settings read must not cost the wake word
            logger.exception("could not read the wake greeting; staying silent this turn")
            return ""
        return (raw or "").strip()

    # -- the gate ----------------------------------------------------------

    def expect_figure(self) -> None:
        """Say that an arrival animation is being played, so a greeting must wait.

        Called from the shell the moment it commands the figure to emerge. Nothing is
        armed if the page reports first -- a report that arrives before the expectation
        is the "she is already on screen" case, which must not be turned into a wait.
        """
        with self._lock:
            self._awaiting = True
            self._started_at = self._clock()
        logger.debug("waiting for the figure to finish appearing before greeting")

    def figure_arrived(self) -> None:
        """The page says the figure is fully drawn. Release anyone waiting."""
        with self._lock:
            was_awaiting = self._awaiting
            self._awaiting = False
        if was_awaiting:
            logger.debug("the figure is on screen; the greeting may speak")

    @property
    def skipped_waits(self) -> int:
        """How many times the deadline expired without a report.

        The one way to tell "the page reports and she waits for it" apart from "the page
        never reports and the greeting is permanently late" -- which is what the log
        warning says once per wake, and what a test can assert on.
        """
        with self._lock:
            return self._skips

    def wait_for_figure(self, should_stop: Callable[[], bool] | None = None) -> bool:
        """Block until the figure is on screen, or the bound says enough.

        Returns whether the gate opened on a report rather than on the deadline --
        ``False`` is counted and logged, because a greeting that always arrives late is
        how "the pet never shows up" gets reported as a voice bug.

        A poll on the injectable clock, deliberately not a condition wait. The first
        version waited on a condition and timed itself against a clock the test could
        advance -- and a test-supplied clock never advanced on its own, so the loop ran
        forever. Sleeping through :attr:`_sleep` keeps the deadline and the passage of
        time on the same axis, which is what makes a 1.7-second bound testable in the
        first place. Fifty milliseconds of latency before the greeting is not a cost
        anybody can hear.
        """
        reported = True
        while True:
            with self._lock:
                if not self._awaiting:
                    break
                if should_stop is not None and should_stop():
                    # The app is going away; the greeting is not worth waiting for.
                    self._awaiting = False
                    reported = False
                    break
                if self._clock() - self._started_at >= self._timeout:
                    self._awaiting = False
                    reported = False
                    break
            self._sleep(_POLL_SECONDS)
        if not reported:
            self._note_skip()
        return reported

    # -- internals ---------------------------------------------------------

    def _note_skip(self) -> None:
        """Count and say out loud that the report never came."""
        with self._lock:
            self._skips += 1
        logger.warning(
            "the page never reported the figure as shown within %.1fs; greeting anyway "
            "(check the UI bundle matches the shell -- an old bundle has no such report)",
            self._timeout,
        )


__all__ = ["DEFAULT_GREETING", "DEFAULT_TIMEOUT_SECONDS", "MAX_GREETING_CHARS", "WakeGreeter"]
