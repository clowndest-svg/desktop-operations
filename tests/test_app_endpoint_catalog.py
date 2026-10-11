"""Tests for :mod:`jarvis.app.endpoint_catalog`: the add-block's model listing."""

from __future__ import annotations

import urllib.error
import urllib.request
from typing import Any

import pytest

import jarvis.app.endpoint_catalog as catalog
from jarvis.core.constants import USER_AGENT


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
        # The listing call is the one the settings panel makes before anything is
        # saved, so it must not arrive wearing the stdlib default either.
        assert calls["headers"] == {"Accept": "application/json", "User-Agent": USER_AGENT}
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


class TestListingFromALocalEndpointSkipsTheProxy:
    """「看看它有哪些模型」打的是本机端口，那条路也不能绕道系统代理。

    这一条不替换 ``_get_json``：要的就是它真的走到开连接那一步，接线断了才看得见。
    """

    def test_the_models_call_is_opened_without_proxies(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: dict[str, object] = {}

        class _Response:
            def __enter__(self) -> _Response:
                return self

            def __exit__(self, *exc: object) -> None:
                return None

            def read(self) -> bytes:
                return b'{"data": [{"id": "qwen2.5:7b"}]}'

        class _Opener:
            def open(self, request, timeout=None):  # type: ignore[no-untyped-def]
                seen["url"] = request.full_url
                return _Response()

        def fake_build_opener(*handlers: object) -> Any:
            seen["handlers"] = handlers
            return _Opener()

        def never(*args: object, **kwargs: object) -> None:
            raise AssertionError("本机端点的清单请求不该走 urlopen（那才会用系统代理）")

        monkeypatch.setattr("urllib.request.build_opener", fake_build_opener)
        monkeypatch.setattr("urllib.request.urlopen", never)
        monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:7897")

        ids, why = catalog.list_endpoint_models("http://127.0.0.1:11434/v1", timeout_seconds=9.0)

        assert ids == ["qwen2.5:7b"] and why == ""
        handlers = seen.get("handlers")
        assert isinstance(handlers, tuple) and handlers
        assert isinstance(handlers[0], urllib.request.ProxyHandler)
        assert vars(handlers[0])["proxies"] == {}
