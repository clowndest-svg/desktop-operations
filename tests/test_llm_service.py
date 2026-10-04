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
