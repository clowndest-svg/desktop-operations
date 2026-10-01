"""Memory vocabulary: what a remembered thing *is*.

The kinds are not decoration — they drive recall. A ``PREFERENCE`` is replayed
into every prompt ("用户偏好简短回答"); an ``EPISODE`` is only ever retrieved when
it is relevant; a ``SUMMARY`` replaces a stretch of raw conversation. Storing
everything as one undifferentiated blob of "memory" is what makes assistants
recite irrelevant trivia.
"""

from __future__ import annotations

import enum
from collections.abc import Mapping
from dataclasses import dataclass, field


class MemoryKind(enum.StrEnum):
    """Why a memory exists, which decides how it is recalled."""

    FACT = "fact"
    """Durable, checkable statement about the user or their world."""

    PREFERENCE = "preference"
    """How the user wants to be answered — replayed into every prompt."""

    EPISODE = "episode"
    """Something that happened at a point in time."""

    SUMMARY = "summary"
    """A compressed stretch of conversation, standing in for its turns."""

    TASK = "task"
    """An open loop the assistant committed to (an unfinished TODO)."""


class MemoryScope(enum.StrEnum):
    """Which bucket a memory belongs to."""

    USER = "user"
    """Cross-session, about the person. The default."""

    SESSION = "session"
    """Only meaningful inside one conversation."""

    PROJECT = "project"
    """Tied to whatever the user is working on."""


@dataclass(frozen=True, slots=True)
class MemoryRecord:
    """One stored memory."""

    memory_id: int
    kind: MemoryKind
    scope: MemoryScope
    content: str
    source: str
    importance: float
    """0..1. Feeds the recall ranking; 1.0 survives almost any pruning."""

    pinned: bool
    """Pinned memories are never auto-pruned and always rank above equals."""

    created_at: str
    updated_at: str
    accessed_at: str
    access_count: int

    def to_dict(self) -> dict[str, object]:
        return {
            "memory_id": self.memory_id,
            "kind": self.kind.value,
            "scope": self.scope.value,
            "content": self.content,
            "source": self.source,
            "importance": round(self.importance, 3),
            "pinned": self.pinned,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "accessed_at": self.accessed_at,
            "access_count": self.access_count,
        }


@dataclass(frozen=True, slots=True)
class RecallHit:
    """A memory returned by recall, with the score that got it there."""

    record: MemoryRecord
    score: float
    """Blended score (semantic + keyword + recency + importance + pin)."""

    matched_by: tuple[str, ...] = ()
    """Which retrieval paths found it: ``semantic`` / ``keyword`` / ``pinned``."""

    def to_dict(self) -> dict[str, object]:
        payload = self.record.to_dict()
        payload["score"] = round(self.score, 4)
        payload["matched_by"] = list(self.matched_by)
        return payload


@dataclass(frozen=True, slots=True)
class ConversationTurn:
    """One persisted exchange turn."""

    turn_id: int
    session_id: str
    role: str
    content: str
    created_at: str

    def to_dict(self) -> dict[str, object]:
        return {
            "turn_id": self.turn_id,
            "session_id": self.session_id,
            "role": self.role,
            "content": self.content,
            "created_at": self.created_at,
        }


@dataclass(frozen=True, slots=True)
class ExtractedMemory:
    """A candidate memory proposed by the extractor, before it is stored."""

    content: str
    kind: MemoryKind = MemoryKind.FACT
    importance: float = 0.5
    metadata: Mapping[str, object] = field(default_factory=dict)
