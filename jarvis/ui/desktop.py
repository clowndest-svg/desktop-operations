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

import json
import logging
import os
import re
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from jarvis.app.preferences import PET_ENABLED, WINDOW_RECT
from jarvis.core.events import VoicePhase
from jarvis.ui import autostart
from jarvis.ui.hotkeys import Hotkeys, default_bindings
from jarvis.ui.instance import InstanceGate
from jarvis.ui.lifecycle import ShellLifecycle
from jarvis.ui.pet import PetController
from jarvis.ui.pump import UiEventPump
from jarvis.ui.state_bridge import StateBridge, UiState
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
    from jarvis.app.voice_picker import VoicePicker
    from jarvis.app.voice_service import VoiceService
    from jarvis.ui.audio_bridge import AudioPusher

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
        computer_access: ComputerAccess | None = None,
        command_access: CommandAccess | None = None,
        process: ProcessService | None = None,
        lifecycle: ShellLifecycle | None = None,
        reminders: ReminderService | None = None,
        announcer: Announcer | None = None,
        memory: MemorySurface | None = None,
        knowledge: KnowledgeSurface | None = None,
    ) -> None:
        self._system = system
        self._disk = disk
        self._voice = voice
        self._chat = chat
        self._state = state
        self._usage = usage
        self._settings = settings
        self._audio = audio
        self._tools = tools
        self._voice_picker = voice_picker
        self._computer_access = computer_access
        self._command_access = command_access
        self._process = process
        self._lifecycle = lifecycle
        self._reminders = reminders
        self._announcer = announcer
        self._memory = memory
        self._knowledge = knowledge
        self._pet: PetController | None = None
        self._remember_pet: Callable[[bool], None] | None = None
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
        """Which models the chat can be pointed at, and which one it is on now."""
        if self._settings is None:
            return {"error": "设置不可用", "current": "", "choices": []}
        return self._settings.model_choices()

    def chat_pick(self, provider: str) -> dict[str, object]:
        """Switch the assistant to another configured model.

        Answers with a fresh choice list, not just an ok flag: the page has to be
        able to show *why* a pick was refused (an unknown name, a provider whose
        endpoint failed validation) without a second round-trip.
        """
        if self._settings is None:
            return {"error": "设置不可用", "current": "", "choices": []}
        return self._settings.choose_model(provider)

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
        return self._settings.apply(dict(patch))

    def usage_summary(self, days: int = 7) -> dict[str, object]:
        """Token totals for the last ``days`` days, plus the per-day series.

        The window is clamped to one month inside the service, not here: the cap is
        a property of the ledger, and a second copy of the rule in the web layer is
        a rule that drifts.
        """
        if self._usage is None:
            return {"error": "用量统计不可用", "summary": None, "daily": []}
        try:
            window = int(days)
        except (TypeError, ValueError):
            window = 7
        return {
            "error": "",
            "summary": self._usage.summary(window).to_dict(),
            "daily": self._usage.daily(window),
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

    def chat_ask(self, text: str, attachments: object = ()) -> dict[str, object]:
        """Answer one typed question, with whatever files came with it.

        ``attachments`` arrives as a JSON list from the page and is cleaned in the
        application layer; the bridge passes it through untouched because a bridge
        that validates is a second place for the rules to drift.
        """
        if self._chat is None:
            return {"question": text, "answer": "", "error": "对话服务未启用"}
        payload = attachments if isinstance(attachments, list) else []
        reply = self._chat.ask(text, attachments=payload)
        if self._state is not None and not reply.error:
            # Typed and spoken turns share one transcript. Two logs in a chat panel
            # leaves the reader working out which one is the conversation.
            self._state.add_turn("user", reply.question)
            self._state.add_turn("assistant", reply.answer)
        if not reply.error and self._should_read_aloud():
            # Fire and forget, and deliberately *after* the answer is on screen: a
            # voice is a second copy of something the operator can already read, so
            # it must never delay or replace the first one.
            self._speak(reply.answer)
        return reply.to_dict()

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

    def audio_ready(self, ok: bool, reason: str = "") -> dict[str, object]:
        """The page's own verdict on whether it can play audio.

        Nothing is sent to the page before it says yes, and a page that says no --
        no ``AudioContext``, an autoplay policy it cannot satisfy -- gets its
        answers out of the speaker instead. The answer is echoed back so the caller
        can render which output it is, without a second round-trip to ``voice_status``.
        """
        if self._audio is None:
            return {"output": "speaker", "reason": "本进程没有接通界面音频通道"}
        self._audio.mark_ready(bool(ok), str(reason or ""))
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
    audio: AudioPusher | None = None,
    tools: ToolLedger | None = None,
    voice_picker: VoicePicker | None = None,
    computer_access: ComputerAccess | None = None,
    command_access: CommandAccess | None = None,
    process: ProcessService | None = None,
    bridge_sink: Callable[[UiState], None] | None = None,
    width: int = 1280,
    height: int = 800,
    preferences: Preferences | None = None,
    tray_enabled: bool = True,
    instance_lock: Path | None = None,
    reminders: ReminderService | None = None,
    executable: Path | None = None,
    hotkeys_enabled: bool = True,
    announcer: Announcer | None = None,
    memory: MemorySurface | None = None,
    knowledge: KnowledgeSurface | None = None,
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
    gate = InstanceGate(instance_lock) if instance_lock is not None else None
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
    lifecycle = ShellLifecycle()
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
        computer_access=computer_access,
        command_access=command_access,
        process=process,
        lifecycle=lifecycle,
        reminders=reminders,
        announcer=announcer,
        memory=memory,
        knowledge=knowledge,
    )
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
    )
    lifecycle.attach_pet(pet)
    bridge.attach_pet(pet, remember=_remember_pet_choice(preferences))
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
        _restore_pet_on_load(window, pet, window_box)
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


def _remember_pet_choice(
    preferences: Preferences | None,
) -> Callable[[bool], None] | None:
    """The one writer of 「桌面宠物」 into the preference file."""
    if preferences is None:
        return None

    def remember(value: bool) -> None:
        preferences.set(PET_ENABLED, bool(value))

    return remember


def _restore_pet_on_load(window: Any, pet: PetController, window_box: list[Any]) -> None:
    """Show the pet on start-up when the operator left it showing.

    One-shot: ``loaded`` also fires on a page reload, and a pet that pops back over
    the window the operator just dismissed is a bug report waiting to be filed.
    """
    events = getattr(window, "events", None)
    if events is None:  # pragma: no cover - a pywebview without its event box
        return
    pending = {"want": True}

    def on_loaded() -> None:
        if not pending["want"]:
            return
        pending["want"] = False
        if pet.show(emerge=True):
            created = pet.window
            if created is not None:
                window_box.append(created)

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
    """
    if not window_box:
        logger.debug("no desktop window yet; dropping state snapshot")
        return
    for window in window_box:
        push_state(window, snapshot)
