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

TELEMETRY_INTERVAL_MS = "telemetry.interval_ms"
"""How often the HUD polls system metrics, in milliseconds."""

TTS_VOICE = "tts.voice"
"""The voice id the window chose over the config's ``tts.voice``."""

COMPUTER_TIER = "computer.tier"
"""How much of the machine the window has let the assistant touch."""

SHELL_TIER = "shell.tier"
"""How far the assistant may go with a command line: 关 / 演练 / 当前用户 / 管理员."""

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
    TELEMETRY_INTERVAL_MS: int,
    TTS_VOICE: str,
    COMPUTER_TIER: int,
    SHELL_TIER: int,
    PET_ENABLED: bool,
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
        """
        expected = PREFERENCE_TYPES.get(key)
        if expected is None or not _matches(value, expected):
            logger.warning("refused to store preference %s=%r", key, value)
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
