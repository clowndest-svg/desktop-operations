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
                "models": ["test-model", "test-model-large"],
                "default_model": "test-model",
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

    def test_key_optional_defaults_to_off_for_every_older_file(self) -> None:
        """Rows written before this flag existed must keep demanding a key."""
        provider = LlmSection.from_mapping(_section()).providers["test"]
        assert provider.key_optional is False

    def test_a_local_endpoint_can_declare_it_needs_no_key(self) -> None:
        data = _section()
        providers = data["providers"]
        assert isinstance(providers, dict)
        provider = providers["test"]
        assert isinstance(provider, dict)
        provider["base_url"] = "http://localhost:11434/v1"
        provider["key_optional"] = True

        parsed = LlmSection.from_mapping(data).providers["test"]

        assert parsed.key_optional is True
        # Plain http on a loopback port is a normal local server, not a typo to fix.
        assert parsed.base_url == "http://localhost:11434/v1"

    def test_key_optional_must_actually_be_a_boolean(self) -> None:
        """ "false" as a string would be truthy, which is the opposite of what it says."""
        data = _section()
        providers = data["providers"]
        assert isinstance(providers, dict)
        provider = providers["test"]
        assert isinstance(provider, dict)
        provider["key_optional"] = "false"
        with pytest.raises(ConfigurationError, match=r"llm.providers.test.key_optional"):
            LlmSection.from_mapping(data)

    def test_the_old_single_model_field_says_how_to_rename_it(self) -> None:
        """One field became a list, so an old file is a hard failure -- and the
        message is the migration. Accepting both spellings would leave every reader
        guessing which one wins, which is how the picker and the panel end up
        showing different sets of models."""
        data = _section()
        providers = data["providers"]
        assert isinstance(providers, dict)
        provider = providers["test"]
        assert isinstance(provider, dict)
        del provider["models"]
        del provider["default_model"]
        provider["model"] = "test-model"

        with pytest.raises(ConfigurationError) as caught:
            LlmSection.from_mapping(data)

        message = str(caught.value)
        assert "llm.providers.test.model" in message
        assert "models" in message and "default_model" in message

    def test_an_empty_model_list_is_rejected(self) -> None:
        """A provider with no models is one the picker cannot show and the client
        factory cannot build, so it is a config error rather than an empty dropdown."""
        data = _section()
        providers = data["providers"]
        assert isinstance(providers, dict)
        provider = providers["test"]
        assert isinstance(provider, dict)
        provider["models"] = []

        with pytest.raises(ConfigurationError, match=r"llm.providers.test.models"):
            LlmSection.from_mapping(data)

    def test_a_default_model_that_is_not_listed_is_rejected(self) -> None:
        data = _section()
        providers = data["providers"]
        assert isinstance(providers, dict)
        provider = providers["test"]
        assert isinstance(provider, dict)
        provider["default_model"] = "not-in-the-list"

        with pytest.raises(ConfigurationError, match=r"llm.providers.test.default_model"):
            LlmSection.from_mapping(data)

    def test_a_duplicated_model_id_is_rejected(self) -> None:
        """Two rows with one id make the second unreachable: the picker keys on the
        id, so the duplicate would simply never be selectable."""
        data = _section()
        providers = data["providers"]
        assert isinstance(providers, dict)
        provider = providers["test"]
        assert isinstance(provider, dict)
        provider["models"] = ["test-model", "test-model"]

        with pytest.raises(ConfigurationError, match=r"重复|duplicate"):
            LlmSection.from_mapping(data)

    def test_a_model_entry_may_be_a_bare_string_or_a_mapping_with_a_label(self) -> None:
        """The display name is optional: most of the time the wire name is what the
        operator wants to see, and forcing a mapping for every row would be noise."""
        data = _section()
        providers = data["providers"]
        assert isinstance(providers, dict)
        provider = providers["test"]
        assert isinstance(provider, dict)
        provider["models"] = ["bare-name", {"id": "wire-name", "label": "显示名"}]
        provider["default_model"] = "wire-name"

        section = LlmSection.from_mapping(data)

        specs = section.providers["test"].models
        assert [(spec.id, spec.display) for spec in specs] == [
            ("bare-name", "bare-name"),
            ("wire-name", "显示名"),
        ]

    def test_an_unknown_model_id_falls_back_to_the_default_rather_than_raising(self) -> None:
        """A stale stored preference is normal; blanking the chat header is not."""
        data = _section()
        section = LlmSection.from_mapping(data)

        spec = section.providers["test"].model_spec("gone-in-a-refactor")

        assert spec.id == "test-model"

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
