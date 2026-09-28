"""Schema validation tests for the ``llm`` configuration section."""

from __future__ import annotations

from collections.abc import Mapping

import pytest

from jarvis.config.loader import load_defaults
from jarvis.config.schema import AppConfig, LlmSection
from jarvis.core.exceptions import ConfigurationError


def _section(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "default_provider": "test",
        "timeout_seconds": 30,
        "max_retries": 1,
        "retry_backoff_seconds": 0.5,
        "providers": {
            "test": {
                "base_url": "https://api.test.example/v1/",
                "model": "test-model",
                "api_key_env": "TEST_API_KEY",
                "cost_input_per_1m": 0.0,
                "cost_output_per_1m": 0.0,
            }
        },
    }
    base.update(overrides)
    return base


class TestLlmSection:
    def test_defaults_yaml_ships_a_valid_llm_section(self) -> None:
        config = AppConfig.from_mapping(load_defaults())
        assert config.llm.default_provider == "deepseek"
        assert set(config.llm.providers) >= {"deepseek", "openai", "kimi", "qwen"}
        assert config.llm.default.model == "deepseek-chat"

    def test_base_url_trailing_slash_is_stripped(self) -> None:
        section = LlmSection.from_mapping(_section())
        assert section.providers["test"].base_url == "https://api.test.example/v1"

    def test_timeout_accepts_int_and_float(self) -> None:
        assert LlmSection.from_mapping(_section(timeout_seconds=30)).timeout_seconds == 30.0
        assert LlmSection.from_mapping(_section(timeout_seconds=12.5)).timeout_seconds == 12.5

    def test_unknown_default_provider_is_rejected(self) -> None:
        with pytest.raises(ConfigurationError, match=r"llm.default_provider"):
            LlmSection.from_mapping(_section(default_provider="nope"))

    def test_empty_providers_is_rejected(self) -> None:
        with pytest.raises(ConfigurationError, match=r"llm.providers"):
            LlmSection.from_mapping(_section(providers={}))

    def test_provider_unknown_key_is_pinpointed(self) -> None:
        data = _section()
        providers = data["providers"]
        assert isinstance(providers, dict)
        provider = providers["test"]
        assert isinstance(provider, dict)
        provider["api_key"] = "sk-plaintext"  # storing a key inline is a config error
        with pytest.raises(ConfigurationError, match=r"llm.providers.test.api_key"):
            LlmSection.from_mapping(data)

    def test_provider_must_be_a_mapping(self) -> None:
        with pytest.raises(ConfigurationError, match=r"llm.providers.bad"):
            LlmSection.from_mapping(_section(providers={"bad": "not-a-mapping"}))

    def test_negative_cost_is_rejected(self) -> None:
        data = _section()
        providers = data["providers"]
        assert isinstance(providers, Mapping)
        provider = providers["test"]
        assert isinstance(provider, dict)
        provider["cost_input_per_1m"] = -1
        with pytest.raises(ConfigurationError, match="cost_input_per_1m"):
            LlmSection.from_mapping(data)

    def test_timeout_below_minimum_is_rejected(self) -> None:
        with pytest.raises(ConfigurationError, match=r"llm.timeout_seconds"):
            LlmSection.from_mapping(_section(timeout_seconds=0))

    def test_boolean_is_not_a_number(self) -> None:
        with pytest.raises(ConfigurationError, match=r"llm.retry_backoff_seconds"):
            LlmSection.from_mapping(_section(retry_backoff_seconds=True))
