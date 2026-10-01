"""Turning conversation into durable memories.

Two decisions make this cheap enough to run on every turn:

1. **A pre-filter runs first.** Most turns ("现在几点", "讲个笑话") contain nothing
   worth remembering, and asking a model about them is a wasted round trip per
   turn, all day. A turn only reaches the model when it contains a first-person
   marker or an explicit "记住…" instruction.
2. **The model proposes, this module disposes.** The reply is parsed
   defensively: a hallucinated field, a wrong type or a chatty preamble all
   degrade to "no memories this turn", never to an exception on the voice path.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterable
from typing import Final

from jarvis.core.exceptions import JarvisError
from jarvis.llm.client import LlmClient
from jarvis.llm.types import ChatMessage
from jarvis.memory.types import ExtractedMemory, MemoryKind
from jarvis.prompt import render_prompt

logger = logging.getLogger("jarvis.memory.extractor")

_MARKERS: Final[tuple[str, ...]] = (
    "我",
    "我的",
    "咱们",
    "记住",
    "以后",
    "下次",
    "提醒我",
    "我叫",
    "我是",
    "i am",
    "my ",
    "remember",
)
"""Cheap gate in front of the model call.

Deliberately over-inclusive: a false positive costs one cheap request, a false
negative silently loses a memory the user expected the assistant to keep.
"""

_JSON_ARRAY: Final[re.Pattern[str]] = re.compile(r"\[.*\]", re.DOTALL)


def looks_worth_extracting(text: str) -> bool:
    """Whether ``text`` might contain something to remember.

    Public because the service logs the skip rate, and because tests assert the
    filter is doing work rather than being a no-op.
    """
    lowered = text.lower()
    return any(marker in lowered for marker in _MARKERS)


class MemoryExtractor:
    """Proposes long-term memories from a conversation turn."""

    def __init__(self, llm: LlmClient, *, max_items: int = 3) -> None:
        self._llm = llm
        self._max_items = max(0, max_items)

    def extract(self, user_text: str, assistant_text: str = "") -> list[ExtractedMemory]:
        """Return candidate memories for one turn.

        Never raises. A failed extraction is a missed memory, not a broken
        conversation — this runs on the voice path where an exception would be
        indistinguishable from "the assistant went silent".
        """
        if self._max_items == 0 or not user_text.strip():
            return []
        if not looks_worth_extracting(user_text):
            return []
        transcript = f"用户：{user_text.strip()}"
        if assistant_text.strip():
            transcript += f"\n助手：{assistant_text.strip()}"
        try:
            reply = self._llm.complete(
                [
                    ChatMessage.system(render_prompt("memory_extraction")),
                    ChatMessage.user(transcript),
                ]
            ).content
        except JarvisError as exc:
            logger.warning("memory extraction skipped: %s", exc)
            return []
        except Exception:  # pragma: no cover - a client bug must not kill the turn
            logger.exception("memory extraction failed unexpectedly")
            return []
        return self._parse(reply)

    def _parse(self, reply: str) -> list[ExtractedMemory]:
        """Parse the model's JSON array, discarding anything malformed."""
        match = _JSON_ARRAY.search(reply)
        if match is None:
            logger.debug("memory extraction returned no JSON array")
            return []
        try:
            raw = json.loads(match.group(0))
        except json.JSONDecodeError:
            logger.debug("memory extraction returned unparsable JSON")
            return []
        if not isinstance(raw, list):
            return []

        memories: list[ExtractedMemory] = []
        for content, item in self._as_items(raw):
            memories.append(
                ExtractedMemory(
                    content=content,
                    kind=self._kind(item.get("kind")),
                    importance=self._importance(item.get("importance")),
                )
            )
            if len(memories) >= self._max_items:
                break
        return memories

    @staticmethod
    def _as_items(raw: list[object]) -> Iterable[tuple[str, dict[str, object]]]:
        """Yield ``(stripped_content, entry)`` for entries that carry usable text.

        The content is extracted here rather than at the call site so the
        ``isinstance`` check and the ``str`` it guarantees stay in one place;
        otherwise the caller has to re-prove a type it already validated.
        """
        for entry in raw:
            if not isinstance(entry, dict):
                continue
            content = entry.get("content")
            if isinstance(content, str) and content.strip():
                yield content.strip(), entry

    @staticmethod
    def _kind(value: object) -> MemoryKind:
        if isinstance(value, str):
            try:
                return MemoryKind(value.strip().lower())
            except ValueError:
                pass
        return MemoryKind.FACT

    @staticmethod
    def _importance(value: object) -> float:
        if isinstance(value, bool) or not isinstance(value, int | float):
            return 0.5
        return min(1.0, max(0.0, float(value)))
