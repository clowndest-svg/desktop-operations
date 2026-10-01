"""Tests for the vector layer (jarvis.vector)."""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path

import pytest

from jarvis.config.schema import VectorSection
from jarvis.core.exceptions import VectorStoreError
from jarvis.database import SqliteStore
from jarvis.vector import (
    HashingEmbedder,
    HttpEmbedder,
    SqliteVectorStore,
    Vector,
    VectorEntry,
    VectorService,
    build_embedder,
    decode_vector,
    dot,
    encode_vector,
    l2_normalize,
)
from jarvis.vector.embedder import _tokenize


def _store(tmp_path: Path) -> SqliteStore:
    store = SqliteStore(tmp_path / "vector.db")
    store.start()
    return store


class _StubEmbedder:
    """Deterministic 4-dim embedder: counts of a, b, c, d. No model, no maths."""

    name = "stub"

    @property
    def dimension(self) -> int:
        return 4

    def embed(self, texts: Sequence[str]) -> list[Vector]:
        vectors: list[Vector] = []
        for text in texts:
            lowered = text.lower()
            vectors.append(
                l2_normalize(
                    (
                        float(lowered.count("a")),
                        float(lowered.count("b")),
                        float(lowered.count("c")),
                        float(lowered.count("d")),
                    )
                )
            )
        return vectors


class _FakeResponse:
    def __init__(self, payload: object) -> None:
        self._body = json.dumps(payload).encode("utf-8")

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *_: object) -> None:
        return None


class TestVectorEncoding:
    def test_roundtrip(self) -> None:
        vector = (0.5, -1.25, 3.0)
        assert decode_vector(encode_vector(vector)) == pytest.approx(vector)

    def test_blob_is_little_endian_float32(self) -> None:
        """Explicit byte order keeps a database file portable; ``array('f')``
        would write native order and produce garbage on a big-endian copy."""
        assert encode_vector((1.0,)) == b"\x00\x00\x80\x3f"

    def test_ragged_blob_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="4 的倍数"):
            decode_vector(b"\x00\x00\x00")


class TestSimilarity:
    def test_normalisation_makes_self_similarity_one(self) -> None:
        assert dot(l2_normalize((3.0, 4.0)), l2_normalize((3.0, 4.0))) == pytest.approx(1.0)

    def test_zero_vector_is_left_alone(self) -> None:
        """Dividing by a zero norm would turn an empty embedding into ``nan``
        scores that poison every comparison downstream."""
        assert l2_normalize((0.0, 0.0)) == (0.0, 0.0)

    def test_dimension_mismatch_raises(self) -> None:
        with pytest.raises(ValueError, match="维度不一致"):
            dot((1.0, 2.0), (1.0,))


class TestTokenizer:
    def test_chinese_is_cut_into_characters_and_pairs(self) -> None:
        """Whitespace tokens in Chinese are whole sentences; hashing them whole
        would match nothing, so the runs are exploded."""
        tokens = _tokenize("你好世界")
        assert "你" in tokens
        assert "你好" in tokens

    def test_latin_keeps_words(self) -> None:
        tokens = _tokenize("deploy the service")
        assert "deploy" in tokens

    def test_punctuation_splits(self) -> None:
        assert _tokenize("a,b") == _tokenize("a b")


class TestHashingEmbedder:
    def test_is_deterministic(self) -> None:
        embedder = HashingEmbedder(dimension=32)
        assert embedder.embed(["你好"])[0] == embedder.embed(["你好"])[0]

    def test_returns_unit_vectors(self) -> None:
        vector = HashingEmbedder(dimension=64).embed(["部署一个服务"])[0]
        assert dot(vector, vector) == pytest.approx(1.0)

    def test_dimension_is_respected(self) -> None:
        assert len(HashingEmbedder(dimension=17).embed(["x"])[0]) == 17

    def test_identical_text_scores_higher_than_unrelated(self) -> None:
        embedder = HashingEmbedder(dimension=512)
        query, same, other = embedder.embed(["如何备份数据库", "如何备份数据库", "今天天气不错"])
        assert dot(query, same) > dot(query, other)

    def test_tiny_dimension_is_refused(self) -> None:
        with pytest.raises(VectorStoreError, match="太小"):
            HashingEmbedder(dimension=4)


class TestBuildEmbedder:
    def test_hashing(self) -> None:
        embedder = build_embedder(
            "hashing",
            dimension=16,
            base_url="",
            model="",
            api_key="",
            timeout_seconds=1.0,
            batch_size=1,
        )
        assert isinstance(embedder, HashingEmbedder)

    def test_http(self) -> None:
        embedder = build_embedder(
            "http",
            dimension=0,
            base_url="https://example.test/v1",
            model="m",
            api_key="k",
            timeout_seconds=1.0,
            batch_size=2,
        )
        assert isinstance(embedder, HttpEmbedder)

    def test_unknown_engine(self) -> None:
        with pytest.raises(VectorStoreError, match="未知的嵌入引擎"):
            build_embedder(
                "nope",
                dimension=1,
                base_url="",
                model="",
                api_key="",
                timeout_seconds=1.0,
                batch_size=1,
            )


class TestHttpEmbedder:
    def test_missing_key_is_refused_before_any_request(self) -> None:
        """JARVIS boots offline; the failure belongs at first use, with a message
        naming the variable, not at startup with a stack trace."""
        embedder = HttpEmbedder(base_url="https://x.test/v1", model="m", api_key="")
        with pytest.raises(VectorStoreError, match="API Key"):
            embedder.embed(["hi"])

    def test_empty_batch_makes_no_request(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls: list[str] = []
        monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: calls.append("called"))
        embedder = HttpEmbedder(base_url="https://x.test/v1", model="m", api_key="k")
        assert embedder.embed([]) == []
        assert not calls

    def test_parses_a_response(self, monkeypatch: pytest.MonkeyPatch) -> None:
        payload = {"data": [{"embedding": [1, 2, 3]}, {"embedding": [4, 5, 6]}]}
        monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: _FakeResponse(payload))
        embedder = HttpEmbedder(base_url="https://x.test/v1", model="m", api_key="k")
        assert embedder.embed(["a", "b"]) == [(1.0, 2.0, 3.0), (4.0, 5.0, 6.0)]
        assert embedder.dimension == 3

    def test_batching_splits_requests(self, monkeypatch: pytest.MonkeyPatch) -> None:
        seen: list[int] = []

        def fake_urlopen(request: object, timeout: float = 0) -> _FakeResponse:
            body = json.loads(getattr(request, "data", b"{}").decode("utf-8"))
            seen.append(len(body["input"]))
            return _FakeResponse({"data": [{"embedding": [0.0, 1.0]} for _ in body["input"]]})

        monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
        embedder = HttpEmbedder(base_url="https://x.test/v1", model="m", api_key="k", batch_size=2)
        assert len(embedder.embed(["a", "b", "c", "d", "e"])) == 5
        assert seen == [2, 2, 1]

    def test_dimension_mismatch_is_reported(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "urllib.request.urlopen",
            lambda *a, **k: _FakeResponse({"data": [{"embedding": [1.0, 2.0]}]}),
        )
        embedder = HttpEmbedder(base_url="https://x.test/v1", model="m", api_key="k", dimension=3)
        with pytest.raises(VectorStoreError, match="维度与配置不符"):
            embedder.embed(["a"])

    def test_short_response_is_reported(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "urllib.request.urlopen",
            lambda *a, **k: _FakeResponse({"data": [{"embedding": [1.0]}]}),
        )
        embedder = HttpEmbedder(base_url="https://x.test/v1", model="m", api_key="k")
        with pytest.raises(VectorStoreError, match="条数不符"):
            embedder.embed(["a", "b"])

    def test_garbage_body_is_reported(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: _FakeResponse({"nope": True}))
        embedder = HttpEmbedder(base_url="https://x.test/v1", model="m", api_key="k")
        with pytest.raises(VectorStoreError, match="条数不符"):
            embedder.embed(["a"])

    def test_connection_failure_is_wrapped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import urllib.error

        def boom(*_: object, **__: object) -> None:
            raise urllib.error.URLError("refused")

        monkeypatch.setattr("urllib.request.urlopen", boom)
        embedder = HttpEmbedder(base_url="https://x.test/v1", model="m", api_key="k")
        with pytest.raises(VectorStoreError, match="连接失败"):
            embedder.embed(["a"])

    def test_dimension_unknown_until_first_call(self) -> None:
        embedder = HttpEmbedder(base_url="https://x.test/v1", model="m", api_key="k")
        with pytest.raises(VectorStoreError, match="尚未确定嵌入维度"):
            _ = embedder.dimension


class TestSqliteVectorStore:
    def test_upsert_and_count(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        try:
            index = _index(store)
            written = index.upsert(
                "docs",
                [VectorEntry("a", "文本一"), VectorEntry("b", "文本二")],
                [(1.0, 0.0), (0.0, 1.0)],
            )
            assert written == 2
            assert index.count("docs") == 2
            assert index.count() == 2
        finally:
            store.stop()

    def test_upsert_replaces_on_conflict(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        try:
            index = _index(store)
            index.upsert("docs", [VectorEntry("a", "old")], [(1.0, 0.0)])
            index.upsert("docs", [VectorEntry("a", "new")], [(0.0, 1.0)])
            assert index.count("docs") == 1
            hit = index.get("docs", "a")
            assert hit is not None
            assert hit.text == "new"
        finally:
            store.stop()

    def test_search_ranks_by_similarity(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        try:
            index = _index(store)
            index.upsert(
                "docs",
                [VectorEntry("far", "远"), VectorEntry("near", "近")],
                [(0.0, 1.0), (1.0, 0.0)],
            )
            hits = index.search("docs", (1.0, 0.0), top_k=2)
            assert [hit.record_id for hit in hits] == ["near", "far"]
            assert hits[0].score == pytest.approx(1.0)
        finally:
            store.stop()

    def test_search_respects_top_k_and_min_score(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        try:
            index = _index(store)
            index.upsert(
                "docs",
                [VectorEntry("a", "A"), VectorEntry("b", "B"), VectorEntry("c", "C")],
                [(1.0, 0.0), (0.7, 0.7), (0.0, 1.0)],
            )
            assert len(index.search("docs", (1.0, 0.0), top_k=2)) == 2
            assert [hit.record_id for hit in index.search("docs", (1.0, 0.0), min_score=0.9)] == [
                "a"
            ]
        finally:
            store.stop()

    def test_search_on_unknown_collection_is_empty(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        try:
            assert _index(store).search("nothing", (1.0, 0.0)) == []
        finally:
            store.stop()

    def test_dimension_change_is_refused_with_an_actionable_message(self, tmp_path: Path) -> None:
        """A width mismatch means the embedder changed under an existing index.
        Ranking those against each other would be nonsense, so the query is
        refused and the message says what to do."""
        store = _store(tmp_path)
        try:
            index = _index(store)
            index.upsert("docs", [VectorEntry("a", "A")], [(1.0, 0.0, 0.0)])
            with pytest.raises(VectorStoreError, match="需要重建该集合"):
                index.search("docs", (1.0, 0.0))
        finally:
            store.stop()

    def test_upsert_length_mismatch(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        try:
            with pytest.raises(VectorStoreError, match="数量不一致"):
                _index(store).upsert("docs", [VectorEntry("a", "A")], [])
        finally:
            store.stop()

    def test_upsert_rejects_mixed_widths(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        try:
            with pytest.raises(VectorStoreError, match="维度不一致"):
                _index(store).upsert(
                    "docs",
                    [VectorEntry("a", "A"), VectorEntry("b", "B")],
                    [(1.0,), (1.0, 0.0)],
                )
        finally:
            store.stop()

    def test_delete_and_drop(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        try:
            index = _index(store)
            index.upsert(
                "docs",
                [VectorEntry("a", "A"), VectorEntry("b", "B")],
                [(1.0, 0.0), (0.0, 1.0)],
            )
            assert index.delete("docs", ["a"]) == 1
            assert index.count("docs") == 1
            assert index.delete_collection("docs") == 1
            assert index.count() == 0
        finally:
            store.stop()

    def test_metadata_roundtrip(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        try:
            index = _index(store)
            index.upsert(
                "docs",
                [VectorEntry("a", "A", {"source": "手册.pdf", "page": 3})],
                [(1.0, 0.0)],
            )
            hits = index.search("docs", (1.0, 0.0))
            assert hits[0].metadata == {"source": "手册.pdf", "page": 3}
        finally:
            store.stop()

    def test_malformed_metadata_is_tolerated(self, tmp_path: Path) -> None:
        """A hand-edited database should degrade to "no metadata", not crash a
        retrieval that is otherwise fine."""
        store = _store(tmp_path)
        try:
            index = _index(store)
            index.upsert("docs", [VectorEntry("a", "A")], [(1.0, 0.0)])
            with store.connection() as connection:
                connection.execute("UPDATE vector_records SET metadata = 'not json'")
            assert index.search("docs", (1.0, 0.0))[0].metadata == {}
        finally:
            store.stop()

    def test_collections_summary(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        try:
            index = _index(store)
            index.upsert("alpha", [VectorEntry("a", "A")], [(1.0, 0.0)], engine="stub")
            index.upsert("beta", [VectorEntry("b", "B")], [(1.0, 0.0)], engine="stub")
            summary = {row["collection"]: row for row in index.collections()}
            assert set(summary) == {"alpha", "beta"}
            assert summary["alpha"]["records"] == 1
            assert summary["alpha"]["engine"] == "stub"
            assert summary["alpha"]["dimension"] == 2
        finally:
            store.stop()

    def test_dimension_of_empty_collection_is_zero(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        try:
            assert _index(store).dimension_of("nothing") == 0
        finally:
            store.stop()

    def test_get_returns_none_for_missing(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        try:
            assert _index(store).get("docs", "nope") is None
        finally:
            store.stop()


def _index(store: SqliteStore) -> SqliteVectorStore:
    from jarvis.vector.store import MIGRATIONS

    store.migrate("vector", MIGRATIONS)
    return SqliteVectorStore(store)


class TestVectorService:
    def _service(self, tmp_path: Path, embedder: object | None = None) -> VectorService:
        store = _store(tmp_path)
        section = VectorSection.from_mapping(
            {
                "embedding": {
                    "engine": "hashing",
                    "dimension": 64,
                    "base_url": "https://x.test/v1",
                    "model": "m",
                    "api_key_env": "TEST_KEY",
                    "timeout_seconds": 5.0,
                    "batch_size": 4,
                }
            }
        )
        return VectorService(store, lambda: section, embedder=embedder)  # type: ignore[arg-type]

    def test_start_migrates_and_reports_stats(self, tmp_path: Path) -> None:
        service = self._service(tmp_path)
        service.start()
        try:
            stats = service.stats()
            assert stats["running"] is True
            assert stats["engine"] == "hashing"
            assert stats["dimension"] == 64
            assert stats["records"] == 0
        finally:
            service.stop()

    def test_index_and_search(self, tmp_path: Path) -> None:
        service = self._service(tmp_path, _StubEmbedder())
        service.start()
        try:
            assert (
                service.index("memo", [VectorEntry("1", "aa bb"), VectorEntry("2", "cc dd")]) == 2
            )
            hits = service.search("memo", "aa", top_k=1)
            assert hits[0].record_id == "1"
        finally:
            service.stop()

    def test_search_on_empty_collection_skips_the_embedder(self, tmp_path: Path) -> None:
        """Asking a cloud endpoint to embed a query that can match nothing is a
        wasted round trip; the caller gets an empty list either way."""

        class _Exploding:
            name = "boom"

            @property
            def dimension(self) -> int:
                return 4

            def embed(self, texts: Sequence[str]) -> list[Vector]:
                raise AssertionError("embedder must not be called")

        service = self._service(tmp_path, _Exploding())
        service.start()
        try:
            assert service.search("empty", "anything") == []
        finally:
            service.stop()

    def test_blank_query_is_ignored(self, tmp_path: Path) -> None:
        service = self._service(tmp_path, _StubEmbedder())
        service.start()
        try:
            assert service.search("memo", "   ") == []
        finally:
            service.stop()

    def test_operations_before_start_raise(self, tmp_path: Path) -> None:
        service = self._service(tmp_path)
        with pytest.raises(VectorStoreError, match="未启动"):
            service.embed(["x"])

    def test_forget_and_drop(self, tmp_path: Path) -> None:
        service = self._service(tmp_path, _StubEmbedder())
        service.start()
        try:
            service.index("memo", [VectorEntry("1", "aa"), VectorEntry("2", "bb")])
            assert service.forget("memo", ["1"]) == 1
            assert service.drop("memo") == 1
            assert service.count("memo") == 0
        finally:
            service.stop()

    def test_empty_batch_short_circuits(self, tmp_path: Path) -> None:
        service = self._service(tmp_path, _StubEmbedder())
        service.start()
        try:
            assert service.embed([]) == []
            assert service.index("memo", []) == 0
        finally:
            service.stop()

    def test_http_engine_without_key_warns_but_still_starts(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Booting offline is a requirement; the warning names the variable so
        the operator knows what to set rather than seeing a stack trace."""
        store = _store(tmp_path)
        section = VectorSection.from_mapping(
            {
                "embedding": {
                    "engine": "http",
                    "dimension": 0,
                    "base_url": "https://x.test/v1",
                    "model": "m",
                    "api_key_env": "JARVIS_TEST_ABSENT_KEY",
                    "timeout_seconds": 5.0,
                    "batch_size": 4,
                }
            }
        )
        service = VectorService(store, lambda: section)
        with caplog.at_level("WARNING"):
            service.start()
        try:
            assert service.running
            assert "JARVIS_TEST_ABSENT_KEY" in caplog.text
            assert service.stats()["dimension"] == 0
        finally:
            service.stop()
