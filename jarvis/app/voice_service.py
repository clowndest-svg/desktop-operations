"""Voice service: the desktop HUD's one owner of the microphone.

Why this exists as its own component
------------------------------------
In a console run ``jarvis/__main__`` wires the whole chain up front and blocks.
The desktop shell cannot do that: ``app.start()`` runs on the way to opening a
window, and loading SenseVoice takes tens of seconds and about 3 GB of memory.
So enabling voice has to be an *action* the user takes from the page, with a
status the page can render while it happens.

Four rules keep this honest:

1. **Nothing heavy in** :meth:`start`. The component exists; the microphone does
   not. Models load on a boot thread only after :meth:`enable`.
2. **One owner of the capture device.** The desktop composition root registers
   this service and deliberately *not* ``WakeWordService`` / ``VadService``, so a
   second capture loop cannot exist alongside it. Mic contention is prevented by
   the shape of the app, not by a config value somebody can set twice.
3. **The status is authoritative, and self-checking.** :attr:`status` is derived
   from the live pipeline where one exists, so a capture thread that died leaves
   the indicator saying so instead of glowing "listening" forever.
4. **A click is the consent, and it is remembered.** :meth:`arm_if_remembered`
   re-opens the microphone on a later launch when an earlier :meth:`enable` said
   so -- which is what makes "say the wake word, touch nothing" possible -- and
   :meth:`mute` forgets it. The *first* choice can still only come from a human
   pressing the button, so no launch ever starts listening on its own initiative.

:class:`~jarvis.core.events.VoicePhase` -- not
:class:`jarvis.orchestration.voice_pipeline.PipelineState` -- is what this
service reports: "can I talk to it" rather than "is it listening this instant".
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Iterable
from typing import Protocol

from jarvis.app.preferences import VOICE_AUTO_ARM, Preferences
from jarvis.core.events import PipelineEvent, VoicePhase, VoiceStatus

logger = logging.getLogger("jarvis.app.voice_service")

BootThreadName = "jarvis-voice-boot"

_NOT_RUNNING = {
    VoicePhase.LOADING: "语音模型还在加载，等它变成「待唤醒」再按",
    VoicePhase.MUTED: "麦克风已释放，先点「启用语音」",
    VoicePhase.FAILED: "语音不可用，状态条上有原因",
    VoicePhase.OFF: "先点「启用语音」，加载约 30 秒",
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

    def speak_text(self, text: str) -> bool:
        """Read one already-shown sentence aloud. ``False`` if it cannot."""
        ...

    def stop_speaking(self) -> bool:
        """Cut off a read-aloud in progress. ``False`` when there is none."""
        ...

    def start(self) -> None: ...

    def stop(self) -> None: ...

    def stop_listening(self) -> None:
        """Release the microphone and nothing else.

        Separate from :meth:`stop` because the two are not the same request, and the
        one the operator makes most often is this one: "stop hearing me". ``stop``
        also closes the speaker, which is why muting used to silence her read-aloud as
        well.
        """
        ...


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
        preferences: Preferences | None = None,
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
            preferences: Where the operator's own choice to be listened to is kept.
                Without it the feature still works, it just costs one click per
                launch -- so a test or a console run can leave this out.
            join_timeout: How long :meth:`stop` waits for a boot in progress.
        """
        self._build = loop_builder
        self._permission = permission
        self._keywords_provider = keywords
        self._preferences = preferences
        self._join_timeout = join_timeout
        self._on_event: Callable[[PipelineEvent], None] | None = None
        """UI fan-out sink, installed by the composition root via :meth:`subscribe`."""

        self._lock = threading.Lock()
        self._phase = VoicePhase.OFF
        self._detail = ""
        self._loop: VoiceLoop | None = None
        self._boot: threading.Thread | None = None
        self._stopping = threading.Event()
        self._generation = 0

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

        Every press that actually starts a load takes a new *generation*, and a
        boot thread may only write status while its own is still current. Without
        that, "mute and immediately re-enable" -- a press sequence a nervous
        operator performs literally -- lets the thread that is already winding down
        own the answer: the new press reports 上一次的加载仍在进行, the old thread
        has nothing left to report, and the light sits on 「加载中」 with nothing
        loading until the process is restarted.
        """
        to_start: threading.Thread | None = None
        resuming: VoiceLoop | None = None
        with self._lock:
            if not self._allowed():
                self._phase = VoicePhase.FAILED
                self._detail = "配置未开启 orchestration.enabled"
            elif self._phase in (VoicePhase.LOADING, VoicePhase.RUNNING):
                pass
            elif self._loop is not None:
                # She is still loaded -- 聆听 was off, or a reopen failed -- so opening
                # the microphone is one call rather than another model load. The phase is
                # set optimistically and walked back below if the source will not open.
                self._phase = VoicePhase.RUNNING
                self._detail = ""
                resuming = self._loop
            else:
                self._phase = VoicePhase.LOADING
                self._detail = (
                    "正在加载语音模型，本机实测约 30 秒（首次运行还要下载约 900 MB 权重）"
                )
                self._generation += 1
                self._stopping.clear()
                to_start = threading.Thread(
                    target=self._boot_voice,
                    args=(self._generation,),
                    name=BootThreadName,
                )
                self._boot = to_start
            status = VoiceStatus(self._phase, self._detail, self._keyword_text())
        if resuming is not None:
            status = self._reopen_microphone(resuming, status)
        self._notify(status)
        if status.phase in (VoicePhase.LOADING, VoicePhase.RUNNING):
            # The press is the consent; recording it is what saves the next one.
            self._remember_auto_arm(True)
        if to_start is not None:
            to_start.start()
        return status

    def _reopen_microphone(self, loop: VoiceLoop, status: VoiceStatus) -> VoiceStatus:
        """Put the microphone back on a stack that is already loaded."""
        try:
            loop.start()
        except Exception as exc:
            logger.exception("the microphone could not be reopened")
            with self._lock:
                self._phase = VoicePhase.FAILED
                self._detail = f"麦克风没能重新打开：{type(exc).__name__}: {exc}"
                return VoiceStatus(self._phase, self._detail, self._keyword_text())
        logger.info("microphone reopened without reloading the models")
        return status

    def arm_if_remembered(self) -> VoiceStatus | None:
        """Re-open the microphone when an earlier press said to do this.

        Returns ``None`` when there was nothing to do, which is deliberately not
        the same answer as a refusal: the caller is the desktop shell on its way to
        opening a window, and a launch that quietly stays off must not be reported
        as a failure. The permission gate is checked *here* rather than left to
        :meth:`enable`, because :meth:`enable` answers a denied press with a
        visible ``failed`` -- correct for a button, wrong for a launch nobody asked
        to be rejected.
        """
        store = self._preferences
        if store is None or not store.flag(VOICE_AUTO_ARM):
            return None
        if not self._allowed():
            logger.warning(
                "「启用语音」 was remembered, but this launch is not permitted to open the "
                "microphone (orchestration.enabled is off and --voice was not passed); "
                "staying silent instead"
            )
            return None
        logger.info("re-arming the microphone from the remembered 「启用语音」 choice")
        return self.enable()

    def _remember_auto_arm(self, value: bool) -> None:
        store = self._preferences
        if store is not None:
            store.set_flag(VOICE_AUTO_ARM, value)

    def mute(self) -> VoiceStatus:
        """Let go of the microphone. Everything else she has stays loaded and working.

        This used to tear the whole stack down, which quietly made the switch mean "turn
        her voice off as well": the read-aloud lives in the same object as the capture
        thread, so closing the microphone closed the speaker with it -- and turning her
        ears back on cost another 30-second model load. Now the microphone is the only
        thing released, so a typed answer still gets spoken while she cannot hear.

        The models stay in memory while she is muted. That is the trade being made, and
        it is the one the operator asked for: the alternative was a switch that is
        expensive to touch.
        """
        # Releasing the microphone is also a choice, and it outranks the old one:
        # nobody should have to hunt for the switch that puts it back to sleep.
        self._remember_auto_arm(False)
        with self._lock:
            loop = self._loop
            # A load still in flight has no business handing the microphone to a window
            # whose operator just asked for silence, so its press is retired either way.
            self._generation += 1
        if loop is None:
            self._teardown(reason="麦克风已释放，可重新开启")
        else:
            try:
                loop.stop_listening()
            except Exception:  # pragma: no cover - a source that will not close
                logger.exception("the microphone could not be released cleanly")
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
    # Read-aloud
    # ------------------------------------------------------------------

    def speak_text(self, text: str) -> bool:
        """Speak a sentence the page already shows; ``False`` when nothing can say it.

        No status is emitted here, and that is deliberate: a read-aloud is not a
        change in *availability*, and flipping the phase to answer "did that
        paragraph get a voice" would drag the one indicator the operator trusts
        across a second meaning. What the page needs to know -- whether samples are
        arriving -- it learns from the audio channel itself.

        The gate is "a stack exists", not "the microphone is open". Those were the same
        condition until 聆听 turned out to be a switch the operator reads as *hearing*:
        with the microphone shut she has nothing to say out loud of her own, but a typed
        answer is still hers to read.

        Refusing is always safe. The caller has already put the text on screen, so
        the worst case for a typed answer is that it stays written.
        """
        sentence = text.strip()
        if not sentence:
            return False
        with self._lock:
            phase, loop = self._phase, self._loop
        if loop is None:
            logger.debug("read-aloud refused (voice phase=%s)", phase.value)
            return False
        try:
            return bool(loop.speak_text(sentence))
        except Exception:
            logger.exception("read-aloud failed unexpectedly")
            return False

    def stop_speaking(self) -> bool:
        """Cut off a read-aloud. Returns whether anything was actually talking."""
        with self._lock:
            loop = self._loop
        if loop is None:
            return False
        try:
            return bool(loop.stop_speaking())
        except Exception:  # pragma: no cover - teardown path
            logger.exception("could not stop the read-aloud")
            return False

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _boot_voice(self, generation: int) -> None:
        """Load and start the stack. Runs on its own thread, never the UI's.

        Every path out of here must leave a status behind. A swallowed exception
        would strand the HUD on "loading" forever, which is the single worst way
        for this feature to fail in front of a customer.

        ``generation`` is the press this thread answers for. A thread whose press
        has been overtaken writes nothing and releases whatever it built, so a
        superseded load cannot hand the microphone to a window that already moved
        on -- and cannot leave it holding a stack nobody asked for.
        """
        try:
            loop = self._build(self.forward_event)
        except Exception as exc:  # any failure must still produce a status
            logger.exception("voice stack failed to load")
            self._report(generation, VoicePhase.FAILED, f"{type(exc).__name__}: {exc}")
            return
        try:
            loop.start()
        except Exception as exc:
            logger.exception("voice stack failed to start")
            self._quietly_stop(loop)
            self._report(generation, VoicePhase.FAILED, f"{type(exc).__name__}: {exc}")
            return
        with self._lock:
            superseded = self._stopping.is_set() or self._generation != generation
            if not superseded:
                self._loop = loop
        if superseded:
            self._quietly_stop(loop)
            if self._stopping.is_set():
                self._report(generation, VoicePhase.OFF, "语音已取消")
            else:
                logger.info("a newer press owns the voice now; this stack steps aside")
            return
        logger.info("voice stack running")
        self._report(generation, VoicePhase.RUNNING, "")

    def _teardown(self, *, reason: str) -> None:
        with self._lock:
            loop, self._loop = self._loop, None
            # Retiring the stack also retires the press it was built for: a load
            # still in flight from before this point has no business reporting a
            # status into a service that has already been muted or stopped.
            self._generation += 1
        if loop is not None:
            self._quietly_stop(loop)
            logger.info("voice stack stopped (%s)", reason)

    @staticmethod
    def _quietly_stop(loop: VoiceLoop) -> None:
        try:
            loop.stop()
        except Exception:  # pragma: no cover - teardown must not raise into shutdown
            logger.exception("voice stack stopped reporting an error")

    def _report(self, generation: int, phase: VoicePhase, detail: str) -> None:
        """Publish a status a boot thread reached, unless it no longer speaks here.

        The guard is the whole reason this exists rather than a plain ``_set``: the
        thread that loses a race must be the one to stay quiet, or the light ends up
        describing a stack that was already thrown away.
        """
        with self._lock:
            if self._generation != generation:
                logger.debug(
                    "dropping %s from a superseded voice boot",
                    phase.value,
                )
                return
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
