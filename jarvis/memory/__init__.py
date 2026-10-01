"""Memory: short/long-term memory, user profile and preferences.

Responsibility (delivered in phase 11):
    * Short-term conversational memory, long-term persistent memory,
      automatic history summarisation, memory compression / recall /
      ranking, user profile & preference store.

How it works, in one paragraph: every turn is written to ``memory_turns`` so a
session can be replayed; a cheap marker filter decides whether the turn is worth
mining, and only then does a model propose durable ``memory_records``. Recall is
hybrid — vector similarity *and* keyword search over the same table, blended with
importance, recency and access count — because a personal assistant is asked for
exact strings (names, order numbers) at least as often as for paraphrases.

Allowed dependencies: ``core``, ``config``, ``database``, ``vector``,
``llm`` (for summarisation).
"""

from jarvis.memory.extractor import MemoryExtractor, looks_worth_extracting
from jarvis.memory.service import COLLECTION_PREFIX, MemoryService
from jarvis.memory.store import MIGRATIONS, NAMESPACE, MemoryRepository
from jarvis.memory.types import (
    ConversationTurn,
    ExtractedMemory,
    MemoryKind,
    MemoryRecord,
    MemoryScope,
    RecallHit,
)

__all__ = [
    "COLLECTION_PREFIX",
    "MIGRATIONS",
    "NAMESPACE",
    "ConversationTurn",
    "ExtractedMemory",
    "MemoryExtractor",
    "MemoryKind",
    "MemoryRecord",
    "MemoryRepository",
    "MemoryScope",
    "MemoryService",
    "RecallHit",
    "looks_worth_extracting",
]
