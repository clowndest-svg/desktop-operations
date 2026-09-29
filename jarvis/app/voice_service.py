"""Voice service: the desktop HUD's one owner of the microphone.

Why this exists as its own component
------------------------------------
In a console run ``jarvis/__main__`` wires the whole chain up front and blocks.
The desktop shell cannot do that: ``app.start()`` runs on the way to opening a
window, and loading SenseVoice takes tens of seconds and about 3 GB of memory.
So enabling voice has to be an *action* the user takes from the page, with a
status the page can render while it happens.

Three rules keep this honest:

1. **Nothing heavy in** :meth:`start`. The component exists; the microphone does
   not. Models load on a boot thread only after :meth:`enable`.
2. **One owner of the capture device.** The desktop composition root registers
   this service and deliberately *not* ``WakeWordService`` / ``VadService``, so a
   second capture loop cannot exist alongside it. Mic contention is prevented by
   the shape of the app, not by a config value somebody can set twice.
3. **The status is authoritative, and self-checking.** :attr:`status` is derived
   from the live pipeline where one exists, so a capture thread that died leaves
   the indicator saying so instead of glowing "listening" forever.

:class:`~jarvis.core.events.VoicePhase` -- not
:class:`jarvis.orchestration.voice_pipeline.PipelineState` -- is what this
service reports: "can I talk to it" rather than "is it listening this instant".
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Iterable
from typing import Protocol

from jarvis.core.events import PipelineEvent, VoicePhase, VoiceStatus

logger = logging.getLogger("jarvis.app.voice_service")

BootThreadName = "jarvis-voice-boot"

_NOT_RUNNING = {
    VoicePhase.LOADING: "语音模型还在加载，等它变成「待唤醒」再按",
    VoicePhase.MUTED: "麦克风已释放，先点「启用语音」",
    VoicePhase.FAILED: "语音不可用，状态条上有原因",
    VoicePhase.OFF: "先点「启用语音」，加载约 2 分钟",
}
"""Why 「按一下说」 refused, per phase. A button that does nothing silently is
indistinguishable from a broken one, and the customer cannot tell which."""


class VoiceLoop(Protocol):
    """The running voice stack, as far as this service cares.

    Structural on purpose: the real implementation owns ASR, TTS and the pipeline,
    and the tests own nothing at all. Neither has to import anything from here.
    """

    @property
    def listening(self) -> bool:
        """Whether the capture loop is alive."""
        ...

    def speak_now(self) -> bool:
        """Open one turn without the wake word. ``False`` if it is busy or down."""
        ...

    def start(self) -> None: ...

    def stop(self) -> None: ...


# A plain function alias rather than a callable Protocol: mypy will not accept an
# ordinary function where a Protocol class with ``__call__`` is expected, and the
# builder genuinely is just a function.
LoopBuilder = Callable[[Callable[[PipelineEvent], None]], VoiceLoop]


class VoiceService:
    """Lifecycle component owning the voice feature's availability."""

    name = "voice"

    def __init__(
        self,
        loop_builder: LoopBuilder,
        *,
        permission: Callable[[], bool] | None = None,
        keywords: Callable[[], Iterable[str]] | None = None,
        join_timeout: float = 10.0,
    ) -> None:
        """Create the service.

        Args:
            loop_builder: Loads ASR/TTS/agent engines and returns a voice stack.
                Called on a background thread, never from :meth:`start`.
            permission: The configuration gate, read when the page asks rather than
                when the service is built -- a component is registered before
                ``ConfigService`` has loaded anything, and reading configuration
                early raises. When it says no, :meth:`enable` refuses instead of
                pretending to work: a demo flag that silently did nothing is
                indistinguishable from a broken microphone.
            keywords: Wake words to display once running, also read lazily. Strings
                only: this layer must not import the L1 wake-word types the UI is
                also forbidden to reach.
            join_timeout: How long :meth:`stop` waits for a boot in progress.
        """
        self._build = loop_builder
        self._permission = permission
        self._keywords_provider = keywords
        self._join_timeout = join_timeout
        self._on_event: Callable[[PipelineEvent], None] | None = None
        """UI fan-out sink, installed by the composition root via :meth:`subscribe`."""

        self._lock = threading.Lock()
        self._phase = VoicePhase.OFF
        self._detail = ""
        self._loop: VoiceLoop | None = None
        self._boot: threading.Thread | None = None
        self._stopping = threading.Event()

    # ------------------------------------------------------------------
    # Component lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Register as available. Loads nothing, opens nothing."""
        with self._lock:
            self._phase = VoicePhase.OFF
            self._detail = ""
        logger.info("voice service ready (microphone closed until enabled)")

    def stop(self) -> None:
        """Release the microphone and wait for any boot in progress. Idempotent."""
        self._stopping.set()
        with self._lock:
            boot, self._boot = self._boot, None
        if boot is not None and boot.is_alive():
            boot.join(timeout=self._join_timeout)
            if boot.is_alive():  # pragma: no cover - only a slow model load gets here
                logger.error("voice boot thread still alive after %.1fs", self._join_timeout)
        self._teardown(reason="语音已关闭")
        with self._lock:
            self._phase = VoicePhase.OFF
            self._detail = ""
        # ``_stopping`` deliberately stays set here: a boot thread that finishes
        # after this point must still tear its own stack down, or a window that has
        # already closed would leave the microphone open. :meth:`enable` clears it.

    # ------------------------------------------------------------------
    # Observable state
    # ------------------------------------------------------------------

    @property
    def status(self) -> VoiceStatus:
        """Current availability, cross-checked against the live pipeline.

        A phase that says RUNNING while the capture thread is gone would be a lie
        on the one screen the operator watches, so the contradiction is reported as
        a failure the moment it is observed.
        """
        with self._lock:
            phase, detail = self._phase, self._detail
            loop = self._loop
        if phase is VoicePhase.RUNNING and loop is not None and not loop.listening:
            return VoiceStatus(
                phase=VoicePhase.FAILED,
                detail="拾音线程已退出（可能麦克风被其他程序占用）",
                keyword=self._keyword_text(),
            )
        return VoiceStatus(phase=phase, detail=detail, keyword=self._keyword_text())

    def _keyword_text(self) -> str:
        provider = self._keywords_provider
        if provider is None:
            return ""
        try:
            return " / ".join(tuple(provider())[:3])
        except Exception:  # a label must never break a status read
            logger.exception("could not read the configured wake words")
            return ""

    def _allowed(self) -> bool:
        provider = self._permission
        if provider is None:
            return True
        try:
            return bool(provider())
        except Exception:  # cannot read the gate == do not open the microphone
            logger.exception("voice permission gate failed; treating as denied")
            return False

    # ------------------------------------------------------------------
    # Actions the page may take
    # ------------------------------------------------------------------

    def enable(self) -> VoiceStatus:
        """Start loading the voice stack, or refuse with the reason.

        Returns the resulting status so the caller can render the answer without a
        second round-trip. The decision is taken under the lock and the thread is
        started after releasing it: a thread that begins reporting status before the
        caller has let go of the lock would deadlock against itself.
        """
        to_start: threading.Thread | None = None
        with self._lock:
            if not self._allowed():
                self._phase = VoicePhase.FAILED
                self._detail = "配置未开启 orchestration.enabled"
            elif self._phase in (VoicePhase.LOADING, VoicePhase.RUNNING):
                pass
            elif self._boot is not None and self._boot.is_alive():
                self._phase = VoicePhase.LOADING
                self._detail = "上一次的加载仍在进行"
            else:
                self._phase = VoicePhase.LOADING
                self._detail = "正在加载语音模型，本机实测约 2 分钟（首次运行还需下载权重）"
                self._stopping.clear()
                to_start = threading.Thread(target=self._boot_voice, name=BootThreadName)
                self._boot = to_start
            status = VoiceStatus(self._phase, self._detail, self._keyword_text())
        self._notify(status)
        if to_start is not None:
            to_start.start()
        return status

    def mute(self) -> VoiceStatus:
        """Let go of the microphone, keeping the process and the panel intact."""
        self._teardown(reason="麦克风已释放，可重新开启")
        with self._lock:
            self._phase = VoicePhase.MUTED
            self._detail = ""
            status = VoiceStatus(self._phase, self._detail, self._keyword_text())
        self._notify(status)
        return status

    def talk(self) -> VoiceStatus:
        """Open one spoken turn without the wake word.

        Nothing here is stored: the phase does not change, and the pipeline's own
        ``state`` event is what tells the page it is listening. The returned detail
        is a one-shot answer to "why didn't that button do anything", which is the
        only feedback a press that was refused would otherwise get.
        """
        with self._lock:
            phase, detail, loop = self._phase, self._detail, self._loop
        keyword = self._keyword_text()
        if phase is not VoicePhase.RUNNING or loop is None:
            reason = detail or _NOT_RUNNING.get(phase, "先点「启用语音」")
            return VoiceStatus(phase, reason, keyword)
        if not loop.speak_now():
            return VoiceStatus(phase, "它正在听或正在回答，等这一轮结束再按", keyword)
        return VoiceStatus(phase, "在听，说完停一下即可", keyword)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _boot_voice(self) -> None:
        """Load and start the stack. Runs on its own thread, never the UI's.

        Every path out of here must leave a status behind. A swallowed exception
        would strand the HUD on "loading" forever, which is the single worst way
        for this feature to fail in front of a customer.
        """
        try:
            loop = self._build(self.forward_event)
        except Exception as exc:  # any failure must still produce a status
            logger.exception("voice stack failed to load")
            self._set(VoicePhase.FAILED, f"{type(exc).__name__}: {exc}")
            return
        try:
            loop.start()
        except Exception as exc:
            logger.exception("voice stack failed to start")
            self._quietly_stop(loop)
            self._set(VoicePhase.FAILED, f"{type(exc).__name__}: {exc}")
            return
        if self._stopping.is_set():
            # ``stop()`` ran while the models were loading; starting the loop after
            # that would hand the microphone back to a window on its way out.
            self._quietly_stop(loop)
            self._set(VoicePhase.OFF, "语音已取消")
            return
        with self._lock:
            self._loop = loop
        logger.info("voice stack running")
        self._set(VoicePhase.RUNNING, "")

    def _teardown(self, *, reason: str) -> None:
        with self._lock:
            loop, self._loop = self._loop, None
        if loop is not None:
            self._quietly_stop(loop)
            logger.info("voice stack stopped (%s)", reason)

    @staticmethod
    def _quietly_stop(loop: VoiceLoop) -> None:
        try:
            loop.stop()
        except Exception:  # pragma: no cover - teardown must not raise into shutdown
            logger.exception("voice stack stopped reporting an error")

    def _set(self, phase: VoicePhase, detail: str) -> None:
        with self._lock:
            self._phase = phase
            self._detail = detail
            status = VoiceStatus(phase, detail, self._keyword_text())
        self._notify(status)

    def forward_event(self, event: PipelineEvent) -> None:
        """The sink handed to the voice stack: every pipeline event, unmodified.

        ``voice_status`` is the one kind this service produces and the stack never
        sees, so keeping the two paths separate stops a phase change from ever
        looking like it came from the microphone loop.
        """
        self._publish(event)

    def _notify(self, status: VoiceStatus) -> None:
        self._publish(status.to_event())

    def _publish(self, event: PipelineEvent) -> None:
        """Send one event to whoever subscribed, never raising into our caller.

        The boot thread is the desktop mode's only way to release the microphone;
        a sink that throws must not take it, or the window, down with it.
        """
        listener = self._on_event
        if listener is None:
            return
        try:
            listener(event)
        except Exception:
            logger.exception("voice event listener raised; ignoring")

    def subscribe(self, callback: Callable[[PipelineEvent], None]) -> None:
        """Receive every pipeline event, including synthesized voice_status ones.

        One subscriber by design: the desktop composition root is the fan-out point
        that also feeds the console logger and the UI bridge.
        """
        self._on_event = callback
