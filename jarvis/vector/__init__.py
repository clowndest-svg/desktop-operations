"""Vector store: embedding-backed similarity search over SQLite.

Responsibility (delivered in phase 11):
    * Typed wrapper around the vector index (record lifecycle, persistence,
      search) plus the embedder abstraction. ``memory`` and ``knowledge`` call
      through :class:`VectorService`; neither constructs an embedder itself.

Why SQLite rather than FAISS: the native wheel is a build-time liability on a
Windows-first project and produces opaque index files that cannot be inspected
when a retrieval result looks wrong. At this corpus size a linear scan is
sub-millisecond. See :mod:`jarvis.vector.store` for the full reasoning.

Allowed dependencies: ``core``, ``config``.
"""

from jarvis.vector.embedder import (
    DEFAULT_HASHING_DIMENSION,
    HashingEmbedder,
    HttpEmbedder,
    build_embedder,
)
from jarvis.vector.service import VectorService
from jarvis.vector.store import MIGRATIONS, NAMESPACE, SqliteVectorStore
from jarvis.vector.types import (
    Embedder,
    SearchHit,
    Vector,
    VectorEntry,
    decode_vector,
    dot,
    encode_vector,
    l2_normalize,
)

__all__ = [
    "DEFAULT_HASHING_DIMENSION",
    "MIGRATIONS",
    "NAMESPACE",
    "Embedder",
    "HashingEmbedder",
    "HttpEmbedder",
    "SearchHit",
    "SqliteVectorStore",
    "Vector",
    "VectorEntry",
    "VectorService",
    "build_embedder",
    "decode_vector",
    "dot",
    "encode_vector",
    "l2_normalize",
]
