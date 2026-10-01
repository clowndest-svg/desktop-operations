"""Vector vocabulary: embeddings, records, hits, and their on-disk encoding.

Embeddings are plain tuples of floats rather than numpy arrays. The distances
this project computes are over hundreds of rows of a few hundred dimensions —
small enough that numpy's import cost and its status as an undeclared native
dependency outweigh the arithmetic it would save. The optional acceleration in
:mod:`jarvis.vector.store` kicks in only when numpy happens to be installed
anyway (``funasr`` pulls it in on a voice install).

Encoding note: vectors are written as **little-endian float32**. ``array('f')``
would be faster but writes native byte order, which silently produces garbage if
a database file is copied from one architecture to another.
"""

from __future__ import annotations

import struct
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

type Vector = tuple[float, ...]
"""A dense embedding."""

FLOAT32_FORMAT: str = "<f"
"""Little-endian float32, explicit so the file is portable."""

FLOAT32_BYTES: int = 4
"""Bytes per element of :data:`FLOAT32_FORMAT`."""


def encode_vector(vector: Vector) -> bytes:
    """Pack a vector into the on-disk BLOB representation."""
    return struct.pack(f"<{len(vector)}f", *vector)


def decode_vector(blob: bytes) -> Vector:
    """Unpack a BLOB written by :func:`encode_vector`.

    Raises:
        ValueError: if the blob length is not a whole number of float32s.
    """
    if len(blob) % FLOAT32_BYTES:
        raise ValueError(f"向量字节数 {len(blob)} 不是 4 的倍数")
    return struct.unpack(f"<{len(blob) // FLOAT32_BYTES}f", blob)


def l2_normalize(vector: Sequence[float]) -> Vector:
    """Scale ``vector`` to unit length so cosine similarity is a dot product.

    A zero vector is returned unchanged: it has no direction, and dividing by
    its norm would turn a harmless empty embedding into ``nan`` scores that
    poison every comparison downstream.
    """
    total = 0.0
    for value in vector:
        total += value * value
    if total <= 0.0:
        return tuple(float(value) for value in vector)
    norm = total**0.5
    return tuple(float(value) / norm for value in vector)


def dot(left: Sequence[float], right: Sequence[float]) -> float:
    """Dot product of two equal-length vectors.

    Raises:
        ValueError: if the lengths differ — a dimension mismatch between the
            query and the index is a configuration bug, not a ranking detail.
    """
    if len(left) != len(right):
        raise ValueError(f"向量维度不一致：{len(left)} vs {len(right)}")
    total = 0.0
    for a, b in zip(left, right, strict=True):
        total += a * b
    return total


@dataclass(frozen=True, slots=True)
class VectorEntry:
    """One thing to embed and store."""

    record_id: str
    """Stable id chosen by the caller (a chunk id, a memory id...)."""

    text: str
    """The text the embedding was produced from (kept for keyword fallback)."""

    metadata: Mapping[str, object] = field(default_factory=dict)
    """Arbitrary JSON-serialisable payload returned with search hits."""


@dataclass(frozen=True, slots=True)
class SearchHit:
    """One retrieved record with its similarity score."""

    record_id: str
    score: float
    """Cosine similarity in [-1, 1]; higher is closer."""

    text: str
    metadata: Mapping[str, object] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        return {
            "record_id": self.record_id,
            "score": round(self.score, 6),
            "text": self.text,
            "metadata": dict(self.metadata),
        }


@runtime_checkable
class Embedder(Protocol):
    """Turns text into vectors. The seam that keeps the store model-agnostic."""

    @property
    def name(self) -> str:
        """Engine id, logged and stored alongside the vectors."""
        ...

    @property
    def dimension(self) -> int:
        """Vector width this embedder produces."""
        ...

    def embed(self, texts: Sequence[str]) -> list[Vector]:
        """Embed a batch, preserving order.

        Raises:
            VectorStoreError: on any engine failure.
        """
        ...
