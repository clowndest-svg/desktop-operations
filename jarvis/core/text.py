"""Text primitives shared by every layer.

A CJK-aware keyword tokenizer lives here rather than in ``memory`` or
``knowledge`` because both need it, they sit in the same layer and are not
allowed to depend on each other, and a second copy would be a second place for
the segmentation rule to drift.

Standard library only, like the rest of ``core``: this module is imported by
everything and must not be able to fail on a missing wheel.
"""

from __future__ import annotations

import re
from typing import Final

CJK_RANGES: Final[tuple[tuple[int, int], ...]] = (
    (0x3400, 0x4DBF),  # CJK Extension A
    (0x4E00, 0x9FFF),  # CJK Unified Ideographs
    (0xF900, 0xFAFF),  # CJK Compatibility Ideographs
)
"""Code-point ranges treated as "one character is a word"."""


def is_cjk(character: str) -> bool:
    """Whether ``character`` belongs to a CJK block."""
    code = ord(character)
    return any(low <= code <= high for low, high in CJK_RANGES)


def keyword_tokens(text: str) -> list[str]:
    """Split ``text`` into tokens suitable for substring (``LIKE``) matching.

    Latin runs become whole words, lowercased, keeping only those longer than
    one character — a bare ``a`` matches nearly every row.

    CJK runs become **bigrams**, not single characters. A single Chinese
    character such as 的 or 我 appears in almost every sentence, so a
    single-character index would return the entire table and call it retrieval;
    two-character windows are the standard cheap substitute for a segmentation
    dictionary. A one-character run is kept as-is, since there is nothing to
    pair it with.

    Duplicates are removed with order preserved, so a query that repeats a word
    does not weight it twice in the match ratio.
    """
    tokens: list[str] = []
    latin: list[str] = []
    cjk: list[str] = []

    def flush_latin() -> None:
        if not latin:
            return
        word = "".join(latin).lower()
        latin.clear()
        if len(word) > 1:
            tokens.append(word)

    def flush_cjk() -> None:
        if not cjk:
            return
        run = "".join(cjk)
        cjk.clear()
        if len(run) == 1:
            tokens.append(run)
        else:
            tokens.extend(run[index : index + 2] for index in range(len(run) - 1))

    for character in text:
        if character.isascii() and character.isalnum():
            flush_cjk()
            latin.append(character)
        elif is_cjk(character):
            flush_latin()
            cjk.append(character)
        else:
            flush_latin()
            flush_cjk()
    flush_latin()
    flush_cjk()
    return list(dict.fromkeys(tokens))


def match_ratio(content: str, tokens: list[str]) -> float:
    """Fraction of ``tokens`` that appear in ``content``.

    A ratio rather than a flat "matched something" bonus: two documents that
    both contain 用户 are not equally relevant to a query about 用户 的 名字, and
    giving them equal credit washes out the signal that could tell them apart.
    """
    if not tokens:
        return 0.0
    lowered = content.lower()
    return sum(1 for token in tokens if token in lowered) / len(tokens)


def human_bytes(size: int) -> str:
    """A byte count a person can read at a glance.

    Lives here because both sides of the bridge want the *same* string: a panel
    that says 1.2 GB next to a model prompt that says 1179.6 MB invites a
    "which one is it" question that has no interesting answer.
    """
    value = float(max(0, size))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"


_FENCED_CODE: Final = re.compile(r"```.*?```|~~~.*?~~~", re.DOTALL)
_MARKDOWN_LINK: Final = re.compile(r"!?\[([^\]]*)\]\([^)]*\)")
_BARE_URL: Final = re.compile(r"\b(?:https?|ftp|file)://\S+", re.IGNORECASE)
_PERCENT: Final = re.compile(r"(\d[\d.,]*)\s*%")
_SPACES: Final = re.compile(r"[ \t]{2,}")

"""Punctuation that only means something *inside* a number or a clock.

``45.0%`` and ``19:32`` have to survive as units; a decimal point that is
deleted turns forty-five into four hundred and fifty, which is the difference
between a sanitizer and a corruption.
"""
_NUMERIC_INFIX: Final = ".,:_"


def speakable(text: str) -> str:
    """The words in ``text``, with everything a voice would have to name removed.

    Written for one complaint — "不要读标点符号，只要读数字和文字" — and applied at the
    single place every utterance passes (:meth:`jarvis.tts.service.TtsService.synthesize`),
    never at the source: the same string is what gets written to the transcript and
    shown on screen, and a transcript with its punctuation shaved off is worse for
    reading than it is for hearing.

    What it removes, and why each case is its own line rather than one character
    class:

    * **Fenced code** goes entirely. A voice reading ``for i in range(3):`` is not
      reading anything a listener can follow.
    * **Markdown structure** (``**``/``#``/``-``/``|``/backticks) is dropped while
      link and heading *text* is kept — it is the label that carries the meaning.
    * **URLs** are dropped, not spelled out.
    * **``80%``** becomes ``百分之80`` because Chinese says a percentage before the
      number, and leaving the sign in place of a word is how you get a voice that
      stalls on a symbol.
    * **``45.0`` / ``1.5 GB`` / ``19:32``** keep their separator, for the reason
      above.
    * Everything else outside letters, digits and CJK becomes a space, which still
      buys the pause the comma was buying.

    An emoji or a stray symbol is simply gone: there is no spoken form that is
    shorter than the silence.
    """
    if not text:
        return ""
    cleaned = _FENCED_CODE.sub(" ", text)
    cleaned = _MARKDOWN_LINK.sub(lambda match: f" {match.group(1)} ", cleaned)
    cleaned = _BARE_URL.sub(" ", cleaned)
    cleaned = _PERCENT.sub(r"百分之\1", cleaned)

    kept: list[str] = []
    for index, character in enumerate(cleaned):
        if character.isascii() and character.isalnum():
            kept.append(character)
            continue
        if is_cjk(character):
            kept.append(character)
            continue
        if character in _NUMERIC_INFIX:
            previous = cleaned[index - 1] if index else ""
            following = cleaned[index + 1] if index + 1 < len(cleaned) else ""
            # Kept verbatim *between* digits: "45.0" is forty-five, "45 0" is a
            # forty-five followed by a zero.
            if previous.isdigit() and following.isdigit():
                kept.append(character)
                continue
        # A space, not nothing: this is where the comma used to buy a breath.
        kept.append(" ")

    collapsed = _SPACES.sub(" ", "".join(kept))
    return re.sub(r"\s+", " ", collapsed).strip()


__all__ = [
    "CJK_RANGES",
    "human_bytes",
    "is_cjk",
    "keyword_tokens",
    "match_ratio",
    "speakable",
]
