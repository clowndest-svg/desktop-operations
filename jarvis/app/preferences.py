"""Preferences: the handful of operator choices that have to survive a restart.

Why a separate file instead of ``config.yaml``
---------------------------------------------
The config tree is an operator-authored document with a strict schema — an
unknown key is a hard failure — and pressing a button in the HUD is not editing
configuration. If the two lived in the same place, every click would rewrite a
file the operator may have open in an editor, and "the app changed my config"
would be normal behaviour rather than a bug.

So this stores exactly one kind of thing: **that a human chose it**. The clearest
example is the microphone. Nothing here opens it by itself; a remembered choice
only skips the click, and the first version of that choice still has to come from
the 「启用语音」 button.

What this must never store
--------------------------
Secrets. An API key written here would sit in plaintext in the data root, show up
in any backup of that directory, and appear as a deletable row in the very disk
cleaning tool this application ships with. Keys go to environment variables only
(see :mod:`jarvis.app.settings_service`, which writes the process environment and
the Windows user environment, and never a file).

Writes are atomic (temporary file plus ``os.replace``) and failures are logged,
never raised: losing a preference is bad, failing to open the window because of
one is worse.
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Any

from jarvis.skills.types import ENABLED_KEY as SKILLS_ENABLED

logger = logging.getLogger("jarvis.app.preferences")

VOICE_AUTO_ARM = "voice.auto_arm"
"""Whether the microphone re-arms itself when the window opens."""

VOICE_AUTO_SPEAK_TYPED = "voice.auto_speak_typed"
"""Whether an answer typed into the console is also read aloud."""

LLM_PROVIDER = "llm.provider"
"""Which configured provider the window overrode the default with."""

LLM_BASE_URL = "llm.base_url"
"""Override for that provider's endpoint."""

LLM_MODEL = "llm.model"
"""Override for that provider's model name."""

LLM_OVERRIDES = "llm.overrides"
"""Per-provider edits made from the window: ``{name: {"base_url":…, "model":…}}``.

Per provider rather than global, because a list of models where editing one row
silently rewrites another is worse than no list."""

LLM_EXTRAS = "llm.extras"
"""Models added from the window that do not exist in ``config.yaml``."""

LLM_HIDDEN = "llm.hidden"
"""Rows the operator does not want to see, by provider name.

Hiding is a *view* decision, so it is stored here rather than written back into
``config.yaml`` -- the file stays the operator's, and 找回 puts the row straight back. A row
in use cannot be hidden, so the assistant is never left pointing at a provider off the menu.
"""

TELEMETRY_INTERVAL_MS = "telemetry.interval_ms"
"""How often the HUD polls system metrics, in milliseconds."""

LLM_THINKING = "llm.thinking"
"""Ask the model for its reasoning alongside the answer, and show it in the chat.

Off by default: the extra tokens are a real cost, and the endpoint only returns
``reasoning_content`` when asked -- so this switch is the difference between a box
that fills in and a box that is permanently empty."""

LLM_THINKING_BUDGET = "llm.thinking_budget"
"""How many tokens of reasoning that is allowed.

A token budget rather than a 高/中/低 label, because the endpoint honours it: at 40 the
reasoning truncates mid-sentence while the answer still completes. The number in the
panel is therefore checkable against the ``reasoning_tokens`` the reply reports."""

LLM_TUNING = "llm.tuning"
"""Per provider/model thinking level and context size, keyed ``provider\x00model``.

Keyed by the pair rather than by the model name alone: two vendors happily use the
same string for different models, and one of them will not accept the other's
thinking parameter. The separator is a NUL, which neither a provider name nor a model
id can contain, so no two pairs can collide.

It is a ``dict`` in :data:`PREFERENCE_TYPES` and must stay one: ``set`` refuses an
unregistered key with a log line and a ``False``, so forgetting to list it here makes
every write a silent no-op -- the dropdown appears to work, the level appears to save,
and the next question is answered with the old one."""

LLM_CAPS = "llm.caps"
"""What a probe established about each model, keyed ``provider\x00model``.

Separate from :data:`LLM_TUNING` on purpose. That table is what the operator *wants* from
a model and survives anything; this one is a measurement, and a measurement has to be
overwritten by the next measurement or expire -- never edited by hand. Keeping them apart
is also what lets 设置 show 「没测过」 truthfully instead of reading an absent field as
"this model cannot see images".

A ``dict`` in :data:`PREFERENCE_TYPES` for the same reason :data:`LLM_TUNING` is one.
"""

CHAT_HISTORY_TURNS = "chat.history_turns"
"""How many past exchanges are replayed into each request.
Was a constructor default and nowhere else, which made "she forgot what I said three
messages ago" unfixable from the window."""

SETTINGS_AI_EDITED = "settings.ai_edited"
"""Which settings the assistant changed itself, by key name.

The plan's condition for letting her edit her own configuration was that it stays
visible: the panel tags these rows 「小夜改的」 so an operator who finds 上下文轮数 at 30
can tell whether they chose it. A human saving the same key clears the tag -- the mark
means "not yet confirmed by a person", not "forever touched by the model"."""

TTS_VOICE = "tts.voice"
"""The voice id the window chose over the config's ``tts.voice``."""

TTS_SPEED = "tts.speed"
"""Speaking rate the window chose, as a multiplier of the engine's normal pace.

A window setting rather than a config edit because it is the kind of thing an
operator moves while listening, and the panel's promise is that it applies to the
next sentence -- not after a restart.
"""

TTS_VOLUME = "tts.volume"
"""Speaking volume the window chose, 1.0 being the engine's own level."""

COMPUTER_TIER = "computer.tier"
"""How much of the machine the window has let the assistant touch."""

COMPUTER_ALLOW_TYPING = "computer.allow_typing"
"""Whether she may type text at all. Separate from the tiers on purpose: switching
windows needs keys, but putting a sentence into somebody else's chat box is a
different act. Off unless a person turns it on."""

SHELL_TIER = "shell.tier"
"""How far the assistant may go with a command line: 关 / 演练 / 当前用户 / 管理员."""

WAKE_GREETING = "voice.wake_greeting"
"""The sentence spoken on the wake word. Empty means she stays quiet -- the switch has
to be reachable from the panel, because a greeting nobody can turn off is a second
voice on every wake."""

VOICE_WAKE_KEYWORDS = "voice.wake_keywords"
"""The phrases the operator chose to call her by. A list, because there is never just one
spelling the recogniser produces. Empty/absent means "use ``wakeword.keywords`` from the
configuration", so clearing the box hands back the shipped words."""

THINKING_LOADER = "ui.thinking_loader"
"""Which animation sits to the left of 「思考中」 while a turn is in flight.

One key for two surfaces on purpose: the chat bubble and the desktop figure have to
agree, or the same wait looks like two different products. Unknown values fall back to
:data:`DEFAULT_THINKING_LOADER` at read time rather than being rejected on write -- a
hand-edited config file should not break the interface.
"""

THINKING_LOADERS: tuple[str, ...] = ("dots", "matrix", "ring", "bars")

DEFAULT_THINKING_LOADER = "dots"

ALERTS_RULES = "alerts.rules"
"""Per-rule ``{enabled, threshold}`` for the alert centre (see :mod:`jarvis.app.alerts`).

Absent means "use the shipped line", not "off": the defaults are the measured useful
ones, and an operator who never opens the section still gets told when C: is full.
"""

ALERTS_COOLDOWN_MINUTES = "alerts.cooldown_minutes"
"""How long a recovered alert stays quiet before it may open again."""

DEFAULT_DISPLAY_NAME = "小夜"
"""What the interface calls her when the operator has not chosen a name.

Labels only. The shipped executable, the wake-word recordings (「你好小夜」 is an acoustic
match, not a string) and the asset files on disk keep their names -- renaming those would
break them rather than rename her.
"""

LOCAL_CONTEXT = "llm.local_context"
"""How big a context window the bundled model loads with, in tokens.

Not free, but cheaper than it looks: on this machine 4096 costs 5061 MB resident and 16384
costs 5226 MB, because llama.cpp's ``--fit`` trades model layers in VRAM for the KV cache.
A buyer without a discrete GPU cannot make that trade -- measured CPU-only the same climb
costs +837 MB -- so the memory gate rises with the tier and the tier is the operator's call.
"""

LOCAL_IDLE_MINUTES = "llm.local_idle_minutes"
"""Minutes of silence before the bundled model is thrown away. ``0`` keeps it loaded."""

LOCAL_MODEL = "llm.bundled_model"
"""Which weights file the bundled runtime loads.

``""`` means auto (the biggest file in ``模型``), ``"none"`` means do not load an offline
model at all, anything else is that exact file name. The files that are *not* selected stay
on disk and never touch memory -- that is the whole point of the picker: a buyer with 8 GB
should be able to run the 0.6B and not have the 7B refuse to load at them.
"""

LOCAL_EAGER = "llm.local_eager"
"""Load the bundled model even when the free-memory gate says there is not room.

Off by default: refusing is a sentence on the screen, while over-committing makes Windows
swap the whole desktop. A machine with more RAM than the developer's can opt in knowingly.
"""

DISPLAY_NAME = "ui.display_name"

MIRROR_TIER = "mirror.tier"
"""Which scrcpy quality preset to mirror with. Per machine, not per phone: what the
link can carry depends on this room's WiFi, and a buyer on 2.4GHz needs a different
number than one on a cable."""

MIRROR_AUDIO = "mirror.audio"
"""Whether to carry the phone's audio across. Off by default -- audio capture is a
real stutter source on a weak link, and a mirrored picture that arrives in chunks is
worse than a silent one that arrives smoothly."""

MIRROR_MODE = "mirror.mode"
"""How the phone is expected to reach this computer: over a cable or over the WiFi.

It is a filter on the implicit pick, not a scrcpy flag -- once adb has the device, the
mirror command is identical either way. What changes is which rows count as candidates,
and what the refusal tells you to do about it.
"""

MIRROR_WINDOW = "mirror.window"
"""How big the scrcpy window should be: ``window``, ``maximized`` or ``fullscreen``.

Maximizing is done to the live window; fullscreen is a launch flag (scrcpy's SDL owns
that state), which is why picking it while a mirror is up says "重开一次" rather than
pretending.
"""

MIRROR_STRETCH = "mirror.stretch"
"""Whether to fill the window without preserving the aspect ratio.

Off by default: a stretched face is an ugly truth, and the black bars are the honest
price of the correct shape. On, the picture covers the screen -- which is what some
people mean when they say 铺满.
"""

MIRROR_KEEP_PHONE_SCREEN = "mirror.keep_screen_on"
"""Leave the phone's own display on while mirroring. On by default because turning it
off is surprising; off saves battery and GPU on the phone, which helps the frame rate
on weaker devices."""
"""The name the interface shows. Empty means :data:`DEFAULT_DISPLAY_NAME`."""

MIRROR_ALLOW_SCREEN_VISION = "mirror.allow_screen_vision"
"""Whether 「我手机上现在是什么」 may send the screenshot to a model that has eyes.

Off by default, and not merely as decoration: a phone screen is WeChat threads,
verification codes and payment pages. The accessibility tree and a local OCR read the
same screen without any of it leaving the machine, so the path that costs nothing runs
first and this switch only decides whether the picture may also go to a provider.
"""

MIRROR_KEEP_SCREENSHOTS = "mirror.keep_screen_shots"
"""Whether reading the phone's screen leaves the PNG on disk. Off by default: the read
needs the bytes for a moment, and keeping a running folder of phone screenshots is a
different promise from answering one question."""

ALERTS_SPEAK_CRITICAL = "alerts.speak_critical"
"""Whether a *critical* alert is also spoken. The screen list is not gated by this."""

IMAGE_ENGINE = "image.engine"
"""Which 生图 engine to use: ``auto`` (本地有就本地，没有就云端)、``local``、``cloud``。

Default ``local`` is not chosen because it is nicer but because it is the only one that
answers without a key: a buyer who has ComfyUI running gets pictures the first time they
ask, and a buyer who has not gets a sentence naming what to do, instead of a silent
fallback to a cloud endpoint they never configured.
"""

IMAGE_COMFY_URL = "image.comfy_url"
"""The address of a ComfyUI already running (``http://127.0.0.1:8188``).

Empty means "look for one": the service probes the ports ComfyUI actually uses rather
than making somebody type a URL. Filled in by hand when the engine is on another machine.
"""

IMAGE_COMFY_DIR = "image.comfy_dir"
"""Where a ComfyUI installation lives, when the app should start it itself.

Empty means never start one -- which is the right default on a 4 GB card: a second engine
next to one already running does not slow the picture down, it crashes while loading the
weights. Measured on the author's machine: the copy started, found the CUDA device, and
died inside ``load_checkpoint`` because the other engine held 2.2 of 4 GB.
"""

IMAGE_CLOUD_BASE = "image.cloud_base"
"""Cloud image endpoint's base URL (``.../v1``), OpenAI-compatible ``/images/generations``.

The key never lands here -- see :data:`jarvis.app.settings_service.IMAGE_KEY_ENV`.
"""

IMAGE_CLOUD_MODEL = "image.cloud_model"
"""Which image model that endpoint should use. Empty means "whatever it defaults to"."""

IMAGE_SIZE = "image.size"
"""``512x512`` and friends. One string, because both engines want it that way:
ComfyUI splits it into the latent node's two numbers, cloud endpoints take it verbatim."""

IMAGE_STEPS = "image.steps"
"""Sampler steps. A real cost knob: 20 steps measured 22 s on a GTX 1650, and the same
card roughly triples that at 40 -- this is the difference between 「等一会儿」 and 「算了」."""

IMAGE_COUNT = "image.count"
"""How many candidates one local request draws. Also a real cost knob, and a *quality*
one: the model flips between "a pig-headed humanoid" and "a girl with pig ears" from seed
to seed, so one image means a real chance of handing the user the wrong one. Four measured
83.6 s on a GTX 1650 (20.9 s each); one measured 22.4 s. Cloud endpoints ignore it --
that path sends ``n: 1``."""

IMAGE_KEEP_OUTPUT = "image.keep"

VIDEO_ENGINE = "video.engine"
"""视频用哪条引擎：``local``（默认，只用本机 AnimateDiff）、``cloud``（只用云端 API）、
``auto``（本机引擎能连上就走本地，连不上且云端配好才走云端）。

默认落在本地，和生图同一个理由：本机那条不需要任何钥匙就能出片，买家第一次问就有画面；
把它翻成云端要他自己在下面那一格填端点，填错还会是他能看懂的一句话，不是静默失败。
"""

VIDEO_SIZE = "video.size"
"""本地视频的分辨率。空串＝**自动（按本机显存）**；手动档只有 256x256/384x384/512x512。"""

VIDEO_FPS = "video.fps"
"""帧率。0＝自动；手动档 8/12/16/24。帧率只改播放快慢，不改生成成本（成本看帧数）。"""

VIDEO_SECONDS = "video.seconds"
"""时长（秒）。0＝自动（短视频档）；手动档 2/4/8/16/32，实际能到多少由显存上限截。"""

VIDEO_STEPS = "video.steps"
"""采样步数。0＝自动（16 步）；范围 12~30，和旧版一致。"""

# --- 云端视频：多家可插拔，配置全部由用户自己填 --------------------------------
# 视频没有像生图 ``/images/generations`` 那样的通用协议：可灵 / 即梦 / 通义万相 / Vidu /
# MiniMax 各家都是「提交一个异步任务 → 轮询状态 → 拿回视频 URL」，字段名各不相同。所以这里
# 不给某一家写死契约，而是把这条异步链路的每个"接口形状"摊开成人能填的格子；默认值按
# 阿里通义万相（项目里已有 DASHSCOPE_API_KEY，能直接跑通）预置，换别家就把对应格子改掉。
# 钥匙的值**永远不落这个文件**（见本模块顶部说明）——这里只存"钥匙住在哪个环境变量"的名字。

VIDEO_CLOUD_SUBMIT = "video.cloud.submit_url"
"""提交任务的完整 POST 地址（不是 /v1 那种 base，是这一家真正的建任务端点）。"""

VIDEO_CLOUD_KEY_ENV = "video.cloud.key_env"
"""保存 API 钥匙的环境变量名（默认 DASHSCOPE_API_KEY）。这里只存名字，值只进环境变量。"""

VIDEO_CLOUD_MODEL = "video.cloud.model"
"""模型 id，填进请求体模板里的 {{model}}（如 wanx2.1-t2v-turbo）。"""

VIDEO_CLOUD_BODY = "video.cloud.body"
"""请求体 JSON 模板。占位符按 JSON 值原样插入、不要加引号：
{{prompt}} {{model}} {{width}} {{height}} {{seconds}} {{fps}} {{frames}}。"""

VIDEO_CLOUD_HEADERS = "video.cloud.headers"
"""额外的请求头，一段 JSON 对象。通义万相要 {"X-DashScope-Async": "enable"}；
鉴权头 Bearer 由软件按钥匙自动加，不用写在这里。"""

VIDEO_CLOUD_TASK_PATH = "video.cloud.task_path"
"""提交响应里"任务 id"的取法，点号路径（可带下标）：如 output.task_id。"""

VIDEO_CLOUD_POLL_URL = "video.cloud.poll_url"
"""查进度的 GET 地址模板，{{task_id}} 会被替换。留空＝这一家一次性返回、不轮询。"""

VIDEO_CLOUD_VIDEO_PATH = "video.cloud.video_path"
"""最终响应里"视频下载地址"的取法：如 output.video_url、或 data[0].url。"""

VIDEO_CLOUD_STATUS_PATH = "video.cloud.status_path"
"""轮询响应里"状态"的取法：如 output.task_status。留空＝按有没有拿到视频地址判断完成。"""

VIDEO_CLOUD_SUCCEEDED = "video.cloud.succeeded"
"""视为成功的状态值，逗号分隔（默认 SUCCEEDED）。"""

VIDEO_CLOUD_RUNNING = "video.cloud.running"
"""视为进行中（继续等）的状态值，逗号分隔（默认 PENDING,RUNNING）。"""

VIDEO_CLOUD_FAILED = "video.cloud.failed"
"""视为失败并中止的状态值，逗号分隔（默认 FAILED,UNKNOWN）。"""

KNOWLEDGE_AUTO_LEARN = "knowledge.auto_learn"
"""她自己把"这轮真从网上查过的东西"存进知识库，这一档开不开。

默认 **开**：关掉这条能力就看不见，和"注册一条永远失败的路"是同一类问题。但开关必须在
设置里看得见，而且屏上要写着默认值 —— 自动往资料库里写东西这件事，人不知道就等于没同意。
"""

KNOWLEDGE_AUTO_SEEN = "knowledge.auto_seen"
"""已经自学过的问题指纹。是 ``list``：清单存原生 list，别自己 dumps 成字符串再存。"""

KNOWLEDGE_AUTO_COUNT = "knowledge.auto_count"
"""今天自学了几条（``{"day": ..., "count": ...}``）。带日期是因为上限按"每天"算。"""

"""Whether generated pictures stay in the ``生图`` folder. On by default: a generated
file the operator cannot go and look at is a capability with a hole in it."""


def display_name(value: object) -> str:
    """The name to print, or the shipped one when the box was left empty or filled with spaces."""
    text = value if isinstance(value, str) else ""
    return text.strip() or DEFAULT_DISPLAY_NAME


def thinking_loader(value: object) -> str:
    """The loader a stored value asks for, or the default when it asks for nothing."""
    return (
        value if isinstance(value, str) and value in THINKING_LOADERS else DEFAULT_THINKING_LOADER
    )


PET_ENABLED = "pet.enabled"
"""Whether the desktop pet is switched on.

Remembered rather than per-run: the choice is about the operator's desktop, and a
figure that reappears after every reboot -- or vanishes after it -- is both annoying
and, since it is the thing that listens for the wake word, confusing about where to
speak."""

PET_SCALE = "pet.scale"
"""她有多大：small / normal / large / giant 四档之一。

四档而不是一个自由数字：设置里那一下要点得中，而且每一档能各带一句实测的代价。底宽
420x640 是**画出来的框**，不是她身体本身 —— 所以变大时多出来那圈仍是透明的。"""

PET_IDLE_MOTION = "pet.idle_motion"
"""要不要每隔一阵自己动一下。关掉之后她只站着，戳她照样有反应。"""

PET_ORIGIN = "pet.origin"
"""她站在屏幕上的左上角 [x, y]（物理像素）。空列表 = 没挪过，按居中放。

只记一个点：拖动是她唯一的摆位方式，而"重启就回中间"是最让人骂的一种失忆。"""

PET_FIGURE = "pet.figure"
"""Which generated pet stands on the desktop. ``""`` means the built-in figure.

An id and not a path: the polygon lives in ``数据/pets/index.json``, and a stored
path would outlive the file it names -- which turns the pet window into a silent
"she did not change" instead of a row the picker can say is gone.
"""

WINDOW_RECT = "window.rect"
"""Where the HUD window was last left: ``{x, y, width, height, maximized}``.

A geometry, not a setting people edit: what is being remembered is that the
operator chose a size on this monitor, which a window that always opens at
1280x800 on a 1920x1080 screen throws away every launch.
"""

PREFERENCE_TYPES: dict[str, type] = {
    "ui.portrait": dict,
    VOICE_AUTO_ARM: bool,
    VOICE_AUTO_SPEAK_TYPED: bool,
    LLM_PROVIDER: str,
    LLM_BASE_URL: str,
    LLM_MODEL: str,
    LLM_OVERRIDES: dict,
    LLM_EXTRAS: list,
    LLM_HIDDEN: list,
    TELEMETRY_INTERVAL_MS: int,
    LLM_THINKING: bool,
    LLM_THINKING_BUDGET: int,
    CHAT_HISTORY_TURNS: int,
    LLM_TUNING: dict,
    LLM_CAPS: dict,
    SETTINGS_AI_EDITED: list,
    WAKE_GREETING: str,
    VOICE_WAKE_KEYWORDS: list,
    TTS_VOICE: str,
    TTS_SPEED: float,
    TTS_VOLUME: float,
    COMPUTER_TIER: int,
    COMPUTER_ALLOW_TYPING: bool,
    SHELL_TIER: int,
    PET_ENABLED: bool,
    PET_SCALE: str,
    PET_IDLE_MOTION: bool,
    PET_ORIGIN: list,
    PET_FIGURE: str,
    THINKING_LOADER: str,
    ALERTS_RULES: dict,
    ALERTS_COOLDOWN_MINUTES: float,
    ALERTS_SPEAK_CRITICAL: bool,
    WINDOW_RECT: dict,
    LOCAL_CONTEXT: int,
    LOCAL_IDLE_MINUTES: int,
    LOCAL_EAGER: bool,
    LOCAL_MODEL: str,
    DISPLAY_NAME: str,
    MIRROR_TIER: str,
    MIRROR_AUDIO: bool,
    MIRROR_KEEP_PHONE_SCREEN: bool,
    MIRROR_ALLOW_SCREEN_VISION: bool,
    MIRROR_KEEP_SCREENSHOTS: bool,
    MIRROR_MODE: str,
    MIRROR_WINDOW: str,
    MIRROR_STRETCH: bool,
    IMAGE_ENGINE: str,
    IMAGE_COMFY_URL: str,
    IMAGE_COMFY_DIR: str,
    IMAGE_CLOUD_BASE: str,
    IMAGE_CLOUD_MODEL: str,
    IMAGE_SIZE: str,
    IMAGE_STEPS: int,
    IMAGE_COUNT: int,
    IMAGE_KEEP_OUTPUT: bool,
    VIDEO_ENGINE: str,
    VIDEO_SIZE: str,
    VIDEO_FPS: int,
    VIDEO_SECONDS: int,
    VIDEO_STEPS: int,
    VIDEO_CLOUD_SUBMIT: str,
    VIDEO_CLOUD_KEY_ENV: str,
    VIDEO_CLOUD_MODEL: str,
    VIDEO_CLOUD_BODY: str,
    VIDEO_CLOUD_HEADERS: str,
    VIDEO_CLOUD_TASK_PATH: str,
    VIDEO_CLOUD_POLL_URL: str,
    VIDEO_CLOUD_VIDEO_PATH: str,
    VIDEO_CLOUD_STATUS_PATH: str,
    VIDEO_CLOUD_SUCCEEDED: str,
    VIDEO_CLOUD_RUNNING: str,
    VIDEO_CLOUD_FAILED: str,
    KNOWLEDGE_AUTO_LEARN: bool,
    KNOWLEDGE_AUTO_SEEN: list,
    KNOWLEDGE_AUTO_COUNT: dict,
    # 键名从 jarvis.skills.types 引，不在这里再打一遍字符串：两处各写一遍迟早漂，
    # 而漂了的失败是 set() 静默拒绝 —— 装好的技能一刷新全灭，界面上看不出原因。
    SKILLS_ENABLED: list,
}
"""The only keys that round-trip, with the type each must have.

Anything else in the file is ignored on read and dropped on the next write. That
is deliberate: this file is small and hand-inspectable, and a stale key left by an
old version should not be carried forward forever.
"""


def _matches(value: object, expected: type) -> bool:
    """Type check that survives Python's bool-is-an-int inheritance."""
    if expected is bool:
        return isinstance(value, bool)
    if expected is int:
        return isinstance(value, int) and not isinstance(value, bool)
    if expected is float:
        # A hand-edited ``1`` is the same number as ``1.0``; refuse only the bool,
        # which Python would happily treat as one of them.
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected is str:
        return isinstance(value, str)
    return isinstance(value, expected)


class Preferences:
    """A small JSON mapping of remembered choices under the data root."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._unsaved: set[str] = set()
        """Keys applied in memory that the disk does not have yet.

        Written by :meth:`set` when the file write fails. Without it, "applied" and
        "persisted" collapse into one boolean and a failed save is indistinguishable
        from a successful one -- which for 命令行档位 meant a downgrade that quietly
        rolled itself back at the next start.
        """
        self._values: dict[str, Any] | None = None

    @property
    def path(self) -> Path:
        """Where the choices are kept; useful for a status line and for tests."""
        return self._path

    # -- reads ---------------------------------------------------------------

    def get(self, key: str, default: Any = None) -> Any:
        """The remembered value for ``key``, or ``default``.

        A value stored under a key whose type has since changed is treated as
        absent rather than coerced, so a half-migrated file cannot hand a string to
        a caller expecting a number.
        """
        expected = PREFERENCE_TYPES.get(key)
        if expected is None:
            return default
        with self._lock:
            value = self._read().get(key, default)
        if value is not default and not _matches(value, expected):
            logger.warning(
                "preference %s=%r is not a %s; ignoring it", key, value, expected.__name__
            )
            return default
        return value

    def flag(self, key: str, *, default: bool = False) -> bool:
        """The remembered boolean for ``key``."""
        value = self.get(key, default)
        return value if isinstance(value, bool) else default

    def text(self, key: str, *, default: str = "") -> str:
        """The remembered string for ``key``."""
        value = self.get(key, default)
        return value if isinstance(value, str) else default

    def number(self, key: str, *, default: int = 0) -> int:
        """The remembered integer for ``key``."""
        value = self.get(key, default)
        return value if isinstance(value, int) else default

    def real(self, key: str, *, default: float = 0.0) -> float:
        """The remembered real number for ``key``.

        Separate from :meth:`number` because that one promises an int and drops a
        float on the floor -- and a speed stored as 1.0 and read back as "not set"
        is a slider that appears to save and does nothing.
        """
        value = self.get(key, default)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return default
        return float(value)

    def as_dict(self) -> dict[str, Any]:
        """Every remembered value, for the settings screen."""
        with self._lock:
            return dict(self._read())

    def _read(self) -> dict[str, Any]:
        """Load once per process. A missing file is the normal first run."""
        if self._values is None:
            self._values = self._load_from_disk()
        return self._values

    def _load_from_disk(self) -> dict[str, Any]:
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError):
            # Corrupt or unreadable: fall back to "no remembered choices" rather
            # than refusing to start. The file is a convenience, not a record.
            logger.exception("preferences at %s are unreadable; starting clean", self._path)
            return {}
        if not isinstance(raw, dict):
            logger.warning("preferences at %s are not an object; ignoring them", self._path)
            return {}
        kept: dict[str, Any] = {}
        for key, value in raw.items():
            expected = PREFERENCE_TYPES.get(key)
            if expected is None:
                continue
            if not _matches(value, expected):
                logger.warning(
                    "preference %s=%r in %s is not a %s; dropping it",
                    key,
                    value,
                    self._path,
                    expected.__name__,
                )
                continue
            kept[key] = value
        return kept

    # -- writes --------------------------------------------------------------

    def set(self, key: str, value: Any) -> bool:
        """Record one choice. Returns whether it was accepted.

        An unknown key or a wrongly typed value is refused rather than stored: the
        settings screen is the caller and it should hear "no" instead of writing a
        file the reader will later discard.

        The value is also checked for being *serialisable*, before it goes anywhere.
        ``_matches`` can only ask whether the top level is a dict, and a dict holding a
        dataclass passes it -- then the write fails, the exception is logged and
        swallowed, and the in-memory copy keeps the value the file never got. From that
        moment the running program reads its own cache while the disk says something
        else: settings that look saved, and a model list that quietly shrinks after a
        restart. Refusing here is the only place both sides can be kept in step.
        """
        expected = PREFERENCE_TYPES.get(key)
        if expected is None or not _matches(value, expected):
            logger.warning("refused to store preference %s=%r", key, value)
            return False
        try:
            json.dumps(value, ensure_ascii=False)
        except (TypeError, ValueError):
            logger.exception("preference %s=%r cannot be serialised for %s", key, value, self._path)
            return False
        with self._lock:
            values = self._read()
            if key in values and values[key] == value and key not in self._unsaved:
                return True  # an unchanged choice that is already on disk is not a write
            values[key] = value
            self._values = values
            try:
                self._write(values)
            except Exception:  # a lost preference must not lose the window
                logger.exception("could not save preference %s=%s to %s", key, value, self._path)
                # The value *is* applied -- this is what keeps a permission downgrade in
                # force for the rest of the session even though the disk still says
                # "wide open". What it is not, is saved, and saying otherwise is how
                # r93 shipped a 命令行档位 that came back after a restart.
                self._unsaved.add(key)
                return False
            self._unsaved.discard(key)
        return True

    def unsaved(self) -> list[str]:
        """Keys held in memory but not on disk.

        ``applied`` and ``persisted`` are two facts, and one bool from :meth:`set` can
        only ever describe one call. Anything that shows "已保存" has to ask this.
        """
        with self._lock:
            return sorted(self._unsaved)

    def retry_unsaved(self) -> list[str]:
        """Write the dirty keys again; returns the ones still not on disk.

        The same-value short-circuit above is what makes this necessary: once the cache
        matches the wish, a plain retry of ``set()`` would be the only way to persist it,
        and that path used to be skipped as "no change".
        """
        with self._lock:
            if not self._unsaved:
                return []
            try:
                self._write(dict(self._read()))
            except Exception:
                logger.exception("补写偏好还是失败：%s", self._path)
                return sorted(self._unsaved)
            count = len(self._unsaved)
            self._unsaved.clear()
        logger.info("补写回了 %d 项偏好", count)
        return []

    def set_flag(self, key: str, value: bool) -> bool:
        """Record one boolean choice. Never raises; ``False`` means it is not on disk."""
        return self.set(key, bool(value))

    def forget(self, key: str) -> None:
        """Drop one remembered choice (the settings screen's "restore default")."""
        with self._lock:
            values = self._read()
            if values.pop(key, None) is None:
                return
            self._values = values
            try:
                self._write(values)
            except Exception:
                logger.exception("could not forget preference %s in %s", key, self._path)

    def _write(self, values: dict[str, Any]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._path.with_name(f"{self._path.name}.tmp")
        temporary.write_text(
            json.dumps(values, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
        )
        temporary.replace(self._path)
