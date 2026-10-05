"""Desktop shell: a pywebview window hosting the Vue HUD.

The page is built by ``frontend/`` into ``jarvis/ui/web`` and served to the
window over loopback HTTP by :class:`jarvis.ui.static_server.BundleServer`.

It used to be loaded from disk with ``index.as_uri()``, and that cannot work:
Vite emits an ES module, the HTML spec requires module scripts to be fetched with
CORS, and the origin of a ``file://`` page is ``null`` — so the browser blocks the
script and the window shows an empty near-black page with no error on the Python
side. See :mod:`jarvis.ui.static_server` for the full reasoning and the security
posture of the server.

The JS bridge is deliberately narrow: the page asks for a reading, Python answers.
Nothing the web layer can call mutates the machine — that stays behind the
confirmation flow in the application layer.
"""

from __future__ import annotations

import base64
import binascii
import contextlib
import json
import logging
import os
import re
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from jarvis.app import voice_cloud
from jarvis.app.collaboration import MODE_BOSS, MODE_TABLE, MODE_VOTE
from jarvis.app.endpoint_catalog import list_endpoint_models
from jarvis.app.mobile_pairing import PairingVault
from jarvis.app.model_probe import ModelProber
from jarvis.app.preferences import PET_ENABLED, WINDOW_RECT
from jarvis.app.turns import (
    KIND_CHAT,
    KIND_ROUNDTABLE,
    STATE_CANCELLED,
    STATE_DONE,
    STATE_RUNNING,
)
from jarvis.app.voice_library import VoiceLibraryError
from jarvis.config import MobileSection
from jarvis.core.events import VoicePhase
from jarvis.ui import autostart
from jarvis.ui.compositor import (
    BUBBLE_ADD,
    BUBBLE_STOP,
    DEFAULT_BUBBLE,
    BubblePalette,
    bubble_palette,
)
from jarvis.ui.hotkeys import Hotkeys, default_bindings
from jarvis.ui.instance import InstanceGate
from jarvis.ui.lifecycle import ShellLifecycle
from jarvis.ui.pet import PetController
from jarvis.ui.pump import UiEventPump
from jarvis.ui.state_bridge import StateBridge, UiState, UiVoiceState
from jarvis.ui.static_server import BundleServer
from jarvis.ui.tray import TrayIcon

if TYPE_CHECKING:  # pragma: no cover - typing only
    from jarvis.app.announcer import Announcer
    from jarvis.app.chat_service import ChatService
    from jarvis.app.command_access import CommandAccess
    from jarvis.app.computer_access import ComputerAccess
    from jarvis.app.disk_service import DiskService
    from jarvis.app.preferences import Preferences
    from jarvis.app.process_service import ProcessService
    from jarvis.app.reminder_service import ReminderService
    from jarvis.app.settings_service import SettingsService
    from jarvis.app.system_service import SystemService
    from jarvis.app.usage_service import UsageService
    from jarvis.app.voice_call import VoiceCall
    from jarvis.app.voice_library import VoiceLibrary
    from jarvis.app.voice_picker import VoicePicker
    from jarvis.app.voice_service import VoiceService
    from jarvis.ui.audio_bridge import AudioPusher
    from jarvis.ui.lan_server import MobileGateway

# memory and knowledge are L2/L3 packages and rule 6 says the web layer may
# not import them -- so the bridge names only the calls it actually makes. Same
# trick as ToolLedger above, same reason: the alternative is relaxing a rule that
# exists to keep this window from quietly becoming the application.

logger = logging.getLogger("jarvis.ui.desktop")


class _Dictifiable(Protocol):
    """Something the bridge can hand to the page without knowing its name."""

    def to_dict(self) -> dict[str, object]: ...


class ToolLedger(Protocol):
    """The one thing the bridge reads off the tool service: its recent calls.

    Declared structurally rather than imported. ``ui`` is not allowed to depend on
    ``tools`` -- ``tests/test_architecture_layers.py`` says so and is right -- and
    this is the same seam ``tools/builtins/computer_tools.py`` uses to name a
    desktop it is not permitted to import.
    """

    def recent(self) -> tuple[_Dictifiable, ...]: ...


class MemorySurface(Protocol):
    """What the bridge reads and deletes in the assistant's own memory.

    Structural for the same reason as :class:`ToolLedger`: ``memory`` is L2 and this
    is L5. Only three calls exist here because only three are made -- list, forget
    one, forget everything -- and the panel that shows them must not need to change
    when the memory service grows a fourth.
    """

    def list_memories(self) -> Sequence[Any]: ...

    def forget(self, memory_id: int) -> bool: ...

    def forget_scope(self, scope: Any = ...) -> int: ...


class KnowledgeSurface(Protocol):
    """The knowledge base, as far as the window is allowed to see it.

    ``ingest`` takes a path string because that is what the page has; the service
    decides what it can actually read.
    """

    def documents(self) -> Sequence[Any]: ...

    def stats(self) -> dict[str, object]: ...

    def ingest(self, path: Any, *, force: bool = ...) -> Any: ...

    def forget(self, doc_id: str) -> bool: ...

    def retrieve(
        self, query: str, *, top_k: int = ..., min_score: float = ...
    ) -> Sequence[Any]: ...


class StatsSurface(Protocol):
    """Anything that can describe itself with JSON-ready counters.

    ``SchedulerService.stats`` and ``WorkflowService.stats`` both carry the docstring
    "for the HUD's automation panel". That panel did not exist, so for two rounds the
    two engines have been running with counters nobody read. This is the seam that
    finally reads them.
    """

    def stats(self) -> dict[str, object]: ...


class JobSurface(Protocol):
    """The scheduler, as far as the automation tab needs it.

    Structural, like the other surfaces here: the window is L5 and the scheduler is
    L3, so the bridge names what it calls instead of importing the class.
    """

    def list_jobs(self) -> Sequence[Any]: ...

    def next_run_time(self, job_id: str) -> str: ...

    def history(self, job_id: str | None = ..., *, limit: int = ...) -> Sequence[Any]: ...

    def set_enabled(self, job_id: str, enabled: bool) -> bool: ...

    def remove_job(self, job_id: str) -> bool: ...

    def run_now(self, job_id: str) -> Any: ...


class WorkflowSurface(Protocol):
    """The workflow engine, as far as the automation tab needs it."""

    def definitions(self) -> Sequence[Any]: ...

    def history(self, name: str | None = ..., *, limit: int = ...) -> Sequence[Any]: ...

    def run(self, name: str) -> Any: ...

    def reload(self) -> Sequence[Any]: ...

    def stats(self) -> dict[str, object]: ...


class PlanSurface(Protocol):
    """A plan, as far as the page renders it. ``Plan.to_dict`` is the whole shape."""

    def to_dict(self) -> dict[str, object]: ...


class PlannerSurface(Protocol):
    """The task planner, as far as the automation tab needs it.

    Only ``plan`` is exposed, and only for display. Nothing in this build executes a
    plan -- the capability list says so -- and a bridge method that looked like it
    could would be the interface promising something the backend does not have.
    """

    @property
    def enabled(self) -> bool: ...

    def plan(self, goal: str) -> PlanSurface: ...

    def stats(self) -> dict[str, object]: ...


REMINDER_PREFIX: str = "reminder:"
"""The reminder service's job namespace.

Duplicated from ``jarvis.app.reminder_service`` on purpose: this is a boundary check
(the automation tab must not offer to toggle a row the 「提醒」 tab owns), and a
boundary that silently follows another module's constant is not a boundary.
"""


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

WINDOW_LOAD_TIMEOUT_SECONDS: float = 25.0
"""How long the window may take to load the page before it is called a failure.

Generous: a cold WebView2 start on a busy machine takes several seconds, and a
false alarm on a slow boot is its own kind of noise. The point is only to turn a
*wedged* window into a message.
"""


def blank_window_hint(timeout: float) -> str:
    """What to say when the window opened but the page never finished loading.

    This case used to be silent, and that is the whole reason the function
    exists: the window appears, ``webview.start()`` blocks, the log says
    everything is fine, and the user is looking at a black rectangle with
    nothing anywhere to explain it. The only error lives inside the webview.

    The most common cause by far is that the WebView2 **renderer process** never
    started — Chromium's sandbox needs process and job-object permissions that
    some environments (restricted shells, hardened AV, certain CI runners) do
    not grant. ``--no-sandbox`` is the standard diagnostic for that, and it is
    deliberately *not* set by default: it removes the sandbox from a browser
    engine, which is not a thing to do for everybody in order to help the few.
    """
    return (
        f"窗口已打开，但界面在 {timeout:.0f} 秒内没有加载完成，看到的应该是一片黑。\n"
        "最可能的原因：WebView2 的渲染进程没能启动（Chromium 沙箱在受限环境里拿不到\n"
        "必要的进程权限）。排查顺序：\n"
        "  1. 先看控制台里 pywebview 打印的报错（以 [pywebview] 开头）。\n"
        "  2. 确认 WebView2 运行时已安装：\n"
        "     C:\\Program Files (x86)\\Microsoft\\EdgeWebView\\Application\n"
        "  3. 如果上面都正常，用这个环境变量再启动一次，验证是不是沙箱问题：\n"
        "     set WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS=--no-sandbox\n"
        "     注意：这会去掉渲染进程的沙箱保护，只用于排障，不要长期开着。\n"
        "  4. 界面产物本身是否完好：python scripts/build_desktop.py --check"
    )


def index_path() -> Path:
    """The entry HTML the window loads."""
    return WEB_DIR / "index.html"


def bundle_hint() -> str:
    """Return what is wrong with the bundle, or ``""`` when it is loadable.

    One function so ``run()``, ``scripts/build_desktop.py`` and the packaging test
    all judge "is there a UI to ship" the same way. A stale bundle is worse than no
    bundle: ``index.html`` can exist while the hashed assets it references do not.

    Three failure modes are checked, and the third one is the one that shipped:
    a missing ``index.html``, a reference to an asset that is not on disk, and a
    page the ``file://`` origin will refuse to execute.
    """
    index = index_path()
    if not index.is_file():
        return f"缺少 {index}"
    referenced = _referenced_assets(index)
    missing = [name for name in referenced if not (WEB_DIR / name).is_file()]
    if missing:
        return f"{index} 引用的资源文件缺失：{', '.join(missing)}"
    crossorigin = _crossorigin_attributes(index)
    if crossorigin:
        return (
            f"{index} 带有 crossorigin 属性（{', '.join(crossorigin)}）。"
            "桌面端用 file:// 加载页面，origin 为 null，带 crossorigin 的脚本和样式"
            "会被 CORS 拦掉，窗口只会显示一片黑。"
            "重新构建：python scripts/build_desktop.py（vite.config.ts 里的 "
            "stripCrossorigin 插件会去掉它）"
        )
    return ""


def _crossorigin_attributes(index: Path) -> tuple[str, ...]:
    """The ``crossorigin`` attributes present in the built HTML, if any.

    Vite adds one to every emitted script and link. Over ``file://`` that turns a
    local read into a CORS request from origin ``null``, which no local file can
    satisfy — so every asset is blocked and the window renders empty. The error
    exists only in the webview console, which is why this has to be checked from
    the Python side rather than discovered by looking at the window.
    """
    html = index.read_text(encoding="utf-8", errors="replace")
    return tuple(re.findall(r"<[^>]*\scrossorigin[^>]*>", html))


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


#: What the 协助分析 button asks, above the scan digest it sends.
#:
#: The last line is the one that matters. A model looking at a list of paths will
#: happily say "delete these three" -- and in this product that is not a thing that
#: may be said, because the deletion gate is a human ticking boxes, not advice.
SYNC_TURN_TIMEOUT_SECONDS: float = 600.0
"""How long a caller that waits for an inline answer will wait before saying so.

Longer than any sane model turn, shorter than an infinite hang: the phone's RPC and the
announcer ask a question and read the answer back in the same call, so what they deserve
on a wedged provider is a stated timeout rather than a connection that dies quietly.
"""

MAX_KEPT_RESULTS: int = 64
"""How many finished inline answers to keep for the callers that wait.

Bounded because nothing else removes them: the page does not wait, so a desktop that
spends the day switching tabs would otherwise grow one payload per turn forever.
"""

FOCUS_INPUT_SCRIPT = "window.__jarvisFocusInput && window.__jarvisFocusInput();"
"""The shell's cue for "put the cursor in the chat box".

Guarded with ``&&`` for the same reason the skin cue is: this runs in whichever window is
on the other end of the bridge, and the pet's page has no input box to focus. The panel
installs the function when it mounts, so the shell never has to know where the box is."""


def _accepted(answer: Mapping[str, object]) -> object:
    """Whether a settings answer says it worked.

    ``ok`` is missing on the paths that never set it -- ``apply()`` reports per field --
    so absence means "nothing complained", which is what the callers here need.
    """
    return answer.get("ok", True)


def _seat_label(item: object) -> str:
    """The name a seat is listed under before anyone has read it as a pair.

    Used for the turn's ``participants`` field, which the task list shows verbatim: this
    is the one place that runs before the table has parsed the page's payload, so it has
    to be readable without being right. A nonsense seat reads as its own repr rather than
    as a blank row.
    """
    if isinstance(item, dict):
        provider = str(item.get("provider") or "")
        model = str(item.get("model") or "")
        return f"{provider}/{model}" if provider else model
    return str(item)


def _round_count(value: object) -> int:
    """The rounds the page asked for, as a number. Clamping lives in the table.

    Written as a type test rather than ``int(value)`` because the page sends a JSON number
    and anything else that arrives here is not a round count to convert -- a dict, a
    string of prose -- and silently turning one of those into ``0`` would open a table
    that answers nothing.
    """
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value
    return 0


#: What may be said, because the deletion gate is a human ticking boxes, not advice.
JUNK_BRIEFING = (
    "下面是一次磁盘垃圾扫描的结果（只读扫描，还没有删除任何东西）。请：\n"
    "1) 用一两句话说明每个分类是什么、删掉的后果；\n"
    "2) 挑出看起来异常大的、或者名字看起来不像垃圾的条目，说清楚为什么要人工再看一眼；\n"
    "3) 如果某个目录看不出装了什么，可以用只读的文件工具去列一下再判断。\n"
    "不要建议删除任何具体路径，也不要给出删除命令——这个项目里删除只能由人逐项勾选并确认。\n\n"
)


class HudBridge:
    """The entire Python surface the page may call.

    Every method returns plain JSON structures and none of them raise: an
    exception thrown out of a ``js_api`` call reaches the page as an opaque
    ``pywebview`` error, which is the worst possible shape for "the microphone
    could not open".
    """

    _model_probe: ModelProber | None = None
    """Class-level default: a few contract checks build the bridge with ``__new__`` and set
    only the services they exercise, so every door a method reads has to answer ``None``
    rather than raise on a half-built object.
    """

    def __init__(
        self,
        system: SystemService,
        disk: DiskService,
        *,
        voice: VoiceService | None = None,
        chat: ChatService | None = None,
        state: StateBridge | None = None,
        usage: UsageService | None = None,
        settings: SettingsService | None = None,
        audio: AudioPusher | None = None,
        tools: ToolLedger | None = None,
        voice_picker: VoicePicker | None = None,
        voice_library: VoiceLibrary | None = None,
        voice_call: VoiceCall | None = None,
        voice_sample: Any | None = None,
        computer_access: ComputerAccess | None = None,
        command_access: CommandAccess | None = None,
        process: ProcessService | None = None,
        lifecycle: ShellLifecycle | None = None,
        reminders: ReminderService | None = None,
        announcer: Announcer | None = None,
        memory: MemorySurface | None = None,
        knowledge: KnowledgeSurface | None = None,
        scheduler: JobSurface | None = None,
        workflow: WorkflowSurface | None = None,
        planner: PlannerSurface | None = None,
        model_probe: ModelProber | None = None,
        alerts: Any | None = None,
    ) -> None:
        self._system = system
        self._alerts = alerts
        self._disk = disk
        self._voice = voice
        self._chat = chat
        self._state = state
        self._usage = usage
        self._settings = settings
        self._model_probe = model_probe
        self._audio = audio
        self._tools = tools
        self._voice_picker = voice_picker
        self._voice_library = voice_library
        self._voice_call = voice_call
        self._voice_sample = voice_sample
        """Records a sample of this machine's operator, in the process that owns the mic.

        ``None`` on a run with no microphone path: the page then gets「这个进程没有接录音」
        instead of a record button that captures nothing.
        """
        self._computer_access = computer_access
        self._command_access = command_access
        self._process = process
        self._lifecycle = lifecycle
        self._reminders = reminders
        self._announcer = announcer
        self._memory = memory
        self._knowledge = knowledge
        self._scheduler = scheduler
        self._workflow = workflow
        self._planner = planner
        self._pet: PetController | None = None
        self._remember_pet: Callable[[bool], None] | None = None
        self._bubble_palette: BubblePalette = DEFAULT_BUBBLE
        self._greeter: Any = None
        self._turns: Any = None
        """The table of running turns. Attached by the composition root, not built here:
        the round table has to submit to the same table a typed question does, or the
        panel would need two notions of "what is running"."""
        self._results: dict[str, dict[str, object]] = {}
        self._result_lock = threading.Lock()
        """Guards :attr:`_results`, which the turn's own thread writes and the caller
        that waited reads. Two threads, one dict, no shared answer."""
        """Finished turns' reply payloads, kept for the callers that still wait.

        The page no longer waits, but the phone's RPC and the announcer do: they ask and
        expect the answer in the same call. One engine, two front doors, and this is the
        part that hands the result to the door that waited.
        """
        self._mobile: MobileGateway | None = None
        self._window: Any = None
        self._geometry: _GeometryKeeper | None = None

    def attach_window(self, window: Any, geometry: _GeometryKeeper | None = None) -> None:
        """Hand the bridge the window it is living in.

        Set after construction rather than passed to ``__init__``: the window does not
        exist until the bridge does, and the tests that build a bridge to check the
        read-only surface should not have to invent one.
        """
        self._window = window
        self._geometry = geometry

    def attach_lifecycle(self, lifecycle: ShellLifecycle | None) -> None:
        self._lifecycle = lifecycle

    def attach_pet(
        self, pet: PetController | None, remember: Callable[[bool], None] | None = None
    ) -> None:
        """The pet window, and where to store the operator's choice about it.

        Attached rather than constructed with the bridge: the pet needs the bridge as
        its JS surface, so neither can be an argument to the other's constructor.
        """
        self._pet = pet
        self._remember_pet = remember
        if pet is not None:
            # The skin may have been changed while she was hidden -- the page only
            # reports the inks, it does not know whether a figure was listening.
            pet.set_palette(self._bubble_palette)
            # Same for the icons: a turn that started before she was summoned still has
            # to be stoppable from her card, and the next announce could be a while off.
            live = bool(self._turns is not None and self._turns.waiting())
            pet.set_actions((BUBBLE_STOP, BUBBLE_ADD) if live else (BUBBLE_ADD,))

    def attach_greeter(self, greeter: Any) -> None:
        """The gate in front of the wake greeting; see :meth:`pet_arrived`."""
        self._greeter = greeter

    def pet_arrived(self) -> dict[str, object]:
        """The page says the figure finished appearing, so a greeting may now be spoken.

        Reported by the page rather than timed here because the page owns the arrival:
        its length lives in ``PetStage.vue`` and the shell has no business carrying a
        second copy of it. A shell that merely slept for a constant would greet in front
        of a half-drawn figure the first time anybody retimed the animation.

        Nothing listens for this on a normal wake with the HUD on screen -- there is no
        arrival then -- so an unexpected report is harmless, and a missing one is
        bounded by the gate's own deadline rather than swallowing the greeting.
        """
        if self._greeter is None:
            return {"ok": False, "error": "问候门闸未接线"}
        self._greeter.figure_arrived()
        return {"ok": True, "error": ""}

    def window_state(self) -> dict[str, object]:
        """Whether the window is filling the screen, for the button that says so."""
        maximized = self._geometry.maximized if self._geometry else False
        return {"maximized": bool(maximized), "error": ""}

    def shell_state(self) -> dict[str, object]:
        """How this build treats the X, and where the app currently is.

        The page needs ``tray`` to label its own 「收进托盘」 button honestly: with no
        tray -- pystray missing, or a machine that refused the icon -- the X quits,
        and a button offering to hide the app somewhere it cannot be found again
        would be a trap.
        """
        lifecycle = self._lifecycle
        if lifecycle is None:
            return {"visible": True, "tray": False, "status": "off", "pet": False}
        return lifecycle.to_mapping()

    def window_hide(self) -> dict[str, object]:
        """Hide to the tray. The X does this; the button says it out loud.

        Refuses with a reason when there is no tray, rather than minimising to
        somewhere nobody will look for it.
        """
        lifecycle = self._lifecycle
        if lifecycle is None or not lifecycle.hides_to_tray:
            return {"visible": True, "error": "托盘不可用，关掉窗口就是退出"}
        # ``hide()`` answers "did the window go away", the page needs "is it here
        # now", and the two differ the moment WinForms refuses the call.
        lifecycle.hide()
        visible = lifecycle.visible
        return {"visible": visible, "error": "" if not visible else "窗口没能藏起来"}

    def window_toggle_max(self) -> dict[str, object]:
        """Maximise, or restore. The page's 「铺满」 button.

        Only ever acts on the shell's own window, which is the one thing on this
        surface a wrong call cannot damage -- unlike everything else the assistant
        can reach, which is behind a tier the operator has to raise by hand.
        """
        window = self._window
        if window is None:
            return {"maximized": False, "error": "窗口还没准备好"}
        keeper = self._geometry
        want_max = not (keeper.maximized if keeper else False)
        try:
            if want_max:
                window.maximize()
            else:
                window.restore()
        except Exception as exc:  # pragma: no cover - a GUI that refuses to move
            logger.warning("window toggle failed", exc_info=True)
            return {"maximized": bool(keeper.maximized) if keeper else False, "error": str(exc)}
        if keeper is not None:
            keeper.set_maximized(want_max)
        return {"maximized": want_max, "error": ""}

    def pet_state(self) -> dict[str, object]:
        """Whether the figure is on the desktop, for the button that says so."""
        pet = self._pet
        return {"shown": bool(pet is not None and pet.shown), "error": ""}

    # 皮肤 id 是 theme.ts 里那六个之一. 这里只做形状校验(小写字母数字加连字符),
    # 具体是否认识由页面自己对着 SKINS 判 -- 桥面不该抄一份会过期的名单.
    _SKIN_ID = re.compile(r"^[a-z0-9_-]{1,24}$")

    def skin_apply(self, skin: object) -> dict[str, object]:
        """Tell the pet window which palette the dashboard just switched to.

        The window the operator clicked in repaints itself (``setSkin``); this call is
        for the *other* one. ``pet`` in the reply says whether a figure was up to
        receive it -- ``False`` is not an error, it means she will appear in the new
        colours next time she is summoned.
        """
        value = str(skin or "").strip()
        if not self._SKIN_ID.match(value):
            return {"ok": False, "error": f"皮肤 id 不合法：{value!r}"}
        pet = self._pet
        applied = bool(pet is not None and pet.apply_skin(value))
        return {"ok": True, "skin": value, "pet": applied, "error": ""}

    def pet_palette(self, fill: object, line: object, glow: object) -> dict[str, object]:
        """The skin's three inks, so the cards Python paints can match the figure.

        ``theme.ts`` owns the skins and this is the page saying out loud what it is
        wearing -- the alternative is a second copy of six palettes in Python, which is
        a list that goes stale the day somebody adds a seventh. The card's own fill is
        derived from the skin's fill rather than reused: a saturated panel behind text is
        unreadable, and this is the same argument that made the caption near-black.

        Bad input keeps the palette already in place and says so. A caption in the wrong
        colours is survivable; one that throws inside a bridge call is not.
        """
        numbers: list[int] = []
        for value in (fill, line, glow):
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                return {"ok": False, "error": f"色值要的是数字，收到 {type(value).__name__}"}
            number = int(value)
            if not 0 <= number <= 0xFFFFFF:
                return {"ok": False, "error": f"色值超出范围：{number}"}
            numbers.append(number)
        palette = bubble_palette(numbers[0], numbers[1], numbers[2])
        self._bubble_palette = palette
        pet = self._pet
        if pet is not None:
            pet.set_palette(palette)
        return {"ok": True, "pet": pet is not None, "error": ""}

    def pet_toggle(self) -> dict[str, object]:
        """Show or hide the desktop pet, and remember which the operator chose.

        The switch is the operator's, so it is theirs next launch too -- but only
        ever through this call. Nothing in the voice pipeline, the agent or a tool
        can put a figure on someone's desktop by itself.
        """
        lifecycle = self._lifecycle
        if lifecycle is None:
            return {"shown": False, "error": "这个界面没有桌面宠物"}
        shown = lifecycle.toggle_pet()
        remember = self._remember_pet
        if remember is not None:
            try:
                remember(shown)
            except Exception:  # pragma: no cover - a preference file that is busy
                logger.debug("could not remember the pet choice", exc_info=True)
        return {"shown": shown, "error": ""}

    def pet_grip(self, rect: object) -> dict[str, object]:
        """Where the page's drag handle is, in fractions of its own viewport.

        The window is click-through everywhere except this patch, and only the page
        knows where it drew the patch -- so it reports it, and the shell watches the
        pointer instead of swallowing every click on the desktop.
        """
        pet = self._pet
        if pet is None:
            return {"ok": False}
        pet.report_grip(rect)
        return {"ok": True}

    def pet_frame(self, payload: object) -> dict[str, object]:
        """One composited frame of the figure, as a PNG data URL.

        The other direction of the same bargain as ``pet_grip``: the page can see the
        figure's pixels and the shell can see the desktop, and only the shell can put
        them together in a window that has real per-pixel alpha. Rejected quietly
        when nothing is compositing -- the page stops sending once the shell never
        asks, and a frame rate should not be an error log.
        """
        pet = self._pet
        if pet is None or not pet.present_frame(payload):
            return {"ok": False}
        return {"ok": True}

    def pet_drag(self, dragging: object) -> dict[str, object]:
        """Freeze the click-through decision while the pet is being moved."""
        pet = self._pet
        if pet is None:
            return {"ok": False}
        pet.set_dragging(bool(dragging))
        return {"ok": True}

    # ------------------------------------------------------------------
    # 提醒 / 记忆 / 知识库 —— 三个已经建好但一直没接到人身上的能力
    # ------------------------------------------------------------------

    def reminders(self) -> dict[str, object]:
        """Every reminder, plus which channels can actually announce one.

        ``channels`` belongs in this answer because a reminder that cannot be said
        or popped is a note nobody reads: with no voice loaded and no tray running,
        the list should say so next to the rows rather than let them look armed.
        """
        service = self._reminders
        if service is None:
            return {"rows": [], "channels": [], "error": "这个进程没有接提醒"}
        try:
            rows = [_as_row(item) for item in service.all_reminders()]
        except Exception as exc:  # pragma: no cover - the service already guards
            logger.exception("listing reminders failed")
            return {"rows": [], "channels": [], "error": str(exc)}
        announcer = self._announcer
        return {
            "rows": rows,
            "channels": list(announcer.channels) if announcer is not None else [],
            "error": "",
        }

    def reminder_add(self, text: str, when: str = "") -> dict[str, object]:
        """Set a reminder from the window. The same path the voice tool takes."""
        service = self._reminders
        if service is None:
            return {"ok": False, "row": None, "error": "这个进程没有接提醒"}
        try:
            made = service.add(str(text or ""), str(when or ""))
        except Exception as exc:
            # A refusal is the normal outcome here (「听不懂这个时间」), so it goes
            # back as an answer rather than a stack trace.
            return {"ok": False, "row": None, "error": str(exc)}
        return {"ok": True, "row": _as_row(made), "error": ""}

    def reminder_cancel(self, key: str) -> dict[str, object]:
        service = self._reminders
        if service is None:
            return {"ok": False, "removed": "", "error": "这个进程没有接提醒"}
        try:
            return {"ok": True, "removed": service.cancel(str(key or "")), "error": ""}
        except Exception as exc:
            return {"ok": False, "removed": "", "error": str(exc)}

    def reminder_toggle(self, job_id: str, enabled: bool) -> dict[str, object]:
        service = self._reminders
        if service is None:
            return {"ok": False, "error": "这个进程没有接提醒"}
        ok = service.toggle(str(job_id or ""), bool(enabled))
        return {"ok": bool(ok), "error": "" if ok else "那条提醒已经不在了"}

    def memory_list(self) -> dict[str, object]:
        """What the assistant remembers about you. Read-only until a row is deleted."""
        memory = self._memory
        if memory is None:
            return {"rows": [], "error": "这个进程没有接记忆"}
        try:
            return {"rows": [_as_row(item) for item in memory.list_memories()], "error": ""}
        except Exception as exc:  # pragma: no cover - the service already guards
            logger.exception("reading memories failed")
            return {"rows": [], "error": str(exc)}

    def memory_forget(self, memory_id: object) -> dict[str, object]:
        memory = self._memory
        if memory is None:
            return {"ok": False, "error": "这个进程没有接记忆"}
        try:
            key = int(str(memory_id))
        except (TypeError, ValueError):
            return {"ok": False, "error": "记忆编号不对"}
        return {"ok": bool(memory.forget(key)), "error": ""}

    def memory_forget_all(self, confirmed: bool) -> dict[str, object]:
        """Wipe the user-scope memory. Confirmation is re-checked, never trusted.

        The same rule as deleting files and ending processes: a button the page can
        call is not a human decision, so ``confirmed`` has to be true *and* the
        caller has to have said what is about to go.
        """
        memory = self._memory
        if memory is None:
            return {"removed": 0, "error": "这个进程没有接记忆"}
        if not bool(confirmed):
            return {"removed": 0, "error": "需要二次确认：这会忘掉它记住的关于你的全部条目"}
        try:
            return {"removed": int(memory.forget_scope()), "error": ""}
        except Exception as exc:  # pragma: no cover - database trouble
            logger.exception("clearing memories failed")
            return {"removed": 0, "error": str(exc)}

    def knowledge_state(self) -> dict[str, object]:
        """The document table and the counters, so the panel can say what is in it."""
        knowledge = self._knowledge
        if knowledge is None:
            return {"documents": [], "stats": {}, "error": "这个进程没有接知识库"}
        try:
            return {
                "documents": [_as_row(row) for row in knowledge.documents()],
                "stats": dict(knowledge.stats()),
                "error": "",
            }
        except Exception as exc:  # pragma: no cover - database trouble
            logger.exception("reading the knowledge base failed")
            return {"documents": [], "stats": {}, "error": str(exc)}

    def knowledge_ingest(self, path: str) -> dict[str, object]:
        """Add one file or directory. Reads it; never moves or deletes it."""
        knowledge = self._knowledge
        if knowledge is None:
            return {"ok": False, "error": "这个进程没有接知识库"}
        text = str(path or "").strip().strip('"')
        if not text:
            return {"ok": False, "error": "要给出文件或文件夹的路径"}
        try:
            result = knowledge.ingest(text)
        except Exception as exc:
            logger.warning("knowledge ingest failed for %s", text, exc_info=True)
            return {"ok": False, "error": str(exc)}
        row = _as_row(result)
        if str(row.get("error") or ""):
            return {"ok": False, "error": str(row.get("error"))}
        skipped = str(row.get("skipped_reason") or "")
        return {
            "ok": not skipped,
            "error": skipped,
            "document": row,
        }

    def knowledge_forget(self, doc_id: str) -> dict[str, object]:
        """Forget a document. Deletes the index rows, never the file on disk.

        The page says this out loud next to the button, because "删除" next to a
        filename means something else to every person who reads it.
        """
        knowledge = self._knowledge
        if knowledge is None:
            return {"ok": False, "error": "这个进程没有接知识库"}
        return {"ok": bool(knowledge.forget(str(doc_id or ""))), "error": ""}

    def knowledge_probe(self, query: str) -> dict[str, object]:
        """What the knowledge base would answer with, for this question.

        Exposed because an answer with no visible source is uncheckable: seeing the
        chunk and its score is the difference between "it looked it up" and "it
        sounded like it knew".
        """
        knowledge = self._knowledge
        if knowledge is None:
            return {"hits": [], "error": "这个进程没有接知识库"}
        text = str(query or "").strip()
        if not text:
            return {"hits": [], "error": ""}
        try:
            hits = [_as_row(item) for item in knowledge.retrieve(text, top_k=5)]
        except Exception as exc:  # pragma: no cover - vector trouble
            logger.exception("knowledge lookup failed")
            return {"hits": [], "error": str(exc)}
        return {"hits": hits, "error": ""}

    # ------------------------------------------------------------------
    # 自动化 —— 定时任务 / 工作流 / 计划
    #
    # Three engines that were already running. ``SchedulerService.stats`` and
    # ``WorkflowService.stats`` both say "for the HUD's automation panel" in their
    # docstrings; the panel was never built, so both have had counters nobody read.
    # These methods only call what those services already expose -- no backend
    # semantics change, and none of them can do anything the CLI could not.
    # ------------------------------------------------------------------

    def automation_overview(self) -> dict[str, object]:
        """The three counters the tab opens with, in one round trip.

        One call rather than three: the tab draws all three at once, and three
        separate trips through ``evaluate_js`` to fill one screen is three chances
        for it to arrive half-filled.
        """
        planner = self._planner
        return {
            "scheduler": _stats_of(self._scheduler),
            "workflow": _stats_of(self._workflow),
            "planner": _stats_of(planner),
            "planner_enabled": bool(planner is not None and planner.enabled),
            "error": "",
        }

    def scheduled_jobs(self) -> dict[str, object]:
        """Every scheduled job that is *not* a reminder.

        Reminders are filtered out rather than shown read-only: they have their own
        tab, and two panels that can toggle the same row is how a UI ends up
        disagreeing with itself about whether something is on.
        """
        service = self._scheduler
        if service is None:
            return {"rows": [], "error": "这个进程没有接调度器"}
        try:
            rows = [_job_row(service, job) for job in service.list_jobs() if not _is_reminder(job)]
        except Exception as exc:  # pragma: no cover - the service guards its reads
            logger.exception("listing scheduled jobs failed")
            return {"rows": [], "error": str(exc)}
        return {"rows": rows, "error": ""}

    def job_toggle(self, job_id: str, enabled: bool = True) -> dict[str, object]:
        """Enable or disable one scheduled job."""
        refusal = _reminder_refusal(job_id)
        if refusal:
            return {"ok": False, "error": refusal}
        service = self._scheduler
        if service is None:
            return {"ok": False, "error": "这个进程没有接调度器"}
        try:
            found = service.set_enabled(str(job_id), bool(enabled))
        except Exception as exc:
            logger.exception("toggling job %s failed", job_id)
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "error": ""} if found else {"ok": False, "error": "没有这个任务"}

    def job_run(self, job_id: str) -> dict[str, object]:
        """Run one job once, right now.

        The same path the clock takes, which is the point: it is how an operator
        finds out whether a job works *before* trusting it with a schedule. The run
        is recorded like any other, so the answer here and the history agree.
        """
        refusal = _reminder_refusal(job_id)
        if refusal:
            return {"ok": False, "run": None, "error": refusal}
        service = self._scheduler
        if service is None:
            return {"ok": False, "run": None, "error": "这个进程没有接调度器"}
        try:
            run = service.run_now(str(job_id))
        except Exception as exc:
            # An unknown action raises here. That is an answer the operator wants as
            # text -- "这条任务的动作没注册" -- rather than a traceback in the log.
            return {"ok": False, "run": None, "error": str(exc)}
        row = _as_row(run)
        return {"ok": bool(row.get("ok", False)), "run": row, "error": ""}

    def job_remove(self, job_id: str) -> dict[str, object]:
        """Delete a scheduled job."""
        refusal = _reminder_refusal(job_id)
        if refusal:
            return {"ok": False, "removed": False, "error": refusal}
        service = self._scheduler
        if service is None:
            return {"ok": False, "removed": False, "error": "这个进程没有接调度器"}
        try:
            removed = service.remove_job(str(job_id))
        except Exception as exc:
            logger.exception("removing job %s failed", job_id)
            return {"ok": False, "removed": False, "error": str(exc)}
        return {"ok": True, "removed": bool(removed), "error": ""}

    def workflow_definitions(self) -> dict[str, object]:
        """The loaded definitions plus the most recent runs."""
        service = self._workflow
        if service is None:
            return {"rows": [], "runs": [], "error": "这个进程没有接工作流"}
        try:
            rows = [_as_row(item) for item in service.definitions()]
        except Exception as exc:  # pragma: no cover - the service guards its reads
            logger.exception("listing workflow definitions failed")
            return {"rows": [], "runs": [], "error": str(exc)}
        try:
            runs = [_as_row(item) for item in service.history(limit=10)]
        except Exception:  # pragma: no cover - history is best-effort
            logger.exception("reading workflow history failed")
            runs = []
        return {"rows": rows, "runs": runs, "error": ""}

    def workflow_run(self, name: str) -> dict[str, object]:
        """Run one workflow by hand.

        Not a way around the safety switches: the steps go through the same tool
        registry the assistant uses, so ``run_shell`` at level 0 is refused here for
        the same reason, with the same message. The panel says so next to the button,
        because "run" in a UI reads like "with my permissions".
        """
        service = self._workflow
        if service is None:
            return {"ok": False, "run": None, "error": "这个进程没有接工作流"}
        try:
            run = service.run(str(name))
        except Exception as exc:
            return {"ok": False, "run": None, "error": str(exc)}
        row = _as_row(run)
        return {"ok": bool(row.get("ok", False)), "run": row, "error": ""}

    def workflow_reload(self) -> dict[str, object]:
        """Re-read the definition folder, so a new YAML does not need a restart."""
        service = self._workflow
        if service is None:
            return {"ok": False, "count": 0, "error": "这个进程没有接工作流"}
        try:
            definitions = service.reload()
        except Exception as exc:
            return {"ok": False, "count": 0, "error": str(exc)}
        return {"ok": True, "count": len(definitions), "error": ""}

    def plan_goal(self, goal: str) -> dict[str, object]:
        """Break a goal into steps. The plan is shown, never executed.

        Deliberately the only planning call in this bridge: ``planner`` has no
        executor wired anywhere in this build, and a button that looked like "do it"
        would be the interface promising what the backend does not have. It costs one
        model call, which the panel says next to the field.
        """
        service = self._planner
        if service is None:
            return {"ok": False, "plan": None, "error": "这个进程没有接任务规划"}
        try:
            plan = service.plan(str(goal or ""))
        except Exception as exc:
            # A refusal is the normal outcome here -- blank goal, no key, or a model
            # answer that is not an executable plan -- so it comes back as an answer.
            return {"ok": False, "plan": None, "error": str(exc)}
        return {"ok": True, "plan": plan.to_dict(), "error": ""}

    def snapshot(self) -> dict[str, Any]:
        """One system reading, plus any caveats. Never raises into the page."""
        return self._system.report().to_dict()

    def alerts(self) -> dict[str, object]:
        """What the alert centre has open, and what just recovered.

        Pulled rather than pushed: the HUD already polls ``snapshot()`` every second
        and a half and carries the open list along with it, so this exists for the
        moments a page joins late and for the phone, which has no poll of its own.
        """
        alerts = self._alerts
        if alerts is None:
            return {"active": [], "history": [], "error": "告警中心未启用"}
        return {
            "active": [alert.to_dict() for alert in alerts.active()],
            "history": [alert.to_dict() for alert in alerts.history()],
            "error": "",
        }

    def alerts_ack(self, code: object) -> dict[str, object]:
        """「知道了」. The reading that caused it is unchanged, and so is the box's honesty."""
        alerts = self._alerts
        if alerts is None:
            return {"ok": False, "error": "告警中心未启用"}
        return {"ok": alerts.acknowledge(str(code or "")), "error": ""}

    def alerts_ack_all(self) -> dict[str, object]:
        alerts = self._alerts
        if alerts is None:
            return {"ok": False, "count": 0, "error": "告警中心未启用"}
        return {"ok": True, "count": alerts.acknowledge_all(), "error": ""}

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

    def disk_advice(self) -> dict[str, object]:
        """What the model makes of the scan the page is looking at.

        Read-only, and it stays that way: this asks for an opinion about a list the
        human still has to tick. The turn is deliberately kept out of the chat
        history — the digest is machine-written, and replaying twelve paths in every
        later answer would steer conversations nobody had.
        """
        if self._chat is None:
            return {"text": "", "error": "对话服务不可用", "tools_used": []}
        digest = self._disk.digest()
        if not digest:
            return {"text": "", "error": "先按「扫描」，有结果才能分析", "tools_used": []}
        reply = self._chat.ask(JUNK_BRIEFING + digest, remembered=False)
        return {"text": reply.answer, "error": reply.error, "tools_used": list(reply.tools_used)}

    def computer_levels(self) -> dict[str, object]:
        """The four desktop-control levels, with the one in force marked."""
        if self._computer_access is None:
            return {"error": "桌面控制不可用", "current": {}, "levels": []}
        return self._computer_access.levels()

    def computer_set_tier(self, tier: object) -> dict[str, object]:
        """Move the assistant between the control levels.

        Takes effect on the next tool call: the service reads its section per
        action, so opening 键鼠全开 does not need a restart -- and neither does
        closing it, which is the direction that matters in a hurry.
        """
        if self._computer_access is None:
            return {"error": "桌面控制不可用", "current": {}, "levels": []}
        return self._computer_access.set_tier(tier)

    def computer_set_typing(self, allowed: object) -> dict[str, object]:
        """Turn typing on or off. Separate from the levels on purpose.

        The dials decide how far she may reach; this one decides whether she may put
        a sentence into somebody else's input box. Same shape as ``set_tier`` --
        written immediately, read per action -- because the moment that matters is
        the one where you want it *off*.
        """
        if self._computer_access is None:
            return {"error": "桌面控制不可用", "current": {}, "levels": []}
        return self._computer_access.set_typing(allowed)

    def shell_levels(self) -> dict[str, object]:
        """The four command-line levels, plus the ledger of what has run."""
        if self._command_access is None:
            return {"error": "命令行不可用", "current": {}, "levels": [], "commands": []}
        return self._command_access.levels()

    def shell_set_tier(self, tier: object) -> dict[str, object]:
        """Move the command line between levels.

        Reading the level and acting on it are the same instant for the tool, which
        asks on every call -- so raising it takes effect on the next command and
        lowering it takes effect even mid-sentence. The 管理员 level does not grant
        anything by itself: each command still waits on the operator clicking 是 in
        the UAC prompt Windows shows.
        """
        if self._command_access is None:
            return {"error": "命令行不可用", "current": {}, "levels": [], "commands": []}
        return self._command_access.set_mode(tier)

    def process_kill(self, items: list[object], confirmed: bool) -> dict[str, object]:
        """End the processes the page ticked, as the page saw them.

        Entries carry ``pid`` *and* ``name`` because a number is not an identity: the
        row drawn two minutes ago may now point at something else entirely, and the
        controller re-reads the name at the moment of the act. Confirmation is
        re-checked in the application layer, never trusted here -- the same two doors
        the disk cleanup walks through.
        """
        if self._process is None:
            return {"results": [], "ended": 0, "error": "进程服务不可用"}
        return self._process.kill(list(items), confirmed=bool(confirmed))

    def process_proposals(self) -> dict[str, object]:
        """What the assistant has asked to end and nobody has answered yet.

        Read on demand and drawn as a strip in the same panel the tick happens in, so
        「关掉记事本」 from the chat lands where the operator is already looking at
        processes. Confirming one goes through :meth:`process_kill`, unchanged: this
        list is a request to show a button, not a second execution path.
        """
        if self._process is None:
            return {"entries": [], "error": "进程服务不可用"}
        return {"entries": self._process.proposals(), "error": ""}

    def process_dismiss(self, pid: object) -> dict[str, object]:
        """Answer a proposal with 「不用了」. Ends nothing."""
        if self._process is None:
            return {"ok": False, "error": "进程服务不可用"}
        number = _int_or(pid, -1)
        if number <= 0:
            return {"ok": False, "error": "pid 得是个正整数"}
        return {"ok": self._process.dismiss(number), "error": ""}

    def tts_voices(self) -> dict[str, object]:
        """The voices the assistant can speak with, current one marked."""
        if self._voice_picker is None:
            return {"error": "语音选择不可用", "engine": "", "current": "", "choices": []}
        return self._voice_picker.voices()

    def tts_pick(self, voice: str) -> dict[str, object]:
        """Choose a voice. Takes effect on the next spoken sentence."""
        if self._voice_picker is None:
            return {"error": "语音选择不可用", "engine": "", "current": "", "choices": []}
        return self._voice_picker.pick(voice)

    def tts_preview(self, voice: str) -> dict[str, object]:
        """Synthesise one sentence with a voice, through the page's own speaker.

        Previewing does not select: hearing the alternative must not be a commitment
        to it.
        """
        if self._voice_picker is None:
            return {"ok": False, "error": "语音选择不可用"}
        return self._voice_picker.preview(voice)

    def tts_preview_pcm(self, voice: object = None, text: object = None) -> dict[str, object]:
        """Synthesise one sentence and **return** the audio instead of playing it.

        The phone cannot hear the desktop's speakers, so ``tts_preview`` -- which
        pushes samples into the computer's own audio channel -- would look like a
        broken button from a phone. The bytes come back here instead and the phone
        plays them itself. One synthesis path behind both is what stops 试听 from
        sounding different on the phone than on the desktop.
        """
        if self._voice_picker is None:
            return {"ok": False, "error": "语音选择不可用"}
        pcm = self._voice_picker.preview_pcm(voice, str(text) if text else None)
        if pcm.get("ok") and isinstance(pcm.get("pcm"), bytes):
            pcm["pcm"] = base64.b64encode(pcm["pcm"]).decode("ascii")  # type: ignore[arg-type]
        return pcm

    def tts_set_style(self, speed: object = None, volume: object = None) -> dict[str, object]:
        """Move the rate / volume sliders. Takes effect on the next utterance.

        Values arrive from a slider, so they are typed and range-checked in the
        picker rather than trusted here; the reply is the same payload ``tts_voices``
        returns, so the page can re-render from one shape.
        """
        if self._voice_picker is None:
            return {"error": "语音选择不可用", "current": "", "choices": []}
        return self._voice_picker.set_style(speed=speed, volume=volume)

    # -- recorded voices and phone calls -----------------------------------

    def voice_clone_list(self) -> dict[str, object]:
        """The voices made from a recording, newest first.

        Returns the same ``choices`` shape as ``tts_voices`` so the phone can
        render both lists with one component, plus the rules the recording UI
        needs to state *before* the user holds the phone to their mouth.
        """
        if self._voice_picker is None:
            return {"error": "语音选择不可用", "voices": [], "cloning": {}}
        listing = self._voice_picker.voices()
        choices = listing.get("choices")
        recorded = [
            entry
            for entry in (choices if isinstance(choices, list) else [])
            if isinstance(entry, dict) and entry.get("kind") == "clone"
        ]
        return {
            "error": listing.get("error", ""),
            "voices": recorded,
            "cloning": listing.get("cloning", {}),
        }

    def voice_clone_add(
        self,
        name: object = None,
        pcm: object = None,
        sample_rate: object = None,
        prompt_text: object = None,
        upload: object = False,
    ) -> dict[str, object]:
        """Store one recording as a reusable voice. Returns the refreshed list.

        ``pcm`` arrives base64-encoded because it crosses JSON: raw s16le bytes
        are not a JSON type, and a list of sample values would be several times
        the size of the audio for no benefit. The reference clip is *not*
        re-encoded to another rate -- a wrong resample here produces a voice that
        sounds like a stranger with no error to explain it, so a rate that is not
        16 kHz is refused by the library and said out loud.

        ``upload`` is the user's decision, taken on the phone, and it defaults to
        **off**. Uploading means the recording leaves this machine and goes to
        Alibaba DashScope, who clone it there -- which is the only way a phone
        call answers in a few hundred milliseconds instead of sixteen seconds.
        Because that is a disclosure, not a performance tweak, it happens only
        when asked for, and the reply says which of the two happened so the
        screen can tell the user rather than leave them guessing.

        The upload is attempted after the store has succeeded, deliberately: a
        recording is the user's and is kept even when the vendor rejects it, so a
        failed upload degrades to a working local voice plus a message -- never
        to a lost recording.
        """
        if self._voice_library is None:
            return {"error": "这个进程没有音色库", "voices": [], "cloning": {}}
        if not isinstance(pcm, str) or not pcm:
            return {**self.voice_clone_list(), "error": "没收到录音"}
        try:
            audio = base64.b64decode(pcm, validate=True)
        except (ValueError, binascii.Error):
            return {**self.voice_clone_list(), "error": "录音数据坏了（base64 解不开），重录一次"}
        rate = _int_or(sample_rate, 0)
        try:
            stored = self._voice_library.add(
                name=name,
                pcm=audio,
                sample_rate=rate,
                prompt_text=prompt_text,
            )
        except VoiceLibraryError as exc:
            # Written for the person holding the phone; shown verbatim.
            return {**self.voice_clone_list(), "error": str(exc)}
        if not _truthy(upload):
            return {"error": "", "cloud": "", "cloud_error": "", **self.voice_clone_list()}
        cloud_error = self._upload_voice(stored.voice_id, audio, rate, prompt_text)
        return {
            "error": "",
            "cloud_error": cloud_error,
            **self.voice_clone_list(),
        }

    def _upload_voice(
        self, voice_id: str, audio: bytes, sample_rate: int, prompt_text: object
    ) -> str:
        """Hand one stored recording to the app layer for uploading.

        The network call itself is in :mod:`jarvis.app.voice_cloud`: this layer
        may only reach ``app``/``config``/``core``, and ``tts`` is none of those.
        """
        return voice_cloud.upload_voice(
            self._voice_library, voice_id, audio, sample_rate, prompt_text
        )

    def _forget_cloud_voice(self, cloud_voice: str) -> None:
        """Best-effort vendor-side delete, for a voice nothing local points at."""
        voice_cloud.forget_cloud_voice(cloud_voice)

    def voice_clone_remove(self, voice_id: object = None) -> dict[str, object]:
        """Forget a recorded voice. Deletes one of the user's own files.

        On the phone whitelist this one *is* allowed, unlike ``chat_delete``: the
        recording is a thing the phone made moments ago and is the only screen
        that can see it, so refusing here would leave a person unable to undo
        their own recording from the device that took it. It is still scoped to
        one voice id, and it never touches anything outside ``voices_dir``.

        A voice that was uploaded also has a counterpart on the vendor's servers,
        and that is deleted too. Leaving a clone of somebody's voice alive after
        they asked for their recording to be removed is not a state this feature
        is allowed to end in -- so the cloud half goes first, while the local
        entry can still tell us its id.
        """
        if self._voice_library is None:
            return {"error": "这个进程没有音色库", "voices": [], "cloning": {}}
        voice = str(voice_id or "")
        if not voice:
            return {**self.voice_clone_list(), "error": "没说删哪一个"}
        cloud_voice = self._voice_library.cloud_id(voice)
        if not self._voice_library.remove(voice):
            return {**self.voice_clone_list(), "error": f"没有这个音色：{voice}"}
        if cloud_voice:
            self._forget_cloud_voice(cloud_voice)
        return {"error": "", **self.voice_clone_list()}

    # -- recording a sample of *this* machine's operator ----------------------

    def voice_sample_start(self) -> dict[str, object]:
        """Begin holding the microphone to capture one sample for a cloned voice.

        Returns at once: the page polls :meth:`voice_sample_status` for the elapsed
        time and the level, because a bridge call that blocked for fifteen seconds
        would freeze the window it came from -- and the operator would read that as
        the recording having hung.
        """
        if self._voice_sample is None:
            return {"ok": False, "error": "这个进程没有接录音", "phase": "idle"}
        return dict(self._voice_sample.start())

    def voice_sample_status(self) -> dict[str, object]:
        """Where the take stands. Cheap enough for the page to poll while it counts."""
        if self._voice_sample is None:
            return {"phase": "idle", "error": "这个进程没有接录音"}
        return dict(self._voice_sample.status())

    def voice_sample_stop(self) -> dict[str, object]:
        """Close the device and keep the take for review."""
        if self._voice_sample is None:
            return {"ok": False, "error": "这个进程没有接录音"}
        return dict(self._voice_sample.stop())

    def voice_sample_discard(self) -> dict[str, object]:
        """Throw the take away without storing it."""
        if self._voice_sample is None:
            return {"ok": False, "error": "这个进程没有接录音"}
        return dict(self._voice_sample.discard())

    def voice_sample_save(
        self, name: object = None, prompt_text: object = None, upload: object = False
    ) -> dict[str, object]:
        """Store the held take as a voice. The audio does not cross the bridge.

        The sentence is asked for, not transcribed: the enrolment matches the clip
        against the words spoken in it, and a guessed transcript makes a worse voice
        than no voice. Same rule the phone's recording screen follows.

        ``upload`` is the operator's per-recording decision to send the clip to the
        vendor for a cloud clone -- the only path that can answer in their timbre on
        a machine that cannot load the offline model. It is read with the same
        "did JSON mean yes" rule as the phone's own upload switch, because a page
        that sends ``"true"`` and a backend that accepts only ``True`` produces a
        voice that is quietly local-only, with nothing on screen to point at this
        line.
        """
        if self._voice_sample is None:
            return {"ok": False, "error": "这个进程没有接录音"}
        result = dict(
            self._voice_sample.save(name=name, prompt_text=prompt_text, upload=_truthy(upload))
        )
        if result.get("ok"):
            # The new voice has to appear in the list without a second round-trip.
            result["voices"] = self.voice_clone_list().get("voices", [])
        return result

    def call_readiness(self) -> dict[str, object]:
        """Whether a spoken turn can happen at all, asked *before* one is tried."""
        if self._voice_call is None:
            return {"error": "这个进程没接通话", "asr": "", "tts": "", "ready": False}
        ready = self._voice_call.readiness()
        return {"error": "", "ready": True, **ready}

    def call_warmup(self) -> dict[str, object]:
        """Begin loading the recognizer now, while the user is still deciding.

        Loading SenseVoice takes tens of seconds. Starting it when the call
        screen opens means the delay lands while the user is reading the screen
        rather than after their first sentence has already been recorded.
        """
        if self._voice_call is None:
            return {"error": "这个进程没接通话", "ready": False}
        return {"error": "", "ready": True, **self._voice_call.warmup()}

    def call_turn(self, pcm: object = None, sample_rate: object = None) -> dict[str, object]:
        """One spoken round trip: hear it, answer it, say it back.

        The reply carries the answer *text* as well as the audio. The audio is
        best-effort -- a synthesis failure still returns what she would have said
        -- so a phone that only rendered audio would show a blank screen and no
        reason.
        """
        if self._voice_call is None:
            return _call_failure("这个进程没接通话")
        if not isinstance(pcm, str) or not pcm:
            return _call_failure("没收到录音")
        try:
            audio = base64.b64decode(pcm, validate=True)
        except (ValueError, binascii.Error):
            return _call_failure("录音数据坏了（base64 解不开），重录一次")
        result = self._voice_call.turn(audio, _int_or(sample_rate, 16_000))
        return _call_reply(result)

    def call_speak(self, text: object = None) -> dict[str, object]:
        """Speak a line the phone already has, without asking the agent again.

        Used when the screen shows a message and the user taps 「读给我听」: going
        back through the agent would answer a second time and change the
        conversation, which is not what pressing that button says.
        """
        if self._voice_call is None:
            return _call_failure("这个进程没接通话")
        return _call_reply(self._voice_call.speak(str(text or "")))

    def activity_log(self) -> dict[str, object]:
        """What the assistant last did to this machine, newest first.

        An agent that moves the mouse and deletes files has to answer "what did you
        just do" without making the operator open a log file. Refusals are in here
        too: a safety gate that fired is the most interesting thing that can happen,
        and it is otherwise invisible.
        """
        if self._tools is None:
            return {"entries": [], "error": "工具服务不可用"}
        return {"entries": [call.to_dict() for call in self._tools.recent()], "error": ""}

    def chat_sessions(self) -> dict[str, object]:
        """Stored conversations, newest first, with the open one marked."""
        if self._chat is None:
            return {"error": "对话服务不可用", "current": "", "sessions": []}
        return self._chat.sessions()

    def chat_new_session(self) -> dict[str, object]:
        """Open a blank conversation. The one on screen stays stored."""
        if self._chat is None:
            return {"error": "对话服务不可用", "current": "", "sessions": []}
        return self._chat.new_session()

    def chat_switch(self, session_id: str) -> dict[str, object]:
        if self._chat is None:
            return {"error": "对话服务不可用", "current": "", "sessions": []}
        return self._chat.switch_session(session_id)

    def chat_rename(self, session_id: str, title: str) -> dict[str, object]:
        if self._chat is None:
            return {"error": "对话服务不可用", "current": "", "sessions": []}
        return self._chat.rename_session(session_id, title)

    def chat_delete(self, session_id: str) -> dict[str, object]:
        """Delete one stored conversation. Only this deletes anything."""
        if self._chat is None:
            return {"error": "对话服务不可用", "current": "", "sessions": []}
        return self._chat.delete_session(session_id)

    def chat_messages(self, session_id: str) -> dict[str, object]:
        """Every stored turn of one conversation, oldest first."""
        if self._chat is None:
            return {"error": "对话服务不可用", "current": "", "messages": []}
        return self._chat.session_messages(session_id)

    def chat_models(self) -> dict[str, object]:
        """Which providers and models the chat can be pointed at, and the current pair.

        Answers with the two-level shape (providers, each with its own models) rather
        than one flat list of "provider + model" rows: a key belongs to a provider and
        a provider offers several models, which is exactly the two pickers the chat
        header draws.
        """
        if self._settings is None:
            return {"error": "设置不可用", "provider": "", "model": "", "providers": []}
        listing = self._settings.model_choices()
        if self._model_probe is not None:
            listing["caps"] = self._model_probe.caps().as_rows()
        return listing

    def chat_pick(self, provider: str, model: str = "") -> dict[str, object]:
        """Point the chat at another provider and/or model.

        ``model`` is optional because picking a provider has to land somewhere: when
        it is blank the provider's own default is used, so switching vendor never
        leaves the picker pointing at a model that does not exist there.

        Answers with a fresh choice list, not just an ok flag: the page has to be
        able to show *why* a pick was refused (an unknown name, a provider whose
        endpoint failed validation) without a second round-trip.
        """
        if self._settings is None:
            return {"error": "设置不可用", "provider": "", "model": "", "providers": []}
        answer = self._settings.choose_model(provider, model)
        listing = self._settings.model_choices()
        if not bool(answer.get("ok", True)):
            listing["error"] = str(answer.get("error") or "切换失败")
        return listing

    def chat_tuning(self, thinking: object = None, turns: object = None) -> dict[str, object]:
        """Set the thinking level and/or context size for the model now in use.

        Both are optional and independent: the two knobs sit next to each other in the
        chat header, and saving one must not silently reset the other.
        """
        if self._settings is None:
            return {"ok": False, "error": "设置不可用"}
        return self._settings.set_tuning(thinking, turns)

    def llm_add_model(
        self, provider: str, model_id: str = "", label: str = ""
    ) -> dict[str, object]:
        """Add a model to one provider's list, but only if it answers.

        Saving first and rolling back is not a shortcut around the rule, it is the only
        way to honour it: a row that does not exist yet cannot be probed, because
        everything about reaching a model is looked up from that row. The window this
        leaves the row open for is one request, and the state the operator ends up in is
        exactly the one they asked for -- nothing saved unless it connected.
        """
        settings = self._settings
        if settings is None:
            return {"ok": False, "error": "设置不可用", "models": []}
        answer = settings.add_model(provider, model_id, label)
        if not _accepted(answer):
            return answer
        return self._gate(
            answer, provider, model_id, lambda: settings.remove_model(provider, model_id)
        )

    def llm_test(self, provider: str, model: str = "") -> dict[str, object]:
        """Ask one model two questions before the panel is allowed to keep it.

        Whether a row can be saved is decided by :class:`SettingsService`, not here: this
        is the door the button presses, and the rule has to hold for the other doors too
        (``settings_apply`` carries ``add_model`` as a field, and the assistant can reach
        that one itself).
        """
        if self._model_probe is None:
            return {"ok": False, "error": "连通测试不可用"}
        return self._model_probe.test(str(provider or ""), str(model or ""))

    def chat_collaborate(
        self,
        text: str,
        seats: object = (),
        rounds: int = 2,
        conversation: str = "",
        mode: str = "table",
    ) -> dict[str, object]:
        """Ask one question with several models, and return with the task id.

        ``mode`` is the shape: ``table`` (圆桌轮流), ``boss`` (主管分发) or ``vote``
        (并行三答 + 匿名互评投票). One door for all three, because the three differ only in
        what each seat is allowed to see, and a separate method per shape is how one of
        them stops recording the transcript the way the others do.

        Same rails as :meth:`chat_send` -- the question goes up first, the answer arrives
        through the state snapshot, and 停止 has an entry to point at. A table can hold the
        line for minutes at a time, so the one thing it may not be is synchronous.
        """
        service = self._chat
        if service is None:
            return {"question": text, "answer": "", "error": "对话服务未启用"}
        chosen = list(seats) if isinstance(seats, list) else []
        if not chosen:
            return {"question": text, "answer": "", "error": "先挑至少一个模型"}
        if mode not in (MODE_TABLE, MODE_BOSS, MODE_VOTE):
            return {"question": text, "answer": "", "error": f"不认识的协作形态：{mode}"}
        target = conversation or service.session_id
        state = self._state
        if state is not None:
            state.add_turn("user", text)
            state.set_turn(UiVoiceState.PROCESSING)
        if self._turns is None:
            return {
                "question": text,
                "answer": "",
                "error": "任务表未启用，圆桌需要它才能中途停下来",
            }
        started = self._turns.start(
            conversation_id=target,
            kind=KIND_ROUNDTABLE,
            question=text,
            participants=tuple(_seat_label(item) for item in chosen),
            work=lambda context: self._drive_table(
                context, service, text, chosen, _round_count(rounds), target, mode
            ),
        )
        self._push_conversations()
        return {
            "ok": True,
            "task_id": started.task_id,
            "conversation": target,
            "question": text,
            "answer": "",
            "error": "",
        }

    def _drive_table(
        self,
        context: Any,
        chat: ChatService,
        text: str,
        seats: list[object],
        rounds: int,
        conversation: str,
        mode: str,
    ) -> dict[str, object]:
        """Run the discussion on the turn's thread, narrating it to the page.

        The model named on the streaming bubble changes with every seat: one bubble shared
        by the table has to say whose words those are, or three answers look like one
        answer that keeps restarting.
        """
        speaker = {"label": str(seats[0]) if seats else ""}

        def delta(partial: str) -> None:
            if self._state is not None and context.conversation_id == self._active_tab():
                self._state.set_stream(
                    context.conversation_id,
                    context.task_id,
                    partial,
                    model=speaker["label"],
                )

        def took_floor(seat: Any, round_index: int) -> None:
            del round_index
            speaker["label"] = str(getattr(seat, "label", seat))

        reply = chat.ask_together(
            text,
            seats,
            mode=mode,
            rounds=rounds,
            conversation=conversation,
            task_id=context.task_id,
            on_floor=took_floor,
            on_delta=delta,
            on_phase=self._phase_reporter(context),
            should_stop=context.should_stop,
        )
        self._finish_turn(reply, conversation, context.task_id)
        with self._result_lock:
            self._results[context.task_id] = reply.to_dict()
            self._prune_results()
        return {
            "answer_chars": len(reply.answer),
            "error": reply.error,
            "state": STATE_CANCELLED if reply.cancelled else STATE_DONE,
        }

    def llm_remove_model(self, provider: str, model_id: str = "") -> dict[str, object]:
        """Remove a model from one provider's list, from the window."""
        if self._settings is None:
            return {"ok": False, "error": "设置不可用", "models": []}
        return self._settings.remove_model(provider, model_id)

    def settings_get(self) -> dict[str, object]:
        """The settings panel's contents. Never contains an API key value."""
        if self._settings is None:
            return {"error": "设置不可用", "providers": [], "api_key_set": False}
        return self._settings.snapshot()

    def settings_apply(self, patch: object) -> dict[str, object]:
        """Save what the panel sent and answer per field.

        The verdict comes back whether or not everything was accepted, because a
        dialog that discards the one field it rejected -- and shows the rest as
        saved -- is how an endpoint typo turns into "the assistant is broken".

        ``patch`` is typed ``object`` rather than a mapping on purpose: it arrives
        from JavaScript, where nothing guarantees the shape, and the guard below
        would be flagged as dead code by a signature that promised a dict.
        """
        if self._settings is None:
            return {"error": "设置不可用", "applied": {}, "problems": {}}
        if not isinstance(patch, dict):
            return {"error": "设置请求格式不对", "applied": {}, "problems": {}}
        answer = self._settings.apply(dict(patch))
        self._sync_surface_switches()
        return self._gate_new_provider(dict(patch), answer)

    def _sync_surface_switches(self) -> None:
        """Push the settings the surfaces draw: the loader kind and the read-aloud switch.

        Called after every apply rather than only when one of these fields moved: the
        chat panel, the desktop figure and the voice core all render these, and the
        visible failure is them disagreeing for a second after a save. Re-reading two
        values is cheaper than being right about which one changed.
        """
        if self._settings is None:
            return
        kind = self._settings.thinking_loader()
        pet = self._pet
        if pet is not None:
            pet.set_thinking_loader(kind)
        state = self._state
        if state is None:
            return
        state.set_thinking_loader(kind)
        state.set_speaks_typed(self._settings.speaks_typed())

    def list_endpoint_models(
        self, base_url: object = "", provider: object = ""
    ) -> dict[str, object]:
        """Ask an endpoint which models it serves, so 起始模型 stops being a copy job.

        The panel sends an address it just typed (or the row being edited). When a
        provider name comes with it and that row actually has a key in the environment,
        the key goes along: a gateway that wants a credential for ``/models`` wants the
        same one it wants for chat, and saying "它要钥匙才肯列模型" is the honest answer
        otherwise.
        """
        url = str(base_url or "").strip()
        if not url.startswith(("http://", "https://")):
            return {"ok": False, "models": [], "error": "地址必须以 http:// 或 https:// 开头"}
        key = ""
        name = str(provider or "").strip()
        settings = self._settings
        if name and settings is not None:
            row = next(
                (item for item in settings.snapshot()["models"] if item["name"] == name), None
            )
            if row is not None and row.get("key_set"):
                key = os.environ.get(str(row.get("key_env") or ""), "")
        ids, why = list_endpoint_models(url, timeout_seconds=10.0, api_key=key)
        if why:
            return {"ok": False, "models": [], "error": why}
        return {"ok": True, "models": ids[:200], "error": ""}

    def _gate_new_provider(
        self, patch: dict[str, object], answer: dict[str, object]
    ) -> dict[str, object]:
        """Probe a provider the panel just added, and take it back out if it is silent.

        Only the ``add_model`` field triggers it. Everything else in a settings patch has
        already been checked where it was written, and re-probing on every save would make
        保存 take as long as the endpoint does.

        A **list** is the panel's queue -- several providers added in one save. Each row is
        probed and rolled back on its own: refusing the whole batch because one endpoint was
        down would throw away the rows that answered perfectly, and keeping all of them
        because one answered would keep the dead one.
        """
        draft = patch.get("add_model")
        settings = self._settings
        if settings is None or not isinstance(draft, (dict, list)) or not draft:
            return answer
        problems = answer.get("problems")
        if isinstance(problems, Mapping) and "add_model" in problems:
            return answer
        if isinstance(draft, list):
            rows = [item for item in draft if isinstance(item, dict)]
        else:
            rows = [draft]
        verdict: dict[str, object] = answer
        refused: dict[str, str] = {}
        for item in rows:
            name = str(item.get("name") or "")
            model = str(item.get("model") or "")

            def rollback(target: str = name) -> dict[str, object]:
                return settings.apply({"remove_model": target})

            verdict = self._gate(verdict, name, model, rollback)
            if not bool(verdict.get("ok", True)):
                refused[name] = str(verdict.get("error") or "连不上")
        if not refused:
            return verdict
        carried: dict[str, object] = {}
        existing = verdict.get("problems")
        if isinstance(existing, Mapping):
            carried.update({str(key): str(value) for key, value in existing.items()})
        # One row probed needs no label; several do -- including when only one of them
        # failed, or the operator is left reading "连不上" about a queue of three.
        carried["add_model"] = (
            next(iter(refused.values()))
            if len(rows) == 1
            else "；".join(f"{key} {value}" for key, value in refused.items())
        )
        applied = verdict.get("applied")
        surviving = dict(applied) if isinstance(applied, Mapping) else {}
        surviving.pop("add_model", None)
        return {**verdict, "applied": surviving, "problems": carried}

    def _gate(
        self,
        answer: dict[str, object],
        provider: str,
        model: str,
        rollback: Callable[[], dict[str, object]] | None,
    ) -> dict[str, object]:
        """One saved model row, checked against the network it claims to reach."""
        if self._model_probe is None:
            # No probe wired (a console run, a test double). Saying so beats the
            # alternative: saving whatever was typed and letting the first question find
            # out, which is the behaviour this gate exists to remove.
            logger.warning("no connectivity probe wired; saved %s/%s untested", provider, model)
            return {
                **answer,
                "ok": bool(_accepted(answer)),
                "warning": "这一版没接连通测试，模型未验证",
            }
        verdict = self._model_probe.test(provider, model)
        if bool(verdict.get("ok")):
            return {**answer, "ok": True, "probe": verdict}
        detail = str(verdict.get("detail") or "连不上")
        if rollback is not None:
            rollback()
        logger.info("refused to keep %s/%s: %s", provider, model, detail)
        return {**answer, "ok": False, "error": f"连不上，没保存：{detail}", "probe": verdict}

    def usage_summary(self, days: int = 7) -> dict[str, object]:
        """Token totals for the last ``days`` days, plus the per-day and per-model series.

        The window is clamped to one month inside the service, not here: the cap is
        a property of the ledger, and a second copy of the rule in the web layer is
        a rule that drifts. ``days = 0`` is the one value that is not a window at all --
        the whole ledger -- and it is answered here rather than filtered out, because the
        panel asks for it by that name.

        ``models`` travels with the totals because the two are read from the same bounds:
        a pie whose slices came from a different query than the number above it is the
        additivity bug wearing a chart.
        """
        if self._usage is None:
            return {"error": "用量统计不可用", "summary": None, "daily": [], "models": []}
        try:
            window = int(days)
        except (TypeError, ValueError):
            window = 7
        return {
            "error": "",
            "summary": self._usage.summary(window).to_dict(),
            "daily": self._usage.daily(window),
            "models": self._usage.by_provider(window),
            "span": self._usage.ledger_span(),
        }

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

    def attach_turns(self, registry: Any) -> None:
        """Hand the bridge the table of running turns.

        Attached rather than constructed here for the same reason the greeter is: the
        composition root owns the one instance, and the round table submits its work to
        that same table. Two registries would mean two answers to 「现在在跑什么」.
        """
        self._turns = registry
        registry.on_update = self._turn_changed
        self._push_conversations()

    # -- conversations and running turns ----------------------------------

    def chat_conversations(self) -> dict[str, object]:
        """Every tab: what it is called, which model it asks, and what it is doing."""
        if self._chat is None:
            return {"error": "对话服务不可用", "conversations": []}
        return {"error": "", "conversations": self._chat.conversations()}

    def chat_open(self, conversation_id: str) -> dict[str, object]:
        """Switch the panel to another conversation.

        The turns are re-read from the store because the live history of a tab that has
        never been on screen is not in the snapshot, and a panel that shows an empty
        conversation while its status says 「3 轮」 is a panel nobody believes.
        """
        if self._chat is None:
            return {"error": "对话服务不可用", "conversation": ""}
        answer = self._chat.focus(str(conversation_id))
        self._push_history(str(conversation_id))
        self._push_conversations()
        return answer

    def chat_new(self) -> dict[str, object]:
        """Open another tab. Nothing is stored until its first question."""
        if self._chat is None:
            return {"error": "对话服务不可用", "conversation": ""}
        answer = self._chat.new_conversation()
        self._push_conversations()
        return answer

    def chat_tab_model(
        self, conversation_id: str, provider: str = "", model: str = ""
    ) -> dict[str, object]:
        """Point one tab at one model, leaving the others where they are.

        This is the half that makes the round table and the multi-model comparison
        possible at all: a single global "current model" cannot describe three tabs,
        and the answer to 「这句是谁说的」 has to be knowable before it is asked.
        """
        if self._chat is None:
            return {"ok": False, "error": "对话服务不可用"}
        answer = self._chat.set_conversation_model(str(conversation_id), provider, model)
        self._push_conversations()
        return answer

    def chat_tasks(self) -> dict[str, object]:
        """The running and recently finished turns the 任务 strip draws."""
        if self._turns is None:
            return {"error": "任务表未启用", "tasks": []}
        return {"error": "", "tasks": [task.to_dict() for task in self._turns.recent()]}

    def chat_cancel(self, task_id: str) -> dict[str, object]:
        """Stop one turn. Returns whether anything was actually stopped.

        A refusal is a real answer here: the turn may have finished a moment ago, and a
        button that reports success either way teaches the operator not to trust it.
        """
        if self._turns is None:
            return {"ok": False, "error": "任务表未启用"}
        answer = dict(self._turns.cancel(str(task_id)))
        self._push_conversations()
        return answer

    def pet_action(self, name: str) -> dict[str, object]:
        """An icon on one of her cards was pressed. Both doors reach the same code.

        ``stop`` ends the conversation she is speaking for -- the same registry call the
        panel's button makes, because a second way to cancel would be a second way to
        disagree about what got cancelled. ``add`` brings the dashboard forward with the
        cursor already in the box: her own page is a renderer parked off screen, so a text
        field drawn *there* would be a field nobody can see, let alone type into.

        Refusions are logged as well as returned. A button on the desktop that does
        nothing and says nothing is the failure the operator describes as "她没反应".
        """
        if name == BUBBLE_STOP:
            return self._pet_stop()
        if name == BUBBLE_ADD:
            return self._pet_raise_input()
        logger.warning("气泡上按了没人认识的东西：%s", name)
        return {"ok": False, "error": f"不认识的按钮：{name}"}

    def _pet_stop(self) -> dict[str, object]:
        turns = self._turns
        if turns is None:
            return {"ok": False, "error": "任务表未启用"}
        answer = dict(turns.cancel_for_conversation(self._active_tab()))
        if not answer.get("ok"):
            logger.warning("气泡上的停止没停掉任何东西：%s", answer.get("error"))
        self._push_conversations()
        return answer

    def _pet_raise_input(self) -> dict[str, object]:
        lifecycle = self._lifecycle
        shown = bool(lifecycle is not None and lifecycle.show())
        window = self._window
        if window is None:
            return {"ok": False, "error": "没有可聚焦的对话面板"}
        try:
            window.evaluate_js(FOCUS_INPUT_SCRIPT)
        except Exception:  # pragma: no cover - a window that will not run script
            logger.debug("the input focus cue did not land", exc_info=True)
            return {"ok": False, "error": "面板没能收到聚焦指令"}
        return {"ok": True, "shown": shown, "error": ""}

    def chat_send(
        self,
        text: str,
        attachments: object = (),
        conversation: str = "",
        provider: str = "",
        model: str = "",
    ) -> dict[str, object]:
        """Start one turn and return immediately with its id.

        The answer arrives through the state snapshot instead: the page keeps typing,
        another tab can ask its own question, and 停止 has something to point at. The
        question is put on screen here rather than after the reply, because "she went
        quiet for twenty seconds" is indistinguishable from "it hung" otherwise.
        """
        return self._start_turn(
            text,
            attachments,
            str(conversation or ""),
            str(provider or ""),
            str(model or ""),
            wait=False,
        )

    def _start_turn(
        self,
        text: str,
        attachments: object,
        conversation: str,
        provider: str,
        model: str,
        *,
        wait: bool,
    ) -> dict[str, object]:
        """One engine behind both doors: the page's ``chat_send`` and ``chat_ask``.

        ``chat_ask`` has to keep answering in the same call for the phone's RPC and for
        anything announcing a result. Routing it through the same table as the page is
        the point: if the two paths grew separately, the phone would keep the old
        single-conversation behaviour and nobody would notice until a operator ran both.
        """
        service = self._chat
        if service is None:
            return {"question": text, "answer": "", "error": "对话服务未启用"}
        payload = attachments if isinstance(attachments, list) else []
        target = conversation or service.session_id
        state = self._state
        if state is not None:
            # Both, and in this order: the question is on screen before she says she is
            # working on it, so "thinking" always has something above it to be about.
            state.add_turn("user", text)
            state.set_turn(UiVoiceState.PROCESSING)
        if self._turns is None:
            # No table wired (the console path, or a test double): answer inline. The
            # page still gets its turn, it just cannot be interrupted or listed.
            reply = service.ask(
                text,
                attachments=payload,
                conversation=target,
                provider=provider,
                model=model,
            )
            self._finish_turn(reply, target, "")
            return reply.to_dict()

        answer = self._turns.start(
            conversation_id=target,
            kind=KIND_CHAT,
            question=text,
            participants=(f"{provider}/{model}" if provider else ()),
            work=lambda context: self._drive_turn(
                context, service, text, payload, target, provider, model
            ),
        )
        self._push_conversations()
        if not wait:
            return {
                "ok": True,
                "task_id": answer.task_id,
                "conversation": target,
                "question": text,
                "answer": "",
                "error": "",
            }
        finished = self._turns.wait(answer.task_id, SYNC_TURN_TIMEOUT_SECONDS)
        with self._result_lock:
            stored = self._results.pop(answer.task_id, None)
        if not finished:
            return {
                "question": text,
                "answer": "",
                "error": f"这一轮超过 {int(SYNC_TURN_TIMEOUT_SECONDS)} 秒还没有结束",
            }
        if stored is None:
            return {"question": text, "answer": "", "error": "这一轮没有带回结果"}
        return stored

    def _drive_turn(
        self,
        context: Any,
        chat: ChatService,
        text: str,
        payload: list[object],
        conversation: str,
        provider: str,
        model: str,
    ) -> dict[str, object]:
        """Ask, forwarding increments to the page, on the turn's own thread.

        Takes the service as an argument instead of re-reading ``self._chat``: the table
        holds this closure for as long as the turn runs, and a service that went away
        mid-answer would otherwise be dereferenced on a thread nobody is watching.
        """

        def delta(partial: str) -> None:
            if self._state is not None and context.conversation_id == self._active_tab():
                self._state.set_stream(
                    context.conversation_id,
                    context.task_id,
                    partial,
                    model=model,
                )

        reply = chat.ask(
            text,
            # The page's own attachment list, typed as the mapping the service cleans.
            # Validating it here would be a second copy of rules that live in one place.
            attachments=[item for item in payload if isinstance(item, dict)],
            conversation=conversation,
            provider=provider,
            model=model,
            task_id=context.task_id,
            on_delta=delta,
            on_phase=self._phase_reporter(context),
            should_stop=context.should_stop,
        )
        self._finish_turn(reply, conversation, context.task_id)
        with self._result_lock:
            self._results[context.task_id] = reply.to_dict()
            self._prune_results()
        return {
            "answer_chars": len(reply.answer),
            "tools_used": reply.tools_used,
            "error": reply.error,
            "state": STATE_CANCELLED if reply.cancelled else STATE_DONE,
        }

    def _phase_reporter(self, context: Any) -> Callable[[str], None]:
        """Route a phase the service just entered into the task table.

        One direction only: the table announces, the bridge re-publishes the strip. Going
        the other way -- the strip reading conversations on a timer -- is how a status
        line ends up lagging the thing it describes.
        """

        def report(phase: str) -> None:
            if self._turns is not None:
                self._turns.set_phase(context.task_id, phase)

        return report

    def _finish_turn(self, reply: Any, conversation: str, task_id: str) -> None:
        """Put the answer where the operator can read it, once, wherever it lands.

        A background tab's answer is not pushed into the visible history: the panel
        shows one conversation at a time, and interleaving another tab's reply into it
        would be the multi-conversation equivalent of the bug this whole change removed.
        """
        state = self._state
        if state is not None:
            state.clear_stream(task_id)
            if conversation == self._active_tab() and reply.answer:
                state.add_turn(
                    "assistant", reply.answer, reply.reasoning, reply.model, reply.record
                )
            if self._turns is None or not self._turns.waiting():
                state.set_turn(UiVoiceState.IDLE)
        self._push_conversations()
        if reply.answer and self._should_read_aloud():
            # Fire and forget, and deliberately *after* the answer is on screen: a voice
            # is a second copy of something the operator can already read.
            self._speak(reply.answer)

    def _turn_changed(self, info: Any) -> None:
        """A turn started, changed phase, or finished: refresh what the strip shows.

        The registry is also what tells the pet she is thinking. Driving that off the
        table rather than off the ask path is the difference between an indicator that
        comes off and one that hangs: every way out of a turn -- answer, cancel, raised
        exception -- ends in ``_finish``, and ``_finish`` announces.
        """
        live = bool(self._turns is not None and self._turns.waiting())
        if self._state is not None and info.state != STATE_RUNNING:
            # Retract the in-progress bubble on every way out, not only the good one.
            # ``_finish_turn`` clears it when an answer lands, but a turn that raises
            # never reaches that call -- and a leftover bubble with the *previous* text
            # in it suppresses the next turn's 「思考中」 (the panel only shows it while
            # nothing has arrived yet). That is the "有时候有有时候没有" the operator sees.
            self._state.clear_stream(str(getattr(info, "task_id", "") or ""))
            self._state.set_turn(UiVoiceState.PROCESSING if live else UiVoiceState.IDLE)
        pet = self._pet
        if pet is not None:
            pet.set_thinking(live)
            # 停止 only exists while there is something to stop; ＋ is always there,
            # because the next question does not wait for her to be busy to be wanted.
            pet.set_actions((BUBBLE_STOP, BUBBLE_ADD) if live else (BUBBLE_ADD,))
        self._push_conversations()

    def _active_tab(self) -> str:
        return self._chat.session_id if self._chat is not None else ""

    def _push_conversations(self) -> None:
        """Publish the tab strip: what the conversations say, plus what the table says.

        The overlay is the fix for a bubble that never appeared. A conversation marks
        itself running inside ``ask``, on the worker thread -- which is after every push
        the bridge makes, so the panel read "idle" across the whole twenty seconds and
        the 「思考中」 bubble conditioned on that status never rendered. The registry knows
        the turn exists the instant the request is accepted, so its running state wins.
        """
        if self._state is None or self._chat is None:
            return
        try:
            rows = self._chat.conversations()
            self._state.set_conversations(self._mark_running(rows))
        except Exception:  # pragma: no cover - a strip failure must not lose the answer
            logger.exception("could not publish the conversation strip")

    def _mark_running(self, rows: list[dict[str, object]]) -> list[dict[str, object]]:
        """Stamp every conversation that owes an answer, and stack up what it waits on.

        Two sources, in this order: a running turn supplies the ``phase`` the card shows,
        and the waiting questions ride along underneath as the stack the panel draws above
        its input box. This row is the only way that list reaches the page -- it does not
        poll the registry, and a second channel for the same list is a second channel that
        can fall behind.
        """
        if self._turns is None:
            return rows
        live: dict[str, Any] = {}
        for info in self._turns.running():
            live.setdefault(str(info.conversation_id), info)
        waiting: dict[str, list[dict[str, str]]] = {}
        for info in self._turns.queued():
            waiting.setdefault(str(info.conversation_id), []).append(
                {"task_id": str(info.task_id), "question": str(info.question)}
            )
        if not live and not waiting:
            return rows
        marked: list[dict[str, object]] = []
        for row in rows:
            conversation = str(row.get("id") or "")
            info = live.get(conversation)
            line = waiting.get(conversation)
            if info is None and not line:
                marked.append(row)
                continue
            stamped: dict[str, object] = {**row, "status": "running"}
            if info is not None:
                stamped["phase"] = str(info.phase)
                stamped["task_id"] = str(info.task_id)
            if line:
                stamped["queued"] = line
            marked.append(stamped)
        return marked

    def _push_history(self, conversation_id: str) -> None:
        if self._state is None or self._chat is None:
            return
        rows = self._chat.session_messages(str(conversation_id)).get("messages") or []
        self._state.replace_history(rows if isinstance(rows, list) else [])

    def _prune_results(self) -> None:
        """Keep the waiting callers' store bounded.

        Without this every turn of a long-running desktop adds a payload to a dict only
        ``chat_ask`` ever removes, and the one caller that reads it is the phone.
        """
        overflow = len(self._results) - MAX_KEPT_RESULTS
        for task_id in list(self._results)[: max(0, overflow)]:
            self._results.pop(task_id, None)

    def chat_ask(self, text: str, attachments: object = ()) -> dict[str, object]:
        """Answer one typed question and hand the reply back in the same call.

        The page has not used this since the tab strip arrived -- it calls ``chat_send``
        and watches the snapshot. It stays because the phone's RPC and the announcer ask
        and expect an answer inline, and because a synchronous door that runs through the
        *same* table as the asynchronous one is the only reason the two paths cannot
        drift into different notions of "which conversation is this".
        """
        return self._start_turn(text, attachments, "", "", "", wait=True)

    def _should_read_aloud(self) -> bool:
        """Whether a typed answer gets a voice on this press.

        The toggle lives in the settings store, not in the page's copy of it, so
        changing one's mind takes effect on the very next question.
        """
        return self._settings is not None and self._settings.speaks_typed()

    def _speak(self, text: str) -> None:
        voice = self._voice
        if voice is None:
            return
        try:
            voice.speak_text(text)
        except Exception:  # pragma: no cover - speak_text already guards
            logger.exception("read-aloud request failed; the text is still on screen")

    def audio_ready(self, ok: bool, reason: str = "", role: str = "hud") -> dict[str, object]:
        """The page's own verdict on whether it can play audio.

        Nothing is sent to the page before it says yes, and a page that says no --
        no ``AudioContext``, an autoplay policy it cannot satisfy -- gets its
        answers out of the speaker instead. The answer is echoed back so the caller
        can render which output it is, without a second round-trip to ``voice_status``.

        ``role`` is which page is answering. Two of them open an audio channel now --
        the dashboard, and the desktop figure whose mouth reads the same samples -- and
        only the current loudspeaker's answer may route the audio.
        """
        if self._audio is None:
            return {"output": "speaker", "reason": "本进程没有接通界面音频通道"}
        self._audio.mark_ready(bool(ok), str(reason or ""), str(role or "hud"))
        return self.audio_output()

    def audio_output(self) -> dict[str, object]:
        """Which device is playing the assistant, and why if it is not the page.

        Read by the page on mount, before its own ``audio_ready`` call lands, so a
        bundle that reloaded can find out where its audio went.
        """
        if self._audio is None:
            return {"output": "speaker", "reason": "", "level": 0.0}
        return {
            "output": "browser" if self._audio.ready else "speaker",
            "reason": self._audio.detail,
            "bytes_sent": self._audio.sent_bytes,
        }

    def speech_stop(self) -> dict[str, object]:
        """Stop talking now. The page's 「停下」 button; also flushes its own queue.

        Two halves because the samples are in two places: Python can stop producing
        more audio, and only the page can retract what it has already been given.
        Answering with just one of them would leave a paragraph playing after the
        button that says otherwise.
        """
        stopped = False
        if self._voice is not None:
            try:
                stopped = self._voice.stop_speaking()
            except Exception:  # pragma: no cover - stop_speaking already guards
                logger.exception("could not cancel the read-aloud on the Python side")
        flushed = False
        if self._audio is not None:
            flushed = self._audio.flush()
        return {"stopped": stopped or flushed}

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
        from jarvis import __version__, build_stamp

        # ``built`` is the whole point: the title line is the only place an
        # operator can tell this artifact from the four older ones still on disk.
        # ``tray`` belongs on the same line for the same reason: it answers "what
        # happens if I close this", and the answer changed with this build.
        lifecycle = self._lifecycle
        return {
            "name": "小夜",
            "version": __version__,
            "engine": "pywebview",
            "built": build_stamp(),
            "tray": bool(lifecycle is not None and lifecycle.hides_to_tray),
        }

    # -- 手机接入 --------------------------------------------------------

    def attach_mobile(self, gateway: MobileGateway | None) -> None:
        """The LAN endpoint this window can switch on.

        Attached, not constructed with the bridge: the gateway calls *into* the
        bridge for every whitelisted method, so neither can be an argument to the
        other's constructor.
        """
        self._mobile = gateway

    def mobile_state(self) -> dict[str, object]:
        """Listener status, address, fingerprint, paired devices, pairing countdown."""
        gateway = self._mobile
        if gateway is None:
            return {
                "running": False,
                "url": "",
                "devices": [],
                "pairing": {"active": False, "expires_in": 0, "attempts_left": 0},
                "error": "这个构建没有打开手机接入（缺少 cryptography 时也会这样）",
            }
        return gateway.state()

    def mobile_toggle(self, on: object = True) -> dict[str, object]:
        """Open or close the port. The 「手机接入」 switches both say which they mean.

        Answers with the state after the attempt rather than with what was asked: a
        bind that failed has to show as closed, or the operator believes there is a
        listener that is not there -- or worse, that a listener they closed is gone.
        """
        gateway = self._mobile
        if gateway is None:
            return self.mobile_state()
        want = bool(on)
        if want == gateway.running:
            return gateway.state()
        if want:
            gateway.enable()
        else:
            gateway.disable()
        return gateway.state()

    def mobile_pair_code(self) -> dict[str, object]:
        """Ask for a pairing code to read out to the phone."""
        gateway = self._mobile
        if gateway is None:
            return {"code": "", "error": "手机接入不可用"}
        return gateway.show_pair_code()

    def mobile_revoke(self, device_id: str) -> dict[str, object]:
        gateway = self._mobile
        if gateway is None:
            return {"revoked": False, "error": "手机接入不可用"}
        return gateway.revoke(str(device_id))

    def mobile_revoke_all(self) -> dict[str, object]:
        gateway = self._mobile
        if gateway is None:
            return {"revoked": 0, "error": "手机接入不可用"}
        return gateway.revoke_all()

    def mobile_selftest(self) -> dict[str, object]:
        """Prove from this process that a plaintext probe gets nothing back.

        The panel offers this because "only TLS" is a claim about a socket, and a
        claim about a socket is checkable in two seconds.
        """
        gateway = self._mobile
        if gateway is None:
            return {"plaintext_refused": True, "note": "手机接入不可用"}
        return gateway.selftest()


def _stats_of(service: Any) -> dict[str, object]:
    """``stats()`` from a component, or ``{}`` when this process has no such one.

    Never raises: a panel that opens onto three counters must not fail to open
    because one of them is missing -- the missing one is exactly what it is there to
    show.
    """
    if service is None:
        return {}
    try:
        return dict(service.stats())
    except Exception as exc:  # pragma: no cover - the services guard their own reads
        logger.exception("reading component stats failed")
        return {"error": str(exc)}


def _is_reminder(job: Any) -> bool:
    """Whether this scheduled job belongs to the reminders service."""
    return str(getattr(job, "job_id", "") or "").startswith(REMINDER_PREFIX)


def _reminder_refusal(job_id: object) -> str:
    """Why the automation tab will not touch this job, or ``""`` to go ahead.

    Two doors onto one row is two chances to disagree about whether it is on, so the
    write methods here answer with where to go instead of doing it.
    """
    if str(job_id or "").startswith(REMINDER_PREFIX):
        return "这条是提醒，请在「提醒」页签里改"
    return ""


def _job_row(service: JobSurface, job: Any) -> dict[str, object]:
    """One scheduled job, plus the two facts that each need a call of their own."""
    row = _as_row(job)
    job_id = str(row.get("job_id", ""))
    row["next_run"] = ""
    row["last"] = None
    try:
        row["next_run"] = str(service.next_run_time(job_id) or "")
    except Exception:  # pragma: no cover - a job APScheduler has already dropped
        logger.debug("no next run time for %s", job_id, exc_info=True)
    try:
        recent = list(service.history(job_id, limit=1))
    except Exception:  # pragma: no cover - history is best-effort
        logger.debug("no history for %s", job_id, exc_info=True)
        recent = []
    if recent:
        row["last"] = _as_row(recent[0])
    return row


def _as_row(item: object) -> dict[str, object]:
    """One row of something, in the shape JSON can carry.

    Reminder rows and memory records are dataclasses that know how to render
    themselves; the knowledge base returns database rows, which are mappings and do
    not. Handling both here is what stops every panel from inventing its own
    conversion -- and a panel that guesses usually guesses one key name wrong.
    """
    to_dict = getattr(item, "to_dict", None)
    if callable(to_dict):
        try:
            return dict(to_dict())
        except Exception:  # pragma: no cover - a broken value object
            logger.debug("row.to_dict() failed", exc_info=True)
    if isinstance(item, Mapping):
        return {str(key): value for key, value in item.items()}
    return {"text": str(item)}


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


@dataclass(frozen=True)
class ScreenBox:
    """The area a window may occupy, in the same units ``create_window`` takes."""

    x: int
    y: int
    width: int
    height: int


def primary_screen() -> ScreenBox | None:
    """The screen the HUD will open on, or ``None`` if nothing can be asked."""
    try:
        import webview

        screens = webview.screens()
    except Exception:  # pragma: no cover - a shell with no display to report
        logger.debug("could not read the screen size", exc_info=True)
        return None
    if not screens:
        return None
    box = screens[0]
    return ScreenBox(int(box.x), int(box.y), int(box.width), int(box.height))


def fit_to_screen(rect: WindowGeometry, screen: ScreenBox | None) -> WindowGeometry:
    """A floating window must fit on the screen it opens on.

    ``create_window(maximized=True)`` leaves WinForms with no smaller normal size of
    its own, so the first 还原 hands back the *maximised* extent -- measured here as
    1936x1048 at (208,208) on a 1920x1080 panel, which is a window with part of its
    title bar off the right edge and no way to reach it. Clamping is what makes
    "restore" mean something.
    """
    if screen is None:
        return rect
    width = min(rect.width, max(900, screen.width - 80))
    height = min(rect.height, max(600, screen.height - 120))
    left = min(max(rect.x, screen.x), screen.x + max(0, screen.width - width))
    top = min(max(rect.y, screen.y), screen.y + max(0, screen.height - height))
    return WindowGeometry(left, top, width, height, rect.maximized)


@dataclass(frozen=True)
class WindowGeometry:
    """Where the HUD window opens, and whether it fills the screen."""

    x: int
    y: int
    width: int
    height: int
    maximized: bool = False

    def to_mapping(self) -> dict[str, object]:
        return {
            "x": self.x,
            "y": self.y,
            "width": self.width,
            "height": self.height,
            "maximized": self.maximized,
        }


def _int_or(value: object, fallback: int) -> int:
    """An int out of a JSON blob, which may hold a float, a string or nothing.

    Written out by type rather than ``try: int(value)`` because a bool is an int in
    Python, and a stored ``{"width": true}`` would otherwise become a one-pixel
    window instead of falling back.
    """
    if isinstance(value, bool):
        return fallback
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return fallback
    return fallback


def _truthy(value: object) -> bool:
    """Whether a JSON argument meant "yes".

    JavaScript sends ``true``, but a form field sends ``"true"`` and a query
    string sends ``"1"``. Accepting only the bool would make the upload switch
    silently do nothing on whichever client sent a string, and the symptom -- a
    voice that is fast for nobody -- points nowhere near this line.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    if isinstance(value, (int, float)):
        return value != 0
    return False


def _call_failure(error: str) -> dict[str, object]:
    """The empty call reply, with one reason filled in.

    Written out so that every refusal on this path has the same *keys* as a
    success. A phone that renders ``audio`` and ``answer`` must not have to
    handle a reply that is missing them just because it failed -- a missing key
    and an empty one look the same on screen and very different in the console.
    """
    return _call_reply({"error": error})


def _call_reply(result: Mapping[str, object]) -> dict[str, object]:
    """Turn a :class:`~jarvis.app.voice_call.VoiceCall` result into an RPC reply.

    Two conversions, and both are about the wire rather than about meaning:
    ``audio`` is raw s16le PCM that JSON cannot carry, so it leaves as base64,
    and ``None`` becomes ``""`` so the phone tests one falsy value instead of
    two. Everything else -- the heard text, the answer, whether it was a
    truncated reading -- passes through untouched, because the screen shows it
    verbatim.
    """
    audio = result.get("audio")
    encoded = ""
    if isinstance(audio, bytes) and audio:
        encoded = base64.b64encode(audio).decode("ascii")
    return {
        "error": result.get("error", ""),
        "heard": result.get("heard", ""),
        "answer": result.get("answer", ""),
        "spoken": result.get("spoken", ""),
        "sample_rate": _int_or(result.get("sample_rate"), 0),
        "truncated": bool(result.get("truncated")),
        "audio": encoded,
    }


def stored_geometry(
    preferences: Preferences | None,
    *,
    width: int,
    height: int,
) -> WindowGeometry | None:
    """The rect this window was last closed at, if there is one.

    ``None`` means "never remembered", which is not the same as "remembered, and it
    was 1280x800": the first launch of a build on a big monitor should fill the
    screen rather than repeat the size the code was written with.
    """
    if preferences is None:
        return None
    raw = preferences.get(WINDOW_RECT)
    if not isinstance(raw, dict):
        return None
    return WindowGeometry(
        x=_int_or(raw.get("x"), 80),
        y=_int_or(raw.get("y"), 60),
        width=max(900, _int_or(raw.get("width"), width)),
        height=max(600, _int_or(raw.get("height"), height)),
        maximized=bool(raw.get("maximized")),
    )


class _GeometryKeeper:
    """Remembers the window's *normal* rect, and that the operator left it maximised.

    Two things make this more than ``preferences.set(json.dumps(rect))``:

    * A maximised window's width and height are the screen's. Saving those as the
      restore size means the next launch opens "restored" at full-screen extent, and
      the operator can never get their old window back. So the rect is captured on
      the events that fire while the window is a normal window, and the maximised
      flag is carried separately.
    * Every method here runs on a GUI or timer thread while the window is closing.
      A geometry write must never be the reason the microphone was not released, so
      nothing in here raises.
    """

    def __init__(
        self,
        window: Any,
        preferences: Preferences | None,
        initial: WindowGeometry,
        screen: ScreenBox | None = None,
    ) -> None:
        self._window = window
        self._preferences = preferences
        self._screen = screen
        self._maximized = initial.maximized
        self._want_maximized = initial.maximized
        self._normal = WindowGeometry(initial.x, initial.y, initial.width, initial.height)

    @property
    def maximized(self) -> bool:
        return self._maximized

    def capture(self) -> None:
        """Record the current rect, unless the window is maximised right now.

        A rect larger than the screen is not recorded as the normal size -- it is
        corrected to one that fits, because that is what the operator asked for when
        they pressed 还原.
        """
        if self._maximized:
            return
        try:
            window = self._window
            seen = WindowGeometry(
                int(window.x),
                int(window.y),
                int(window.width),
                int(window.height),
            )
        except Exception:  # pragma: no cover - a GUI thread that cannot answer
            logger.debug("window geometry unavailable", exc_info=True)
            return
        fitted = fit_to_screen(seen, self._screen)
        self._normal = WindowGeometry(fitted.x, fitted.y, fitted.width, fitted.height)
        if (fitted.width, fitted.height, fitted.x, fitted.y) != (
            seen.width,
            seen.height,
            seen.x,
            seen.y,
        ):
            self._place(fitted)

    def _place(self, rect: WindowGeometry) -> None:
        """Move and size the window to ``rect``. Best-effort, and never raises."""
        window = self._window
        try:
            window.resize(rect.width, rect.height)
            window.move(rect.x, rect.y)
        except Exception:  # pragma: no cover - a GUI that will not move
            logger.debug("could not fit the restored window to the screen", exc_info=True)

    def set_maximized(self, value: bool) -> None:
        self._maximized = value
        self.capture()

    def ensure_maximized(self) -> None:
        """Re-assert the maximised start once the page has actually loaded.

        ``create_window(maximized=True)`` is applied before the window is shown, and
        the WinForms show sequence can restore it again afterwards -- measured here:
        the first capture after launch was full-screen, a later one was the 1280x800
        restore size, with nothing in between touching the window. Asking once on
        ``loaded`` is the version of "maximised" that survives its own startup.
        """
        if not self._want_maximized or self._maximized:
            return
        self._want_maximized = False
        try:
            self._window.maximize()
        except Exception:  # pragma: no cover - a GUI that will not move
            logger.debug("could not maximise the window on load", exc_info=True)
        self.set_maximized(True)

    def save(self) -> None:
        if self._preferences is None:
            return
        try:
            stored = self._normal.to_mapping() | {"maximized": self._maximized}
            self._preferences.set(WINDOW_RECT, stored)
        except Exception:  # pragma: no cover - preferences are best-effort here
            logger.warning("could not remember the window geometry", exc_info=True)


def _watch_geometry(
    window: Any,
    preferences: Preferences | None,
    initial: WindowGeometry,
    screen: ScreenBox | None = None,
) -> _GeometryKeeper:
    """Wire the keeper to the window's own events."""
    keeper = _GeometryKeeper(window, preferences, initial, screen)
    events = getattr(window, "events", None)
    if events is None:  # pragma: no cover - a pywebview without its event box
        return keeper
    for name, handler in (
        ("resized", keeper.capture),
        ("moved", keeper.capture),
        ("restored", lambda: keeper.set_maximized(False)),
        ("maximized", lambda: keeper.set_maximized(True)),
        ("loaded", keeper.ensure_maximized),
    ):
        listener = getattr(events, name, None)
        if listener is not None:
            listener += handler
    events.closed += keeper.save
    return keeper


def run(
    system: SystemService,
    disk: DiskService,
    *,
    voice: VoiceService | None = None,
    chat: ChatService | None = None,
    usage: UsageService | None = None,
    settings: SettingsService | None = None,
    alerts: Any | None = None,
    audio: AudioPusher | None = None,
    tools: ToolLedger | None = None,
    voice_picker: VoicePicker | None = None,
    voice_library: VoiceLibrary | None = None,
    voice_call: VoiceCall | None = None,
    voice_sample: Any | None = None,
    computer_access: ComputerAccess | None = None,
    command_access: CommandAccess | None = None,
    process: ProcessService | None = None,
    bridge_sink: Callable[[UiState], None] | None = None,
    width: int = 1280,
    height: int = 800,
    preferences: Preferences | None = None,
    tray_enabled: bool = True,
    instance_lock: Path | None = None,
    instance_gate: InstanceGate | None = None,
    reminders: ReminderService | None = None,
    executable: Path | None = None,
    hotkeys_enabled: bool = True,
    announcer: Announcer | None = None,
    memory: MemorySurface | None = None,
    knowledge: KnowledgeSurface | None = None,
    scheduler: JobSurface | None = None,
    workflow: WorkflowSurface | None = None,
    planner: PlannerSurface | None = None,
    mobile: MobileSection | None = None,
    mobile_dir: Path | None = None,
    greeter: Any = None,
    turns: Any = None,
    model_probe: ModelProber | None = None,
    debug: bool = False,
) -> int:
    """Open the HUD window and block until it closes.

    Args:
        bridge_sink: Where live state goes. Defaults to pushing into the page;
            tests pass a collector so the whole wiring can be exercised without a
            browser.
        audio: The PCM channel feeding the page's own audio graph. Attached to the
            window here and detached on the way out; the composition root built it
            earlier, because the voice stack needs to be pointed at it before this
            function has a window to talk about.
        preferences: Remembers the window's size and position. Without it the window
            still works, it just opens the same size forever.
        tray_enabled: Put an icon in the notification area and make the X hide
            instead of quitting. ``--no-tray`` turns this off, which is also what
            happens by itself when the icon cannot be created.
        instance_gate: A gate the caller has already claimed. The composition root
            claims *before* starting the scheduler and the workflow engine, because a
            second copy must not bring those up at all: two sets of persistent jobs
            racing to fire the same reminder is a worse outcome than a late window.
            Claiming twice is a no-op, so handing it in here is what keeps the two
            checks in step instead of binding a second port. ``instance_lock`` is the
            fallback for callers (tests, mostly) that have no gate of their own.
        mobile: The ``mobile.*`` config block. ``None`` or ``enabled: false`` leaves
            the machine with **no listening port at all**: the LAN endpoint is built
            but not started, and only 「手机接入」 opens it.
        mobile_dir: Where the TLS key and the paired-device list live. Without it
            there is no persistence to offer, so 「手机接入」 is not even on the menu.
    """
    index = index_path()
    problem = bundle_hint()
    if problem:
        logger.error("desktop bundle unusable: %s", problem)
        print(BUILD_HINT.format(path=index), flush=True)
        return 1

    # Before anything is opened -- a port, a window, the microphone. With the tray
    # in the picture a second double-click is the *expected* thing for an operator
    # to do when they cannot see the app, and two copies means two icons and one
    # fight over the capture device. The winner continues; the newcomer wakes the
    # first window and leaves.
    gate = instance_gate
    if gate is None and instance_lock is not None:
        gate = InstanceGate(instance_lock)
    if gate is not None and not gate.claim():
        logger.info("another 小夜 is already running; this process is only the alarm clock")
        print("小夜已在运行：已把它的前台窗口叫回来，这次启动直接退出。", flush=True)
        return 0

    import webview

    # Served, not opened from disk: see the module docstring. Binding can fail
    # (an exhausted port range, a locked-down machine), and that has to be a
    # readable message rather than a window with nothing in it.
    server = BundleServer(WEB_DIR)
    try:
        base_url = server.start()
    except OSError as exc:
        logger.exception("could not serve the HUD bundle")
        print(f"界面服务启动失败：{exc}", flush=True)
        return 1

    state = StateBridge()
    window_box: list[Any] = []
    sink = bridge_sink or (lambda snapshot: _deliver(window_box, snapshot))
    pump = UiEventPump(sink)
    unsubscribe = state.subscribe(pump.submit)

    # The tray and the hide-instead-of-quit policy, built before the window exists
    # so the window can be born with its closing handler already wired. The
    # lifecycle owns both directions from here: pystray's thread calls into it, and
    # it is the only thing that decides whether the X hides or quits.
    def _own_audio(window: Any) -> None:
        """Give the answer to exactly one window, because two would echo.

        This was the missing wire for a whole release: ``ShellLifecycle`` knew how to
        decide who speaks, and nothing ever told the decision who to tell. The pet
        stayed still while the HUD held the samples, and the tray-hands-over rule that
        the lifecycle implements was true only in the tests.

        The role travels with it: the page that is playing answers for itself, and a
        claim from the other one must not reroute the sound.
        """
        if audio is None:
            return
        from jarvis.ui.audio_bridge import ROLE_HUD, ROLE_PET

        owner = ROLE_PET if (pet is not None and window is pet.audio_window) else ROLE_HUD
        audio.attach_window(window, owner)

    def _watch_audio(window: Any) -> None:
        """The window that may see the voice but must not play it: the desktop figure.

        Same samples, gain closed, so her mouth and the answer are still one signal.
        """
        if audio is not None:
            audio.attach_spectator(window)

    lifecycle = ShellLifecycle(on_audio_window=_own_audio, on_spectator_window=_watch_audio)
    # The gateway is built after the bridge (it calls into it), but the tray menu is
    # built before. A late-bound holder is the whole trick: pystray asks the tick and
    # the callback when the menu is *opened*, by which time the bridge exists.
    mobile_box: list[Any] = []
    tray = TrayIcon(
        on_activate=lifecycle.activate,
        on_quit=lifecycle.quit,
        on_toggle_pet=lifecycle.toggle_pet,
        pet_shown=lifecycle.pet_shown,
        on_toggle_autostart=(lambda: _flip_autostart(executable, tray)) if executable else None,
        autostart_on=(
            (lambda: bool(executable and autostart.enabled(executable)))
            if executable
            else (lambda: False)
        ),
        on_toggle_mobile=(lambda: _flip_mobile(mobile_box, tray)) if mobile_dir else None,
        mobile_on=lambda: bool(mobile_box and mobile_box[0].running),
    )
    if tray_enabled and not tray.start():
        logger.warning("tray unavailable (%s); the X will quit as it used to", tray.detail)
    lifecycle.attach_tray(tray)
    if announcer is not None and tray.running:
        # The tray is the only audience when the window is hidden, so a reminder that
        # comes due then still has to land somewhere the operator can see.
        announcer.bind(notify=tray.notify)
    if gate is not None:
        # Now that there is a window to bring back: the next double-click arrives
        # here instead of starting a second assistant.
        gate.on_activate = lifecycle.activate
        gate.start()
    if voice is not None:
        voice.subscribe(_fan_out(state, audio, lifecycle))
        # After the subscribe, not before: a launch-time microphone opening is an
        # event like any other, and one emitted while nobody is listening would
        # leave the page showing 「未启用」 over a live capture thread. The shell
        # only pulls the trigger -- whether to re-arm is the service's decision.
        voice.arm_if_remembered()
        # The event stream only says something when the phase *changes*, and a
        # window that opens after that change would leave the icon claiming
        # 「麦克风未开启」 over an armed microphone. Seed it from the service.
        lifecycle.set_voice_running(getattr(voice.status.phase, "value", "") == VoicePhase.RUNNING)

    # The page plays the assistant's voice, and a fresh webview has never been
    # clicked -- there is no gesture behind the first answer of a session, and
    # Chromium's autoplay policy would keep the audio context suspended. A voice
    # assistant that needs the operator to click something before it can talk has
    # not solved the problem it was built for, so the window is launched with that
    # one policy relaxed. Scoped to this process, and to nothing else.
    allow_autoplay()

    # create_window registers the window with pywebview's own loop; the handle is
    # kept only so the pump can push state into it once it exists.
    bridge = HudBridge(
        system,
        disk,
        voice=voice,
        chat=chat,
        state=state,
        usage=usage,
        settings=settings,
        audio=audio,
        tools=tools,
        voice_picker=voice_picker,
        voice_library=voice_library,
        voice_call=voice_call,
        voice_sample=voice_sample,
        computer_access=computer_access,
        command_access=command_access,
        process=process,
        lifecycle=lifecycle,
        reminders=reminders,
        announcer=announcer,
        memory=memory,
        knowledge=knowledge,
        scheduler=scheduler,
        workflow=workflow,
        planner=planner,
        model_probe=model_probe,
        alerts=alerts,
    )
    gateway = build_mobile_gateway(bridge, mobile, mobile_dir)
    if turns is not None:
        # Attached after construction like the pet and the greeter: the table exists from
        # the composition root, and the bridge is the thing that renders it.
        bridge.attach_turns(turns)
    if gateway is not None:
        mobile_box.append(gateway)
        bridge.attach_mobile(gateway)
        if mobile is not None and mobile.enabled:
            # The operator wrote ``enabled: true`` into config.yaml, so the port
            # opens with the app. Say so in the log: a listener that starts because
            # of a file nobody re-reads is the kind of thing an audit asks about.
            if gateway.enable():
                logger.warning("mobile endpoint opened at launch (mobile.enabled=true)")
            else:
                logger.warning("mobile.enabled=true but the port did not open: %s", gateway.reason)
    # A first launch fills the screen. The 1280x800 in the signature is the size a
    # *restored* window falls back to, not what a person on a 1920x1080 panel should
    # be handed every morning -- "页面还没铺满" was the complaint, and the code that
    # opened the window had never once looked at the screen it was opening on.
    screen = primary_screen()
    initial = fit_to_screen(
        stored_geometry(preferences, width=width, height=height)
        or WindowGeometry(x=80, y=60, width=width, height=height, maximized=True),
        screen,
    )
    window = webview.create_window(
        title="小夜",
        url=base_url,
        js_api=bridge,
        width=initial.width,
        height=initial.height,
        # A position and a maximised state are asked for together only by someone
        # who has not tried it: the WinForms backend applies the location after the
        # window state, which restores the window. Measured, not guessed -- the same
        # build came up full-screen once and 1280x800 the next launch. So a maximised
        # start asks for no position, and the remembered rect is used as-is only when
        # the operator actually left the window floating.
        x=None if initial.maximized else initial.x,
        y=None if initial.maximized else initial.y,
        maximized=initial.maximized,
        min_size=(900, 600),
        background_color="#04070d",
        text_select=True,
    )
    pet = PetController(
        base_url=base_url,
        bridge=bridge,
        # Any visibility change can move the microphone's answer to another window;
        # the lifecycle owns that rule and this is how it applies it.
        on_active_change=lambda _shown: lifecycle.set_audio_owner(),
        # Her cards are drawn by Python, so the press on one arrives here rather than
        # through a page. Same handlers the panel's own buttons call.
        on_action=bridge.pet_action,
    )
    lifecycle.attach_pet(pet)
    bridge.attach_pet(pet, remember=_remember_pet_choice(preferences))
    bridge._sync_surface_switches()
    if greeter is not None:
        # Two roles, one object: the lifecycle arms the wait when this wake summons the
        # figure, and ``pet_arrived`` releases it when the page says she is fully drawn.
        lifecycle.attach_greeter(greeter)
        bridge.attach_greeter(greeter)
    # The HUD is not the only window that shows voice state -- she does too, and hers
    # is created on demand rather than at start-up. Tracking it from the pet's own
    # show/hide is what makes the tray item and the button on the bar behave like the
    # remembered-on-launch case; before this, only that one path registered her, so
    # every other way of opening her produced a window nobody pushed state into and
    # her listening pose never moved.
    pet.observe_window(_pet_window_tracker(window_box))
    # Her speech bubble. The transcript is the one waist that both the microphone and
    # a typed question already write through, so subscribing here covers both paths
    # without teaching either of them that a desktop figure exists.
    state.subscribe(pet.on_snapshot)
    keeper = _watch_geometry(window, preferences, initial, screen)
    bridge.attach_window(window, keeper)
    if window is not None:
        lifecycle.attach_window(window)
    events = getattr(window, "events", None)
    if events is not None:
        # The one handler in this file whose return value means something: ``False``
        # cancels the close and the window is hidden instead. See
        # :meth:`jarvis.ui.lifecycle.ShellLifecycle.on_closing`.
        events.closing += lifecycle.on_closing
        # Tracked rather than queried: pywebview exposes no "is this window
        # minimised" getter, and clicking the tray icon has to undo a minimise
        # without also undoing a 「铺满」.
        events.minimized += lifecycle.on_minimized
        events.restored += lifecycle.on_restored
    window_box.append(window)
    if audio is not None:
        audio.attach_window(window)
    hotkeys = Hotkeys(
        default_bindings(
            on_talk=_talk_chord(lifecycle, voice),
            on_toggle=lifecycle.toggle,
        )
    )
    if hotkeys_enabled and not hotkeys.start():
        logger.info("global hotkeys are not up (%s)", "；".join(hotkeys.failures) or "已关闭")
    pump.start()
    _watch_for_blank_window(window)
    # The pet is remembered, so a launch that had it on the desktop gets it back --
    # after the page has loaded, because creating a window is only safe once the
    # message loop is running (and ``loaded`` handlers run off the UI thread).
    if preferences is not None and bool(preferences.get(PET_ENABLED)):
        _restore_pet_on_load(window, pet)
    logger.info(
        "opening desktop window (%sx%s at %s,%s, maximized=%s) at %s",
        initial.width,
        initial.height,
        initial.x,
        initial.y,
        initial.maximized,
        base_url,
    )
    try:
        # gui="edgechromium" pins the WebView2 backend instead of letting pywebview
        # fall back to Qt/GTK, which are not installed here and would fail obscurely.
        webview.start(debug=debug, gui="edgechromium")
    finally:
        # Reaching here means the message loop ended, which since the tray exists
        # only happens after a real quit: the X hides, and the only door out is the
        # tray menu destroying every window. The icon goes first so a teardown that
        # takes a second does not leave a clickable ghost on the taskbar.
        lifecycle.shutdown()
        hotkeys.stop()
        unsubscribe()
        pump.stop()
        if audio is not None:
            audio.stop()
        if voice is not None:
            voice.stop()
        if gateway is not None:
            # Before the bundle server, and never left to "the process is exiting":
            # the phone's certificate and device list are on disk, but the listener
            # is a live door, and a door the app knows how to open has to be a door
            # the app closes on the way out.
            gateway.disable()
        server.stop()
        if gate is not None:
            gate.release()
    return 0


def _talk_chord(lifecycle: ShellLifecycle, voice: VoiceService | None) -> Callable[[], object]:
    """What Ctrl+Alt+K does: come out, then ask for one spoken turn.

    The window comes first because the chord is how you use the assistant where you
    cannot be heard -- and a turn that is being listened to with no level meter on
    screen is a guess. talk() still refuses when the microphone was never
    consented to, and that refusal is shown rather than swallowed.
    """

    def fire() -> object:
        lifecycle.activate()
        return voice.talk() if voice is not None else None

    return fire


def _flip_autostart(executable: Path | None, tray: TrayIcon) -> None:
    """The tray's 「开机自启」 tick: write, then read the key back."""
    if executable is None:
        return
    wanted = not autostart.enabled(executable)
    now, error = autostart.set_enabled(executable, wanted)
    tray.notify("开机自启已" + ("打开" if now else "没打开") + (f"：{error}" if error else ""))


def build_mobile_gateway(
    bridge: HudBridge, mobile: MobileSection | None, directory: Path | None
) -> MobileGateway | None:
    """The LAN endpoint, or ``None`` when this build cannot offer one.

    Both the config section and a directory have to be there. Falling back to
    invented defaults would put a port number in the pairing screen that nothing
    in the configuration agrees with, and the operator would be dialling a number
    only this function knows.
    """
    if mobile is None or directory is None:
        return None
    from jarvis import __version__
    from jarvis.ui.lan_server import MobileGateway

    vault = PairingVault(
        directory / "devices.json",
        pair_minutes=mobile.pair_minutes,
        max_pair_attempts=mobile.max_pair_attempts,
        max_devices=mobile.max_devices,
    )
    return MobileGateway(
        bridge,
        vault,
        directory,
        bind_host=mobile.bind_host,
        port=mobile.port,
        version=__version__,
    )


def _flip_mobile(box: list[Any], tray: TrayIcon) -> None:
    """The tray's 「手机接入」: flip the port, then say what it actually did.

    The address and the fingerprint go into the balloon because that is the moment
    a person is holding a phone and needs to type something. Both are safe to show
    on the operator's own screen; neither is ever served to an unauthenticated
    client.
    """
    gateway = box[0] if box else None
    if gateway is None:
        tray.notify("手机接入不可用：这个构建没有接通局域网服务")
        return
    if gateway.toggle():
        state = gateway.state()
        short = str(state["fingerprint"])[:19]
        tray.notify(f"手机接入已打开：{state['url']}\n证书指纹 {short}…（界面里有全部）")
        return
    tray.notify(f"手机接入已关闭。{gateway.reason}".rstrip())


def _remember_pet_choice(
    preferences: Preferences | None,
) -> Callable[[bool], None] | None:
    """The one writer of 「桌面宠物」 into the preference file."""
    if preferences is None:
        return None

    def remember(value: bool) -> None:
        preferences.set(PET_ENABLED, bool(value))

    return remember


def _pet_window_tracker(window_box: list[Any]) -> Callable[[Any | None], None]:
    """The observer that keeps the push list in step with the pet window.

    Split out of ``run`` so it can be exercised without a browser: the bug it fixes
    was invisible for exactly that reason -- the only way to see it was to open the
    pet by hand and notice a pose that never moved.

    Reassigning the slice rather than rebinding the name matters: the state sink
    captured this list object, so a new list would silently stop being the one that
    gets pushed into.
    """
    pet_windows: set[int] = set()

    def observe(target: Any | None) -> None:
        if target is None:
            # Hidden: stop feeding it. The window object survives for the next show,
            # but state pushed at a window nobody can see is work with no reader.
            window_box[:] = [item for item in window_box if id(item) not in pet_windows]
            pet_windows.clear()
            return
        if any(id(item) == id(target) for item in window_box):
            return
        window_box.append(target)
        pet_windows.add(id(target))

    return observe


def _restore_pet_on_load(window: Any, pet: PetController) -> None:
    """Show the pet on start-up when the operator left it showing.

    One-shot: ``loaded`` also fires on a page reload, and a pet that pops back over
    the window the operator just dismissed is a bug report waiting to be filed.

    Registering the window is not done here any more: ``PetController.show`` reports
    it through its own observer, which is what makes this path and the tray item the
    same thing instead of two implementations that have to be kept in step.
    """
    events = getattr(window, "events", None)
    if events is None:  # pragma: no cover - a pywebview without its event box
        return
    pending = {"want": True}

    def on_loaded() -> None:
        if not pending["want"]:
            return
        pending["want"] = False
        pet.show(emerge=True)

    events.loaded += on_loaded


def _fan_out(
    state: StateBridge,
    audio: AudioPusher | None,
    lifecycle: ShellLifecycle | None = None,
) -> Callable[[Any], None]:
    """One subscriber that feeds both the transcript and the audio channel.

    The wake-word half of the pair is what makes interrupting *work*: by the time
    the operator starts talking over an answer, its samples have already crossed
    into the page, and nothing on the Python side can take them back.
    """

    def deliver(event: Any) -> None:
        state.push_event(event)
        if audio is not None:
            audio.on_event(event)
        if lifecycle is not None:
            # The tray icon is the only indicator left once the window is hidden, and
            # an icon that keeps claiming 「正在听」 after 释放麦克风 is a claim about
            # the operator's microphone, not about this app.
            lifecycle.on_event(event)

    return deliver


AUTOPLAY_ARGUMENTS = "--autoplay-policy=no-user-gesture-required"
"""The single Chromium switch the HUD needs, and the reason it is named here."""


def allow_autoplay(environ: dict[str, str] | None = None) -> str:
    """Ask WebView2 to let the assistant speak without a prior click.

    Returns the value written, so a caller can assert what the child process will
    have seen. An operator-supplied value is left alone: whoever set
    ``WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS`` by hand (to debug the sandbox, say)
    knows more about that window than this function does, and overwriting their
    flags to fix an audio problem would be a rude way to solve it.
    """
    target = environ if environ is not None else os.environ
    current = target.get("WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS", "")
    if AUTOPLAY_ARGUMENTS in current:
        return current
    merged = f"{current} {AUTOPLAY_ARGUMENTS}".strip()
    target["WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS"] = merged
    return merged


def _watch_for_blank_window(window: Any) -> None:
    """Warn if the page never finishes loading.

    Runs on a daemon thread so it cannot hold the process open, and only ever
    logs: a window that loads slowly is not an error worth aborting for, and the
    one thing the user must not get here is silence.
    """
    loaded = threading.Event()

    def on_loaded() -> None:
        loaded.set()

    try:
        window.events.loaded += on_loaded
    except Exception:  # pragma: no cover - a backend without the event
        logger.debug("this backend has no 'loaded' event; skipping the blank-window check")
        return

    def check() -> None:
        if loaded.wait(WINDOW_LOAD_TIMEOUT_SECONDS):
            logger.debug("window page loaded")
            return
        hint = blank_window_hint(WINDOW_LOAD_TIMEOUT_SECONDS)
        # Both channels on purpose: the console is what the user is looking at
        # right now, and the log file is what survives the console being closed
        # or scrolled away.
        logger.error("the desktop window never finished loading\n%s", hint)
        print(hint, flush=True)

    threading.Thread(target=check, name="jarvis-ui-load-watchdog", daemon=True).start()


def _deliver(window_box: list[Any], snapshot: UiState) -> None:
    """Push one snapshot into every window that exists yet.

    Both of them, not the first: the HUD and the desktop pet show the same voice
    state, and a pet that stopped pulsing when the assistant started listening would
    be lying about the one thing it is there to say.

    Per window, not per batch. A window that has gone away used to abort the whole
    loop, so the *other* window went un-updated and the pump backed off -- one dead
    window read as "the HUD stopped updating". The dead one is dropped here instead
    of being retried every frame.
    """
    if not window_box:
        logger.debug("no desktop window yet; dropping state snapshot")
        return
    dead: list[Any] = []
    for window in window_box:
        try:
            push_state(window, snapshot)
        except Exception:
            logger.warning("a window stopped taking state; dropping it", exc_info=True)
            dead.append(window)
    for window in dead:
        # 它可能在我们遍历的时候已经被别的窗口先移走了，那件事没有后果。
        # 写成 `suppress` 而不是 `try/except/pass`：后者在静态检查里是一个
        # "这里吞了一个异常"的信号，而这里吞掉的确实是"本来就没打算处理"的那一种。
        with contextlib.suppress(ValueError):  # pragma: no cover - removed while iterating
            window_box.remove(window)
