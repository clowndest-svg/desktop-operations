"""Tests for :mod:`jarvis.app.endpoint_catalog`: the add-block's model listing."""

from __future__ import annotations

import urllib.error

import pytest

import jarvis.app.endpoint_catalog as catalog


class TestListingEndpointModels:
    """Every refusal has to say which kind it is; "连不上" and "要钥匙" send the
    operator to different places."""

    @staticmethod
    def _list(
        status: int, payload: object, monkeypatch: pytest.MonkeyPatch
    ) -> tuple[list[str], str, dict[str, object]]:
        calls: dict[str, object] = {}

        def fake(url: str, *, timeout: float, headers: object) -> tuple[int, object]:
            calls.update({"url": url, "timeout": timeout, "headers": headers})
            if isinstance(payload, Exception):
                raise payload
            return status, payload

        monkeypatch.setattr(catalog, "_get_json", fake)
        ids, why = catalog.list_endpoint_models("http://localhost:11434/v1", timeout_seconds=9.0)
        return ids, why, calls

    def test_a_talking_endpoint_hands_back_its_ids(self, monkeypatch: pytest.MonkeyPatch) -> None:
        ids, why, calls = self._list(
            200, {"data": [{"id": "qwen2.5:7b-instruct"}, {"id": "llava:7b"}]}, monkeypatch
        )
        assert ids == ["qwen2.5:7b-instruct", "llava:7b"]
        assert why == ""
        assert calls["url"] == "http://localhost:11434/v1/models"
        assert calls["timeout"] == 9.0

    def test_a_credential_refusal_names_the_key_not_the_network(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ids, why, _ = self._list(401, None, monkeypatch)
        assert ids == []
        assert "钥匙" in why

    def test_a_dead_port_says_unreachable_with_the_address(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ids, why, _ = self._list(0, urllib.error.URLError("refused"), monkeypatch)
        assert ids == []
        assert "连不上" in why and "11434" in why

    def test_a_non_list_body_is_named_not_silently_empty(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ids, why, _ = self._list(200, {"unexpected": True}, monkeypatch)
        assert ids == []
        assert "data" in why

    def test_an_empty_listing_is_said_so(self, monkeypatch: pytest.MonkeyPatch) -> None:
        ids, why, _ = self._list(200, {"data": []}, monkeypatch)
        assert ids == []
        assert "空" in why

    def test_a_key_in_the_environment_travels_as_a_bearer_header(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _, _, calls = self._list(200, {"data": [{"id": "m"}]}, monkeypatch)
        assert calls["headers"] == {"Accept": "application/json"}
        _, _, with_key = self._list_with_key(monkeypatch)
        sent = with_key["headers"]
        assert isinstance(sent, dict)
        assert sent["Authorization"] == "Bearer sk-local-test"

    def _list_with_key(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> tuple[list[str], str, dict[str, object]]:
        calls: dict[str, object] = {}

        def fake(url: str, *, timeout: float, headers: object) -> tuple[int, object]:
            calls.update({"url": url, "timeout": timeout, "headers": headers})
            return 200, {"data": [{"id": "m"}]}

        monkeypatch.setattr(catalog, "_get_json", fake)
        ids, why = catalog.list_endpoint_models(
            "https://gw.example/v1", timeout_seconds=9.0, api_key="sk-local-test"
        )
        return ids, why, calls
