"""The wake word: what the operator may call her, and what happens when they change it.

The shipped words live in ``wakeword.keywords`` in the configuration, and four of them are
the measured homophone spellings of one phrase (``jarvis/config/defaults.yaml``) -- the
recogniser transcribes 「你好小夜」 as 你好小智 / 你好晓夜 often enough that listing only the
correct spelling loses wakes. A person who wants to rename her should not have to open a
YAML file, so this is the editable layer on top of it:

* what is stored is *only* what the operator typed, in ``voice.wake_keywords``;
* an empty store means "use the configured ones", so clearing the box gives the shipped
  words back rather than leaving a machine that answers to nothing.

Why the bounds are this small: these strings are matched as a substring of a transcription,
so a long phrase is not safer, it is just easier to mis-say, and every extra word is another
phrase the recogniser can hit inside an ordinary sentence. Six phrases of up to twelve
characters is well past what any one person actually uses at a desk.

Why the change takes effect without restarting the microphone: the wake engine reads the
list per utterance. Rebuilding the stack would drop the model load (seconds) and, on a
machine where the operator is mid-sentence, would silently eat that turn.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Sequence
from typing import Any

from jarvis.app.preferences import VOICE_WAKE_KEYWORDS

logger = logging.getLogger("jarvis.app.wake_keywords")

MAX_WAKE_KEYWORDS: int = 6
"""How many phrases one person can reasonably keep in mind."""

MIN_KEYWORD_CHARS: int = 2
"""Shorter than this and the phrase appears inside ordinary speech.

The match is a substring test on the folded transcription, so 「好」 would wake her from
half the sentences in a room.
"""

MAX_KEYWORD_CHARS: int = 12
"""Longer than this and it is a sentence, and sentences get mis-said."""

_SPLIT = re.compile(r"[、,，;；/\n]+")
"""What the panel's one text box is split on.

One box rather than six: the operator says a name, not a data structure, and they type the
separator that comes to mind. Deliberately **not** a space -- 「Hey Jarvis」 is one phrase,
and the shipped ``hey_jarvis`` spelling has a fold that already ignores inner spaces, so
splitting on them would turn one name into two refuse-worthy ones.
"""


def parse_wake_keywords(raw: Any) -> tuple[list[str], str]:
    """Turn whatever the panel sent into a storable list.

    Returns ``(keywords, refusal)``. ``refusal`` is non-empty when the input must not be
    stored, and in that case ``keywords`` is empty -- a caller that ignores the second
    value would be storing a half-accepted list.

    Accepts a list (the shape on disk) or a single string (the shape from the text box).
    """
    if isinstance(raw, str):
        pieces = [part for part in _SPLIT.split(raw) if part.strip()]
    elif isinstance(raw, (list, tuple, set)):
        pieces = [str(part) for part in raw]
    elif raw is None:
        return [], ""
    else:
        return [], "唤醒词得是一个字符串或一个列表"

    kept: list[str] = []
    seen: set[str] = set()
    for piece in pieces:
        word = piece.strip()
        if not word:
            continue
        if len(word) < MIN_KEYWORD_CHARS:
            return [], (
                f"「{word}」太短了：唤醒词至少 {MIN_KEYWORD_CHARS} 个字，" "不然日常说话就会撞到"
            )
        if len(word) > MAX_KEYWORD_CHARS:
            return [], f"「{word}」太长了：唤醒词最多 {MAX_KEYWORD_CHARS} 个字"
        folded = word.casefold()
        if folded in seen:
            continue
        seen.add(folded)
        kept.append(word)
    if len(kept) > MAX_WAKE_KEYWORDS:
        return [], f"最多 {MAX_WAKE_KEYWORDS} 个唤醒词，现在 {len(kept)} 个"
    return kept, ""


def resolve(
    stored: Sequence[str] | None,
    configured: Sequence[str],
) -> tuple[str, ...]:
    """The words to actually watch: the operator's if they wrote any, else the shipped ones.

    An empty *or unusable* store falls back to the configuration. Falling back on unusable
    input is logged by the caller that reads it, never here -- this function is also used
    by the settings panel, which must show the fallback rather than complain.
    """
    if isinstance(stored, (list, tuple)):
        words = tuple(str(item).strip() for item in stored if str(item).strip())
        if words:
            return words
    return tuple(str(item).strip() for item in configured if str(item).strip())


class WakeWords:
    """The one place that knows both the operator's list and the configured default."""

    def __init__(
        self,
        preferences: Any | None,
        configured_provider: Callable[[], Sequence[str]],
    ) -> None:
        """Create it.

        Args:
            preferences: Where the typed list lives. ``None`` means "always the configured
                words", which is what the console wants.
            configured_provider: The shipped list, read lazily because configuration is
                not loaded when the composition root builds this object.
        """
        self._prefs = preferences
        self._configured_provider = configured_provider

    def configured(self) -> tuple[str, ...]:
        """The words that came with the installation."""
        try:
            return tuple(self._configured_provider())
        except Exception:  # a broken config read must not cost the wake word entirely
            logger.exception("could not read wakeword.keywords; falling back to nothing")
            return ()

    def stored(self) -> tuple[str, ...]:
        """What the operator typed, or ``()`` when they have not typed anything."""
        if self._prefs is None:
            return ()
        raw = self._prefs.get(VOICE_WAKE_KEYWORDS)
        if not isinstance(raw, (list, tuple)):
            return ()
        keywords, refusal = parse_wake_keywords(list(raw))
        if refusal:
            # Staying quiet here would make the box and the machine disagree with no trace.
            logger.warning("stored wake words rejected (%s); using the configured ones", refusal)
            return ()
        return tuple(keywords)

    def effective(self) -> tuple[str, ...]:
        """What the engine should watch for right now."""
        return resolve(self.stored(), self.configured())

    def settings(self) -> dict[str, Any]:
        """The section the settings panel draws from this."""
        return {
            "wake_keywords": list(self.effective()),
            "wake_keywords_stored": list(self.stored()),
            "wake_keywords_default": list(self.configured()),
            "wake_keywords_max": MAX_WAKE_KEYWORDS,
            "wake_keyword_min_chars": MIN_KEYWORD_CHARS,
            "wake_keyword_max_chars": MAX_KEYWORD_CHARS,
        }

    def apply(self, raw: Any) -> str | None:
        """Store the operator's list. Returns a refusal message, or ``None``.

        An empty box is a *value*, not a no-op: it means "give me back the configured
        words", which is how a person undoes a rename without finding the file.
        """
        keywords, refusal = parse_wake_keywords(raw)
        if refusal:
            return refusal
        if self._prefs is None:
            return "这台机器没有可写的设置存储"
        self._prefs.set(VOICE_WAKE_KEYWORDS, keywords)
        logger.info("唤醒词改为：%s", "、".join(keywords) if keywords else "(用配置里的那几个)")
        return None


__all__ = [
    "MAX_KEYWORD_CHARS",
    "MAX_WAKE_KEYWORDS",
    "MIN_KEYWORD_CHARS",
    "WakeWords",
    "parse_wake_keywords",
    "resolve",
]
