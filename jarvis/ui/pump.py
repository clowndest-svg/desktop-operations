"""Cross-thread delivery of UI state to the webview.

Why a pump and not a direct call
--------------------------------
Two hard constraints meet here:

* ``Window.evaluate_js`` blocks until the page's JavaScript returns, and pywebview
  serialises calls into its own UI loop. Calling it from the microphone capture
  thread means a slow or wedged page stalls audio capture -- the one thread that
  must never stall, because that is how "it stopped hearing me" happens.
* Voice state changes arrive in bursts (wake, state, reply, state) from several
  threads: the capture loop, the ``jarvis-process`` turn thread, and
  ``jarvis-voice-boot``.

So producers hand snapshots to :meth:`UiEventPump.submit`, which does nothing but
store one reference, and a single daemon thread drains them into the sink.

The slot keeps only the newest value. Nothing in the HUD is a queue to be worked
through -- it is a *state* to be current, so dropping intermediate snapshots is the
point rather than a loss. The one thing that must never be lost is the newest one.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable

from jarvis.ui.state_bridge import UiState

logger = logging.getLogger("jarvis.ui.pump")

DEFAULT_DEBOUNCE_SECONDS = 0.05
"""Long enough to coalesce a burst, short enough that the indicator feels instant."""


class UiEventPump:
    """Latest-wins coalescing delivery of snapshots to a single sink."""

    def __init__(
        self,
        sink: Callable[[UiState], None],
        *,
        debounce: float = DEFAULT_DEBOUNCE_SECONDS,
        name: str = "jarvis-ui-pump",
        max_errors_before_backoff: int = 3,
        backoff_seconds: float = 1.0,
    ) -> None:
        """Create the pump. Nothing runs until :meth:`start`.

        Args:
            sink: Called with the newest snapshot, on the pump thread only. It may
                raise: the pump logs and keeps going, because a page that fails to
                render must not take the desktop process down with it.
            debounce: Quiet period before a snapshot is delivered.
            max_errors_before_backoff: Consecutive sink failures tolerated before
                the loop slows down. A sink that always throws -- ``evaluate_js``
                against a killed window is the realistic case -- would otherwise
                spin at full CPU for the rest of the process' life.
            backoff_seconds: The slower cadence it settles into after that.
        """
        self._sink = sink
        self._debounce = max(0.0, debounce)
        self._name = name
        self._error_budget = max(1, max_errors_before_backoff)
        self._backoff = max(0.0, backoff_seconds)

        self._condition = threading.Condition()
        self._pending: UiState | None = None
        self._running = False
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Begin draining. Idempotent, so a restart cannot spawn a second thread."""
        with self._condition:
            if self._running:
                return
            self._running = True
            self._thread = threading.Thread(target=self._loop, name=self._name, daemon=True)
            self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        """Stop draining and join. Idempotent; never blocks a shutdown for long."""
        with self._condition:
            if not self._running:
                return
            self._running = False
            thread, self._thread = self._thread, None
            self._condition.notify_all()
        if thread is not None:
            thread.join(timeout=timeout)
            if thread.is_alive():  # pragma: no cover - a wedged sink
                logger.error("%s did not stop within %.1fs", self._name, timeout)

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    # ------------------------------------------------------------------
    # Producer side
    # ------------------------------------------------------------------

    def submit(self, snapshot: UiState) -> None:
        """Offer a snapshot. Never blocks, never raises, keeps only the newest."""
        with self._condition:
            self._pending = snapshot
            self._condition.notify_all()

    # ------------------------------------------------------------------
    # Delivery thread
    # ------------------------------------------------------------------

    def _loop(self) -> None:
        failures = 0
        while True:
            with self._condition:
                while True:
                    # Pending is checked *before* the stop flag so the last state
                    # change still reaches the page on the way out; a HUD that shows
                    # an older reading during shutdown is a HUD people distrust.
                    if self._pending is not None:
                        break
                    if not self._running:
                        return
                    self._condition.wait(self._debounce)
                snapshot = self._pending
                self._pending = None
            # Delivered outside the lock: the sink is where ``evaluate_js`` blocks,
            # and holding the condition across it would stall every producer thread
            # that just wants to drop a newer snapshot.
            if self._deliver(snapshot):
                failures = 0
                continue
            failures += 1
            if failures >= self._error_budget and self._backoff > 0.0:
                logger.warning(
                    "ui sink failed %d times in a row; slowing the pump to %.1fs",
                    failures,
                    self._backoff,
                )
                time.sleep(self._backoff)

    def _deliver(self, snapshot: UiState) -> bool:
        try:
            self._sink(snapshot)
        except Exception:  # a broken page must not kill the desktop app
            logger.exception("ui sink raised; the HUD may be showing stale state")
            return False
        return True
