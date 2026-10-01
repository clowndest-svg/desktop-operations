"""Embedding engines.

Two of them, chosen by ``vector.embedding.engine``:

``hashing`` (default)
    A deterministic bag-of-character-n-grams hashed into a fixed width. It needs
    no model, no network and no key, and it is *good enough* for the job the
    knowledge base actually does — finding the three passages of a manual that
    talk about the thing the user just asked about. It is the default because the
    alternative makes a fresh install depend on an API key before the knowledge
    base will accept a single document.

``http``
    Any OpenAI-compatible ``/embeddings`` endpoint (DeepSeek, Qwen, OpenAI,
    a local server). Strictly better recall than ``hashing``, at the cost of a
    network round trip per batch and a key.

Neither engine lives in ``jarvis.llm``: the architecture table gives ``vector``
``core`` and ``config`` only, so the HTTP call is a small stdlib POST rather than
a reuse of the chat transport. That is a deliberate trade — twenty lines of
``urllib`` against a layer violation that the test suite would reject.
"""

from __future__ import annotations

import hashlib
import json
import logging
import urllib.error
import urllib.request
from collections.abc import Mapping, Sequence

from jarvis.core.constants import DEFAULT_ENCODING
from jarvis.core.exceptions import VectorStoreError
from jarvis.vector.types import Embedder, Vector, l2_normalize

logger = logging.getLogger("jarvis.vector.embedder")

DEFAULT_HASHING_DIMENSION: int = 512
"""Width of the offline embedding. 512 is the knee of the curve for this corpus
size: recall stops improving noticeably past it while the index keeps growing."""

_CJK_RANGES: tuple[tuple[int, int], ...] = (
    (0x3400, 0x4DBF),  # CJK Extension A
    (0x4E00, 0x9FFF),  # CJK Unified Ideographs
    (0xF900, 0xFAFF),  # Compatibility Ideographs
)
"""Ranges treated as "one character is a word". Used to decide whether a token
should be exploded into character n-grams — Chinese has no spaces, so a
whitespace token is a whole sentence and hashing it whole would match nothing."""


def _is_cjk(character: str) -> bool:
    code = ord(character)
    return any(low <= code <= high for low, high in _CJK_RANGES)


def _tokenize(text: str) -> list[str]:
    """Split into matchable units: words, plus character n-grams for CJK runs.

    Latin text keeps its words (``deploy`` matches ``deploy``); CJK runs are cut
    into single characters and adjacent pairs, which is the standard cheap
    substitute for a segmentation dictionary.
    """
    tokens: list[str] = []
    word: list[str] = []

    def flush() -> None:
        if not word:
            return
        chunk = "".join(word)
        word.clear()
        if any(_is_cjk(character) for character in chunk):
            tokens.extend(chunk)
            tokens.extend(chunk[index : index + 2] for index in range(len(chunk) - 1))
        elif len(chunk) > 1:
            tokens.append(chunk)

    for character in text:
        if character.isalnum() or _is_cjk(character):
            word.append(character)
        else:
            flush()
    flush()
    return tokens


class HashingEmbedder:
    """Offline, dependency-free embedder using signed feature hashing.

    The sign is what makes it work: without it, two different tokens landing in
    the same bucket always reinforce each other and every document drifts toward
    looking similar to every other one. With it, collisions cancel on average.
    """

    def __init__(self, *, dimension: int = DEFAULT_HASHING_DIMENSION) -> None:
        if dimension < 8:
            raise VectorStoreError(
                f"哈希嵌入维度太小：{dimension}", details={"dimension": dimension}
            )
        self._dimension = dimension

    @property
    def name(self) -> str:
        return "hashing"

    @property
    def dimension(self) -> int:
        return self._dimension

    def embed(self, texts: Sequence[str]) -> list[Vector]:
        return [self._embed_one(text) for text in texts]

    def _embed_one(self, text: str) -> Vector:
        buckets = [0.0] * self._dimension
        for token in _tokenize(text.lower()):
            digest = hashlib.blake2b(token.encode(DEFAULT_ENCODING), digest_size=8).digest()
            value = int.from_bytes(digest, "little")
            index = value % self._dimension
            buckets[index] += 1.0 if value >> 63 else -1.0
        return l2_normalize(buckets)


class HttpEmbedder:
    """OpenAI-compatible ``/embeddings`` client (stdlib HTTP only)."""

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        api_key: str,
        timeout_seconds: float = 30.0,
        batch_size: int = 32,
        dimension: int = 0,
    ) -> None:
        """Create the client.

        Args:
            base_url: API root, e.g. ``https://api.deepseek.com/v1``.
            model: Embedding model id.
            api_key: Bearer token. Empty is accepted at construction (JARVIS
                boots offline) and rejected on first use.
            timeout_seconds: Per-request timeout.
            batch_size: Inputs per request; long documents are chunked to this.
            dimension: Expected width. ``0`` means "learn it from the first
                response" — the endpoint's answer is authoritative.
        """
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._api_key = api_key
        self._timeout = timeout_seconds
        self._batch_size = max(1, batch_size)
        self._dimension = dimension

    @property
    def name(self) -> str:
        return f"http:{self._model}"

    @property
    def dimension(self) -> int:
        """Width of the vectors this endpoint returns.

        Raises:
            VectorStoreError: before the first successful call, when the
                configured dimension is 0. Callers that need a width up front
                (schema decisions, empty-index handling) should configure it.
        """
        if self._dimension <= 0:
            raise VectorStoreError(
                "尚未确定嵌入维度：请先调用一次 embed()，或在配置里写明 vector.embedding.dimension"
            )
        return self._dimension

    def embed(self, texts: Sequence[str]) -> list[Vector]:
        if not texts:
            return []
        if not self._api_key:
            raise VectorStoreError(
                "未配置嵌入模型 API Key（请设置对应的环境变量）",
                details={"model": self._model},
            )
        vectors: list[Vector] = []
        for start in range(0, len(texts), self._batch_size):
            batch = list(texts[start : start + self._batch_size])
            vectors.extend(self._request(batch))
        return vectors

    def _request(self, batch: Sequence[str]) -> list[Vector]:
        payload = {"model": self._model, "input": batch}
        request = urllib.request.Request(
            url=f"{self._base_url}/embeddings",
            data=json.dumps(payload, ensure_ascii=False).encode(DEFAULT_ENCODING),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self._api_key}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                body = json.loads(response.read().decode(DEFAULT_ENCODING))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(DEFAULT_ENCODING, errors="replace")[:200]
            raise VectorStoreError(
                f"嵌入接口返回 HTTP {exc.code}",
                details={"model": self._model, "body": detail},
            ) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise VectorStoreError(
                f"嵌入接口连接失败：{exc}",
                details={"model": self._model, "base_url": self._base_url},
            ) from exc
        except json.JSONDecodeError as exc:
            raise VectorStoreError("嵌入接口返回的不是合法 JSON") from exc

        return self._parse(body, expected=len(batch))

    def _parse(self, body: object, *, expected: int) -> list[Vector]:
        if not isinstance(body, Mapping):
            raise VectorStoreError("嵌入接口返回结构异常：顶层不是对象")
        raw_items = body.get("data")
        if not isinstance(raw_items, list) or len(raw_items) != expected:
            raise VectorStoreError(
                f"嵌入接口返回条数不符：期望 {expected}，实际 "
                f"{len(raw_items) if isinstance(raw_items, list) else '未知'}"
            )
        vectors: list[Vector] = []
        for item in raw_items:
            if not isinstance(item, Mapping):
                raise VectorStoreError("嵌入接口返回结构异常：data 元素不是对象")
            raw_vector = item.get("embedding")
            if not isinstance(raw_vector, list) or not raw_vector:
                raise VectorStoreError("嵌入接口返回结构异常：embedding 缺失或为空")
            vector = tuple(float(value) for value in raw_vector)
            if self._dimension and len(vector) != self._dimension:
                raise VectorStoreError(
                    f"嵌入维度与配置不符：配置 {self._dimension}，实际 {len(vector)}",
                    details={"model": self._model},
                )
            self._dimension = len(vector)
            vectors.append(vector)
        return vectors


def build_embedder(
    engine: str,
    *,
    dimension: int,
    base_url: str,
    model: str,
    api_key: str,
    timeout_seconds: float,
    batch_size: int,
) -> Embedder:
    """Construct the embedder named by ``engine``.

    Raises:
        VectorStoreError: on an unknown engine id.
    """
    if engine == "hashing":
        return HashingEmbedder(dimension=dimension)
    if engine == "http":
        return HttpEmbedder(
            base_url=base_url,
            model=model,
            api_key=api_key,
            timeout_seconds=timeout_seconds,
            batch_size=batch_size,
            dimension=dimension,
        )
    raise VectorStoreError(f"未知的嵌入引擎：{engine}", details={"engine": engine})
