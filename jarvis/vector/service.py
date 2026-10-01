"""``VectorService`` — the lifecycle component the upper layers talk to.

Holds the one embedder instance and the one index, so ``memory`` and
``knowledge`` cannot each construct their own and quietly disagree about what a
vector means. Both call through here.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING

from jarvis.core.exceptions import VectorStoreError
from jarvis.vector.embedder import build_embedder
from jarvis.vector.store import MIGRATIONS, NAMESPACE, SqliteVectorStore
from jarvis.vector.types import Embedder, SearchHit, Vector, VectorEntry

if TYPE_CHECKING:  # pragma: no cover - typing only
    from jarvis.config.schema import VectorSection
    from jarvis.database import SqliteStore

logger = logging.getLogger("jarvis.vector.service")


class VectorService:
    """Embeddings plus similarity search over the shared database."""

    name = "vector"

    def __init__(
        self,
        store: SqliteStore,
        settings_provider: Callable[[], VectorSection],
        *,
        embedder: Embedder | None = None,
    ) -> None:
        """Create the service.

        Args:
            store: The shared database. Must already be started; the composition
                root registers ``database`` before ``vector``.
            settings_provider: Returns the validated ``vector`` config section.
                A provider rather than the section itself because configuration
                does not exist yet at registration time.
            embedder: Optional override; tests inject a deterministic stub and
                skip the config path entirely.
        """
        self._store = store
        self._settings_provider = settings_provider
        self._override = embedder
        self._index = SqliteVectorStore(store)
        self._embedder: Embedder | None = None
        self._started = False

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        """Apply the schema and build the embedder (idempotent, no network)."""
        if self._started:
            return
        self._store.migrate(NAMESPACE, MIGRATIONS)
        self._embedder = self._override or self._build_embedder()
        self._started = True
        logger.info(
            "vector service ready (engine=%s, dimension=%d, records=%d)",
            self._embedder.name,
            self._safe_dimension(),
            self._index.count(),
        )

    def stop(self) -> None:
        """Release the embedder. The database outlives this component."""
        self._embedder = None
        self._started = False

    @property
    def running(self) -> bool:
        """Whether the service is usable."""
        return self._started

    @property
    def embedder(self) -> Embedder:
        """The active embedder.

        Raises:
            VectorStoreError: if the service has not been started.
        """
        if self._embedder is None:
            raise VectorStoreError("向量服务未启动")
        return self._embedder

    # -- operations --------------------------------------------------------

    def embed(self, texts: Sequence[str]) -> list[Vector]:
        """Embed a batch. Returns ``[]`` for an empty batch without calling out."""
        if not texts:
            return []
        return self.embedder.embed(texts)

    def index(self, collection: str, entries: Sequence[VectorEntry]) -> int:
        """Embed and store ``entries``; returns how many rows were written.

        Raises:
            VectorStoreError: if the service is stopped or the embedder fails.
        """
        if not entries:
            return 0
        embedder = self.embedder
        vectors = embedder.embed([entry.text for entry in entries])
        return self._index.upsert(collection, entries, vectors, engine=embedder.name)

    def search(
        self,
        collection: str,
        query: str,
        *,
        top_k: int = 5,
        min_score: float = 0.0,
    ) -> list[SearchHit]:
        """Retrieve the passages closest to ``query``.

        An empty collection short-circuits before the embedder runs: asking a
        cloud endpoint to embed a query that can match nothing is a wasted round
        trip, and the caller gets the same empty list either way.
        """
        if not query.strip() or top_k <= 0:
            return []
        if self._index.count(collection) == 0:
            return []
        vector = self.embed([query])[0]
        return self._index.search(collection, vector, top_k=top_k, min_score=min_score)

    def forget(self, collection: str, record_ids: Sequence[str]) -> int:
        """Delete specific records."""
        return self._index.delete(collection, record_ids)

    def drop(self, collection: str) -> int:
        """Delete an entire collection."""
        return self._index.delete_collection(collection)

    def get(self, collection: str, record_id: str) -> SearchHit | None:
        """Fetch one record's text and metadata without scoring."""
        return self._index.get(collection, record_id)

    def count(self, collection: str | None = None) -> int:
        """Indexed record count."""
        return self._index.count(collection)

    def collections(self) -> list[dict[str, object]]:
        """Per-collection summary for the HUD's storage panel."""
        return self._index.collections()

    def stats(self) -> dict[str, object]:
        """Read-only snapshot, safe to call when the service is stopped."""
        return {
            "running": self._started,
            "engine": self._embedder.name if self._embedder else "",
            "dimension": self._safe_dimension(),
            "records": self._index.count(),
            "collections": self._index.collections(),
        }

    # -- helpers -----------------------------------------------------------

    def _build_embedder(self) -> Embedder:
        section = self._settings_provider()
        api_key = os.environ.get(section.embedding.api_key_env, "").strip()
        if section.embedding.engine == "http" and not api_key:
            # Not fatal: the assistant still boots and still answers questions.
            # Retrieval is what breaks, and it says so with a message naming the
            # variable, rather than failing at startup with a stack trace.
            logger.warning(
                "embedding engine is 'http' but %s is unset; "
                "knowledge/memory retrieval will fail until it is set",
                section.embedding.api_key_env,
            )
        return build_embedder(
            section.embedding.engine,
            dimension=section.embedding.dimension,
            base_url=section.embedding.base_url,
            model=section.embedding.model,
            api_key=api_key,
            timeout_seconds=section.embedding.timeout_seconds,
            batch_size=section.embedding.batch_size,
        )

    def _safe_dimension(self) -> int:
        if self._embedder is None:
            return 0
        try:
            return self._embedder.dimension
        except VectorStoreError:
            # http engine before its first call: the width is genuinely unknown.
            return 0


__all__ = ["VectorService"]
