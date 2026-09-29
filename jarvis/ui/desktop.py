"""Desktop shell: a pywebview window hosting the Vue HUD.

The page is built by ``frontend/`` into ``jarvis/ui/web`` and loaded from disk,
so there is no local HTTP server, no port to fight over, and nothing reachable
from the network.

The JS bridge is deliberately narrow: the page asks for a reading, Python answers.
Nothing the web layer can call mutates the machine — that stays behind the
confirmation flow in the application layer.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from jarvis.ui.pump import UiEventPump
from jarvis.ui.state_bridge import StateBridge, UiState

if TYPE_CHECKING:  # pragma: no cover - typing only
    from jarvis.app.chat_service import ChatService
    from jarvis.app.disk_service import DiskService
    from jarvis.app.system_service import SystemService
    from jarvis.app.voice_service import VoiceService

logger = logging.getLogger("jarvis.ui.desktop")

WEB_DIR = Path(__file__).resolve().parent / "web"
"""Where the Vite build deposits the bundle that this shell loads.

``package-data`` in ``pyproject.toml`` ships this directory inside the wheel; it
is not in git, so a source checkout has to run ``python scripts/build_desktop.py``
once before ``--desktop`` has anything to show.
"""

BUILD_HINT = (
    "找不到界面构建产物：{path}\n"
    "先构建前端：python scripts/build_desktop.py\n"
    "（或 cd frontend && npm install && npm run build）"
)


def index_path() -> Path:
    """The entry HTML the window loads."""
    return WEB_DIR / "index.html"


def bundle_hint() -> str:
    """Return what is wrong with the bundle, or ``""`` when it is loadable.

    One function so ``run()``, ``scripts/build_desktop.py`` and the packaging test
    all judge "is there a UI to ship" the same way. A stale bundle is worse than no
    bundle: ``index.html`` can exist while the hashed assets it references do not.
    """
    index = index_path()
    if not index.is_file():
        return f"缺少 {index}"
    referenced = _referenced_assets(index)
    missing = [name for name in referenced if not (WEB_DIR / name).is_file()]
    if missing:
        return f"{index} 引用的资源文件缺失：{', '.join(missing)}"
    return ""


_ASSET_ATTRS = ("src", "href")


def _referenced_assets(index: Path) -> tuple[str, ...]:
    """Relative ``assets/...`` references inside the built HTML.

    Parsed as text rather than with an HTML parser: the only thing that matters is
    whether the files Vite pointed at are on disk.
    """
    html = index.read_text(encoding="utf-8", errors="replace")
    found: list[str] = []
    for attribute in _ASSET_ATTRS:
        for raw in re.findall(rf'{attribute}\s*=\s*["\']([^"\']+)["\']', html):
            target = raw[2:] if raw.startswith("./") else raw
            if target.startswith("assets/") and target not in found:
                found.append(target)
    return tuple(found)


class HudBridge:
    """The entire Python surface the page may call.

    Every method returns plain JSON structures and none of them raise: an
    exception thrown out of a ``js_api`` call reaches the page as an opaque
    ``pywebview`` error, which is the worst possible shape for "the microphone
    could not open".
    """

    def __init__(
        self,
        system: SystemService,
        disk: DiskService,
        *,
        voice: VoiceService | None = None,
        chat: ChatService | None = None,
        state: StateBridge | None = None,
    ) -> None:
        self._system = system
        self._disk = disk
        self._voice = voice
        self._chat = chat
        self._state = state

    def snapshot(self) -> dict[str, Any]:
        """One system reading, plus any caveats. Never raises into the page."""
        return self._system.report().to_dict()

    def disk_scan(self) -> dict[str, object]:
        """List deletable junk. Read-only."""
        return self._disk.plan().to_dict()

    def disk_delete(self, items: list[object], confirmed: bool) -> dict[str, object]:
        """Delete the entries a human ticked, as the page received them from
        ``disk_scan``. Entries are objects rather than path strings because one
        entry can stand for a thousand loose files, and the tool has to know that.
        Confirmation is re-checked in the application layer, never trusted here.
        """
        return self._disk.clean(list(items), confirmed=bool(confirmed)).to_dict()

    def voice_status(self) -> dict[str, object]:
        """Whether the microphone is available, and what it is doing about it."""
        if self._voice is None:
            return {"phase": "off", "detail": "本进程未启用语音功能", "keyword": ""}
        return voice_status_dict(self._voice.status)

    def voice_enable(self) -> dict[str, object]:
        """Begin loading the voice stack. Returns immediately.

        Model loading takes tens of seconds and about 3 GB, so it runs on the voice
        service's own boot thread; the page watches ``voice_status`` from here on.
        """
        if self._voice is None:
            return {"phase": "off", "detail": "本进程未启用语音功能", "keyword": ""}
        return voice_status_dict(self._voice.enable())

    def voice_mute(self) -> dict[str, object]:
        """Let go of the microphone. The page can never open it by accident."""
        if self._voice is None:
            return {"phase": "off", "detail": "本进程未启用语音功能", "keyword": ""}
        return voice_status_dict(self._voice.mute())

    def voice_talk(self) -> dict[str, object]:
        """Open one spoken turn without saying the wake word.

        Refused presses come back with a reason rather than an empty success: a
        button that silently does nothing is indistinguishable from a dead bridge,
        and the operator has no way to tell which one they are looking at.
        """
        if self._voice is None:
            return {
                "phase": "off",
                "detail": "本进程未启用语音功能",
                "keyword": "",
            }
        return voice_status_dict(self._voice.talk())

    def chat_ask(self, text: str) -> dict[str, object]:
        """Answer one typed question. Blocks until the model replies."""
        if self._chat is None:
            return {"question": text, "answer": "", "error": "对话服务未启用"}
        reply = self._chat.ask(text)
        if self._state is not None and not reply.error:
            # Typed and spoken turns share one transcript. Two logs in a chat panel
            # leaves the reader working out which one is the conversation.
            self._state.add_turn("user", reply.question)
            self._state.add_turn("assistant", reply.answer)
        return reply.to_dict()

    def chat_clear(self) -> dict[str, object]:
        """Forget the conversation and empty the transcript the page renders."""
        if self._chat is not None:
            self._chat.clear_history()
        if self._state is not None:
            self._state.clear_history()
        return {"ok": True}

    def state_snapshot(self) -> dict[str, object]:
        """The live voice/turn state, for a page that joined after the events flew.

        Push alone is not enough: the page loads asynchronously, so the state can
        have moved on before the first render. Pull on mount, then trust pushes.
        """
        if self._state is None:
            return UiState.idle().to_dict()
        return self._state.snapshot().to_dict()

    def app_info(self) -> dict[str, Any]:
        from jarvis import __version__

        return {"name": "小夜", "version": __version__, "engine": "pywebview"}


def voice_status_dict(status: object) -> dict[str, object]:
    phase = getattr(status, "phase", None)
    return {
        "phase": getattr(phase, "value", "off"),
        "detail": str(getattr(status, "detail", "") or ""),
        "keyword": str(getattr(status, "keyword", "") or ""),
    }


_STATE_SCRIPT = "window.__jarvisState && window.__jarvisState({payload});"


def push_state(window: object, snapshot: UiState) -> None:
    """Hand one snapshot to the page.

    Runs on the pump thread, never on a capture thread: ``evaluate_js`` blocks
    until the browser answers, and a microphone loop that stops reading samples
    drops audio the user already spoke.
    """
    payload = json.dumps(snapshot.to_dict(), ensure_ascii=False)
    evaluate = getattr(window, "evaluate_js", None)
    if not callable(evaluate):  # pragma: no cover - window gone mid-shutdown
        return
    evaluate(_STATE_SCRIPT.format(payload=payload))


def run(
    system: SystemService,
    disk: DiskService,
    *,
    voice: VoiceService | None = None,
    chat: ChatService | None = None,
    bridge_sink: Callable[[UiState], None] | None = None,
    width: int = 1280,
    height: int = 800,
    debug: bool = False,
) -> int:
    """Open the HUD window and block until it closes.

    Args:
        bridge_sink: Where live state goes. Defaults to pushing into the page;
            tests pass a collector so the whole wiring can be exercised without a
            browser.
    """
    index = index_path()
    problem = bundle_hint()
    if problem:
        logger.error("desktop bundle unusable: %s", problem)
        print(BUILD_HINT.format(path=index), flush=True)
        return 1

    import webview

    state = StateBridge()
    window_box: list[Any] = []
    sink = bridge_sink or (lambda snapshot: _deliver(window_box, snapshot))
    pump = UiEventPump(sink)
    unsubscribe = state.subscribe(pump.submit)
    if voice is not None:
        voice.subscribe(state.push_event)

    # create_window registers the window with pywebview's own loop; the handle is
    # kept only so the pump can push state into it once it exists.
    window = webview.create_window(
        title="小夜",
        url=index.as_uri(),
        js_api=HudBridge(system, disk, voice=voice, chat=chat, state=state),
        width=width,
        height=height,
        min_size=(900, 600),
        background_color="#04070d",
        text_select=True,
    )
    window_box.append(window)
    pump.start()
    logger.info("opening desktop window (%sx%s)", width, height)
    try:
        # gui="edgechromium" pins the WebView2 backend instead of letting pywebview
        # fall back to Qt/GTK, which are not installed here and would fail obscurely.
        webview.start(debug=debug, gui="edgechromium")
    finally:
        # Closing the window is the only shutdown path there is: no tray, so the
        # process must not outlive it holding a microphone.
        unsubscribe()
        pump.stop()
        if voice is not None:
            voice.stop()
    return 0


def _deliver(window_box: list[Any], snapshot: UiState) -> None:
    """Push one snapshot into the first window that exists yet."""
    for window in window_box:
        push_state(window, snapshot)
        return
    logger.debug("no desktop window yet; dropping state snapshot")
