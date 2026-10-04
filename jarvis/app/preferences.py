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

ALERTS_SPEAK_CRITICAL = "alerts.speak_critical"
"""Whether a *critical* alert is also spoken. The screen list is not gated by this."""


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

WINDOW_RECT = "window.rect"
"""Where the HUD window was last left: ``{x, y, width, height, maximized}``.

A geometry, not a setting people edit: what is being remembered is that the
operator chose a size on this monitor, which a window that always opens at
1280x800 on a 1920x1080 screen throws away every launch.
"""

PREFERENCE_TYPES: dict[str, type] = {
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
    THINKING_LOADER: str,
    ALERTS_RULES: dict,
    ALERTS_COOLDOWN_MINUTES: float,
    ALERTS_SPEAK_CRITICAL: bool,
    WINDOW_RECT: dict,
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
            if key in values and values[key] == value:
                return True  # an unchanged choice is not a disk write
            values[key] = value
            self._values = values
            try:
                self._write(values)
            except Exception:  # a lost preference must not lose the window
                logger.exception("could not save preference %s=%s to %s", key, value, self._path)
        return True

    def set_flag(self, key: str, value: bool) -> None:
        """Record one boolean choice. Never raises into the caller."""
        self.set(key, bool(value))

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
