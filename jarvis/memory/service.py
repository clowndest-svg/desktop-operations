"""``MemoryService`` — recall, remember, forget, and compress.

Recall is deliberately **hybrid**. Pure vector search fails on the queries a
personal assistant gets most often — a name, an order number, "我上次说的那个" —
because a hashing embedder blurs exact strings into the same neighbourhood as
everything else. Pure keyword search fails on paraphrase. Running both and
blending the scores costs one extra SQL pass and fixes both.

The ranking is a blend rather than a filter chain, so no single signal can veto
a memory: a pinned preference stays available even when it is semantically
distant from the question, and a memory that keeps proving useful drifts upward
through its access count without anybody deciding it should.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from jarvis.core.exceptions import JarvisError
from jarvis.core.text import keyword_tokens, match_ratio
from jarvis.database import as_int
from jarvis.llm.types import ChatMessage, Role
from jarvis.memory.extractor import MemoryExtractor
from jarvis.memory.store import MIGRATIONS, NAMESPACE, MemoryRepository
from jarvis.memory.types import (
    ConversationTurn,
    MemoryKind,
    MemoryRecord,
    MemoryScope,
    RecallHit,
)
from jarvis.prompt import render_prompt

if TYPE_CHECKING:  # pragma: no cover - typing only
    from jarvis.config.schema import MemorySection
    from jarvis.database import SqliteStore
    from jarvis.llm.client import LlmClient
    from jarvis.vector import VectorService

logger = logging.getLogger("jarvis.memory.service")

COLLECTION_PREFIX: str = "memory:"
"""Per-scope vector collection, e.g. ``memory:user``.

One index per scope rather than one shared index filtered afterwards: a filter
applied after ``top_k`` silently returns fewer than ``top_k`` results, which
looks like "the assistant forgot" rather than "the query was scoped".
"""

KEYWORD_WEIGHT: float = 0.5
"""How much a full keyword match adds to a memory's score.

Chosen so that a memory matched by keywords alone (no semantic hit at all, e.g.
an exact order number the hashing embedder blurs away) still clears the default
0.15 floor — while a memory matched by every query token still cannot outrank a
strong semantic hit.
"""

IMPORTANCE_WEIGHT: float = 0.15
"""Contribution of the stored importance. A tie-breaker, not a gate: an
unimportant memory that is genuinely relevant must still be able to surface."""

ACCESS_WEIGHT: float = 0.05
"""Contribution of ``log1p(access_count)``, so memories that keep proving useful
drift upward without anybody deciding they should. Logarithmic because the
difference between 0 and 1 recalls matters far more than 30 versus 31."""


def _recency_boost(timestamp: str, now: datetime) -> float:
    """Fresh memories rank slightly higher; the effect halves about every 30 days.

    Small on purpose (max 0.1). Recency is a tie-breaker, not a reason to forget
    something the user told you a year ago and still means.
    """
    try:
        moment = datetime.strptime(timestamp, "%Y-%m-%dT%H:%M:%S.%f%z")
    except ValueError:
        return 0.0
    age_days = max(0.0, (now - moment).total_seconds() / 86400.0)
    return 0.1 * math.exp(-age_days / 30.0)


def _tokens(text: str) -> list[str]:
    """Keyword tokens for the SQL fallback.

    Delegates to :func:`jarvis.core.text.keyword_tokens`, which ``knowledge``
    uses too — a second copy would be a second place for the CJK segmentation
    rule to drift.
    """
    return keyword_tokens(text)


class MemoryService:
    """Long-term memory: what the assistant knows about this user."""

    name = "memory"

    def __init__(
        self,
        store: SqliteStore,
        vector: VectorService,
        settings_provider: Callable[[], MemorySection],
        *,
        llm_provider: Callable[[], LlmClient] | None = None,
    ) -> None:
        """Create the service.

        Args:
            store: The shared database (already started).
            vector: The vector service (already started).
            settings_provider: Returns the validated ``memory`` config section.
            llm_provider: Yields a chat client for extraction and summarisation.
                ``None`` disables both, leaving store/recall fully functional —
                a text-only install still gets memory, just not automatic
                fact extraction.
        """
        self._store = store
        self._vector = vector
        self._settings_provider = settings_provider
        self._llm_provider = llm_provider
        self._repo = MemoryRepository(store)
        self._started = False

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        """Apply the schema (idempotent, no network, no model load)."""
        if self._started:
            return
        self._store.migrate(NAMESPACE, MIGRATIONS)
        self._started = True
        logger.info(
            "memory service ready (%d memories; by kind: %s)",
            self._repo.count(),
            self._repo.count_by_kind(),
        )

    def stop(self) -> None:
        self._started = False

    @property
    def running(self) -> bool:
        return self._started

    # -- remember ----------------------------------------------------------

    def remember(
        self,
        content: str,
        *,
        kind: MemoryKind = MemoryKind.FACT,
        scope: MemoryScope = MemoryScope.USER,
        importance: float = 0.5,
        pinned: bool = False,
        source: str = "user",
    ) -> MemoryRecord:
        """Store a memory, deduplicated, and index it for semantic recall.

        Raises:
            JarvisError: if the service has not been started.
        """
        self._require()
        text = content.strip()
        if not text:
            raise JarvisError("记忆内容不能为空")
        record = self._repo.upsert(
            scope=scope,
            kind=kind,
            content=text,
            source=source,
            importance=min(1.0, max(0.0, importance)),
            pinned=pinned,
        )
        self._index(record)
        logger.debug("remembered %s/%s: %s", scope.value, kind.value, text[:60])
        return record

    def remember_many(
        self,
        items: Sequence[tuple[str, MemoryKind]],
        *,
        scope: MemoryScope = MemoryScope.USER,
        source: str = "extractor",
    ) -> list[MemoryRecord]:
        """Store several memories; failures on one do not abort the rest."""
        stored: list[MemoryRecord] = []
        for content, kind in items:
            try:
                stored.append(
                    self.remember(content, kind=kind, scope=scope, source=source, importance=0.6)
                )
            except JarvisError as exc:
                logger.warning("skipped a memory: %s", exc)
        return stored

    # -- recall ------------------------------------------------------------

    def recall(
        self,
        query: str,
        *,
        scope: MemoryScope = MemoryScope.USER,
        top_k: int = 0,
        min_score: float = -1.0,
        kinds: Sequence[MemoryKind] | None = None,
    ) -> list[RecallHit]:
        """Retrieve the memories most worth putting in front of the model.

        Args:
            query: What the user just said.
            scope: Which bucket to search.
            top_k: Maximum hits; ``0`` means "use the configured default".
            min_score: Floor for the blended score; negative means "use config".
            kinds: Optional kind filter applied after ranking.

        Returns:
            Hits, highest score first. Empty when nothing clears the floor —
            an empty list is the honest answer, and a wrong memory recited
            confidently is worse than no memory at all.
        """
        self._require()
        settings = self._settings_provider()
        limit = top_k if top_k > 0 else settings.max_recall
        floor = min_score if min_score >= 0 else settings.min_score
        if limit <= 0 or not query.strip():
            return []

        now = datetime.now(UTC)
        scores: dict[int, float] = {}
        records: dict[int, MemoryRecord] = {}
        matched_by: dict[int, set[str]] = {}

        def note(record: MemoryRecord, score: float, path: str) -> None:
            records[record.memory_id] = record
            scores[record.memory_id] = max(scores.get(record.memory_id, 0.0), score)
            matched_by.setdefault(record.memory_id, set()).add(path)

        # Pinned memories bypass ranking entirely: the operator said "always".
        for record in self._repo.pinned(scope):
            note(record, 1.0 + record.importance, "pinned")

        for hit in self._vector.search(COLLECTION_PREFIX + scope.value, query, top_k=limit * 3):
            found = self._repo.get(as_int(hit.record_id))
            if found is not None:
                note(found, hit.score, "semantic")

        tokens = _tokens(query)
        for record in self._repo.keyword_search(scope, tokens, limit=limit * 3):
            note(record, KEYWORD_WEIGHT * match_ratio(record.content, tokens), "keyword")

        if not records:
            return []

        hits: list[RecallHit] = []
        for memory_id, record in records.items():
            if kinds and record.kind not in kinds:
                continue
            score = (
                scores[memory_id]
                + IMPORTANCE_WEIGHT * record.importance
                + _recency_boost(record.updated_at, now)
                + ACCESS_WEIGHT * math.log1p(record.access_count)
            )
            if score < floor:
                continue
            hits.append(
                RecallHit(
                    record=record,
                    score=score,
                    matched_by=tuple(sorted(matched_by.get(memory_id, ()))),
                )
            )
        hits.sort(key=lambda hit: hit.score, reverse=True)
        selected = hits[:limit]
        self._repo.touch([hit.record.memory_id for hit in selected])
        logger.debug("recall(%r) -> %d/%d memories", query[:40], len(selected), len(records))
        return selected

    def profile_block(self, scope: MemoryScope = MemoryScope.USER, *, limit: int = 8) -> str:
        """Render the always-inject memories as a prompt fragment.

        Preferences first, then the most important facts: a prompt that opens
        with "用户希望回答简短" is more useful than one that opens with an
        address. Returns an empty string when there is nothing to say, so the
        caller can concatenate unconditionally.
        """
        self._require()
        preferences = self._repo.all_for(scope, kinds=[MemoryKind.PREFERENCE], limit=limit)
        facts = self._repo.all_for(scope, kinds=[MemoryKind.FACT], limit=limit)
        lines = [f"- [偏好] {record.content}" for record in preferences] + [
            f"- [事实] {record.content}" for record in facts[: max(0, limit - len(preferences))]
        ]
        if not lines:
            return ""
        return "关于用户的长期记忆（供参考，不要生硬复述）：\n" + "\n".join(lines)

    # -- forget ------------------------------------------------------------

    def forget(self, memory_id: int) -> bool:
        """Delete one memory from both the table and the index."""
        self._require()
        removed = self._repo.delete(memory_id)
        if removed:
            for scope in MemoryScope:
                self._vector.forget(COLLECTION_PREFIX + scope.value, [str(memory_id)])
        return removed

    def forget_scope(self, scope: MemoryScope = MemoryScope.USER) -> int:
        """Delete every memory in a scope."""
        self._require()
        removed = self._repo.delete_scope(scope)
        self._vector.drop(COLLECTION_PREFIX + scope.value)
        logger.info("forgot %d memories in scope %s", removed, scope.value)
        return removed

    def list_memories(
        self,
        scope: MemoryScope = MemoryScope.USER,
        *,
        kinds: Sequence[MemoryKind] | None = None,
        limit: int = 50,
    ) -> list[MemoryRecord]:
        """List stored memories for the management panel."""
        self._require()
        return self._repo.all_for(scope, kinds=kinds, limit=limit)

    def prune(self, scope: MemoryScope = MemoryScope.USER, *, keep: int) -> int:
        """Drop the least valuable memories beyond ``keep``."""
        self._require()
        removed = self._repo.prune(scope, keep=keep)
        if removed:
            self._reindex(scope)
        return removed

    # -- conversation ------------------------------------------------------

    def record_turn(self, session_id: str, role: str, content: str) -> ConversationTurn:
        """Persist one conversation turn."""
        self._require()
        return self._repo.append_turn(session_id, role, content)

    def history(self, session_id: str, *, limit: int = 0) -> list[ChatMessage]:
        """Replay a session's recent turns as chat messages.

        Args:
            session_id: Session to read.
            limit: Turns to return; ``0`` means "use the configured default".
        """
        self._require()
        settings = self._settings_provider()
        count = limit if limit > 0 else settings.history_turns
        turns = self._repo.recent_turns(session_id, limit=count)
        return [ChatMessage(role=self._role(turn.role), content=turn.content) for turn in turns]

    @staticmethod
    def _role(raw: str) -> Role:
        try:
            return Role(raw)
        except ValueError:
            logger.warning("unknown turn role %r; treating as user", raw)
            return Role.USER

    def observe_turn(
        self,
        session_id: str,
        user_text: str,
        assistant_text: str = "",
    ) -> list[MemoryRecord]:
        """Record a turn and, if enabled, mine it for durable memories.

        Called after a reply has been produced, never before: memory extraction
        must not sit between the user and their answer.
        """
        self._require()
        self.record_turn(session_id, Role.USER.value, user_text)
        if assistant_text:
            self.record_turn(session_id, Role.ASSISTANT.value, assistant_text)

        settings = self._settings_provider()
        if not settings.auto_extract or self._llm_provider is None:
            return []
        try:
            client = self._llm_provider()
        except JarvisError as exc:
            logger.debug("memory extraction unavailable: %s", exc)
            return []
        extractor = MemoryExtractor(client, max_items=settings.max_extract)
        proposals = extractor.extract(user_text, assistant_text)
        if not proposals:
            return []
        stored: list[MemoryRecord] = []
        for proposal in proposals:
            try:
                stored.append(
                    self.remember(
                        proposal.content,
                        kind=proposal.kind,
                        importance=proposal.importance,
                        source="extractor",
                    )
                )
            except JarvisError as exc:
                logger.warning("could not store an extracted memory: %s", exc)
        return stored

    def summarize_session(self, session_id: str) -> MemoryRecord | None:
        """Compress a session's older turns into one summary memory.

        The raw turns are kept: a summary is a retrieval aid, not a licence to
        destroy the record. Returns ``None`` when there is nothing to do or no
        model is available.
        """
        self._require()
        settings = self._settings_provider()
        if self._llm_provider is None or settings.summarize_after_turns <= 0:
            return None
        turns = self._repo.recent_turns(session_id, limit=settings.summarize_after_turns)
        if len(turns) < settings.summarize_after_turns:
            return None
        transcript = "\n".join(
            f"{'用户' if turn.role == Role.USER.value else '助手'}：{turn.content}"
            for turn in turns
        )
        try:
            client = self._llm_provider()
            reply = client.complete(
                [
                    ChatMessage.system(render_prompt("memory_summary")),
                    ChatMessage.user(transcript),
                ]
            ).content
        except JarvisError as exc:
            logger.warning("session summarisation skipped: %s", exc)
            return None
        except Exception:  # pragma: no cover - defensive
            logger.exception("session summarisation failed unexpectedly")
            return None
        summary = reply.strip()
        if not summary:
            return None
        return self.remember(
            summary,
            kind=MemoryKind.SUMMARY,
            importance=0.4,
            source=f"summary:{session_id}",
        )

    def clear_session(self, session_id: str) -> int:
        """Delete a session's turns (its extracted memories survive)."""
        self._require()
        return self._repo.clear_session(session_id)

    # -- introspection -----------------------------------------------------

    def stats(self) -> dict[str, object]:
        """Read-only snapshot for the HUD and the health check."""
        return {
            "running": self._started,
            "total": self._repo.count() if self._started else 0,
            "by_kind": self._repo.count_by_kind() if self._started else {},
            "sessions": self._repo.sessions(limit=10) if self._started else [],
            "vector": self._vector.stats(),
        }

    # -- internals ---------------------------------------------------------

    def _require(self) -> None:
        if not self._started:
            raise JarvisError("记忆服务未启动")

    def _index(self, record: MemoryRecord) -> None:
        """Index one memory, tolerating an unavailable embedder.

        A cloud embedder with no key configured is a normal state on a fresh
        install; the memory is still stored and still findable by keyword, so a
        failed index must not fail the write.
        """
        from jarvis.vector import VectorEntry

        try:
            self._vector.index(
                COLLECTION_PREFIX + record.scope.value,
                [
                    VectorEntry(
                        record_id=str(record.memory_id),
                        text=record.content,
                        metadata={"kind": record.kind.value, "scope": record.scope.value},
                    )
                ],
            )
        except JarvisError as exc:
            logger.warning("memory %d stored but not indexed: %s", record.memory_id, exc)

    def _reindex(self, scope: MemoryScope) -> None:
        """Rebuild a scope's index from the table (used after pruning)."""
        from jarvis.vector import VectorEntry

        collection = COLLECTION_PREFIX + scope.value
        self._vector.drop(collection)
        records = self._repo.all_for(scope)
        if not records:
            return
        try:
            self._vector.index(
                collection,
                [
                    VectorEntry(
                        record_id=str(record.memory_id),
                        text=record.content,
                        metadata={"kind": record.kind.value, "scope": record.scope.value},
                    )
                    for record in records
                ],
            )
        except JarvisError as exc:
            logger.warning("could not rebuild the %s memory index: %s", scope.value, exc)


__all__ = ["COLLECTION_PREFIX", "MemoryService"]
