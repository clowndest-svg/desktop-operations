"""Unit tests for the LlmService lifecycle component."""

from __future__ import annotations

import logging

import pytest

from jarvis.config.schema import LlmSection
from jarvis.core.exceptions import ConfigurationError
from jarvis.llm.openai_compat import OpenAiCompatClient
from jarvis.llm.service import LlmService


def make_section() -> LlmSection:
    return LlmSection.from_mapping(
        {
            "default_provider": "alpha",
            "timeout_seconds": 15,
            "max_retries": 1,
            "retry_backoff_seconds": 0.1,
            "providers": {
                "alpha": {
                    "base_url": "https://alpha.example/v1",
                    "models": ["alpha-1", "alpha-small"],
                    "default_model": "alpha-1",
                    "api_key_env": "ALPHA_KEY",
                    "cost_input_per_1m": 0.0,
                    "cost_output_per_1m": 0.0,
                },
                "beta": {
                    "base_url": "https://beta.example/v1",
                    "models": ["beta-9"],
                    "api_key_env": "BETA_KEY",
                    "cost_input_per_1m": 0.0,
                    "cost_output_per_1m": 0.0,
                },
            },
        }
    )


class TestLlmService:
    def test_start_builds_default_client_lazily(self) -> None:
        service = LlmService(make_section, environ={"ALPHA_KEY": "k"})
        service.start()
        client = service.client
        assert isinstance(client, OpenAiCompatClient)
        assert client.provider_name == "alpha"
        assert client.model == "alpha-1"

    def test_clients_are_cached_per_provider(self) -> None:
        service = LlmService(make_section, environ={"ALPHA_KEY": "k", "BETA_KEY": "k"})
        service.start()
        assert service.client_for("alpha") is service.client_for("alpha")
        assert service.client_for("alpha") is not service.client_for("beta")
        assert service.client_for("beta").model == "beta-9"

    def test_unknown_provider_is_a_configuration_error(self) -> None:
        service = LlmService(make_section, environ={})
        service.start()
        with pytest.raises(ConfigurationError, match="unknown LLM provider"):
            service.client_for("gamma")

    def test_access_before_start_fails(self) -> None:
        service = LlmService(make_section, environ={})
        with pytest.raises(ConfigurationError, match="not started"):
            _ = service.client

    def test_missing_keys_warn_at_start_but_do_not_block(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        service = LlmService(make_section, environ={"ALPHA_KEY": "k"})
        with caplog.at_level(logging.WARNING, logger="jarvis.llm.service"):
            service.start()
        warned = [r.message for r in caplog.records if "no API key yet" in r.message]
        assert len(warned) == 1  # only beta lacks a key

    def test_stop_clears_state_and_start_is_idempotent(self) -> None:
        service = LlmService(make_section, environ={"ALPHA_KEY": "k"})
        service.start()
        service.start()  # no-op
        first = service.client
        service.stop()
        with pytest.raises(ConfigurationError):
            _ = service.client
        service.start()
        assert service.client is not first  # fresh cache after restart

    def test_name_is_stable(self) -> None:
        assert LlmService(make_section).name == "llm"


def _local_section() -> LlmSection:
    """一份只有自带模型那一行的配置，且它写的模型名是 defaults.yaml 那个旧的。"""
    return LlmSection.from_mapping(
        {
            "default_provider": "local",
            "timeout_seconds": 30,
            "max_retries": 0,
            "retry_backoff_seconds": 0.1,
            "providers": {
                "local": {
                    "base_url": "http://127.0.0.1:28181/v1",
                    "models": ["qwen2.5-7b"],
                    "default_model": "qwen2.5-7b",
                    "api_key_env": "LOCAL_NOTHING",
                    "key_optional": True,
                }
            },
        }
    )


def test_the_bundled_row_asks_for_the_model_that_is_actually_loaded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """那一行不再自带模型名：发出去的就是「选哪个离线模型」选中那张卡的别名。

    实拍：设置里那一行写着 qwen2.5-7b，而实际加载的是 Qwen3-4B —— 报错尾巴
    (provider='local', model='qwen2.5-7b') 就是两处各说一套的证据。
    """
    from jarvis.llm import local_server

    monkeypatch.setattr(
        local_server, "selected_card", lambda: local_server.MODEL_CARDS["Qwen3-4B.Q4_K_M.gguf"]
    )
    monkeypatch.setattr(local_server.shared_server(), "ensure_running", lambda **k: (True, ""))
    monkeypatch.setattr(local_server, "endpoint", lambda: "http://127.0.0.1:28181/v1")

    service = LlmService(_local_section, environ={})
    service.start()

    client = service.client_for("local", "qwen2.5-7b")

    assert client.model == "qwen3-4b", "发出去的名字必须是在内存里那个"
    assert isinstance(client, OpenAiCompatClient)
    assert client._settings.bundled is True, "自带那一行要能被看门狗认出来（请求在飞不许关）"
