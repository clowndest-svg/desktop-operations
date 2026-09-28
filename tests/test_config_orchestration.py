"""Validation of the ``orchestration`` configuration section."""

from __future__ import annotations

import pytest

from jarvis.config.loader import load_config
from jarvis.config.paths import AppPaths
from jarvis.config.schema import OrchestrationSection
from jarvis.core.exceptions import ConfigurationError


def test_default_orchestration_section() -> None:
    cfg = load_config(AppPaths.resolve())
    assert cfg.orchestration.enabled is False
    assert cfg.orchestration.barge_in is True
    assert cfg.orchestration.default_agent == "chat"


def test_section_parses() -> None:
    s = OrchestrationSection.from_mapping(
        {"enabled": True, "barge_in": False, "default_agent": "tools"}
    )
    assert s.enabled is True
    assert s.barge_in is False
    assert s.default_agent == "tools"


def test_section_rejects_unknown_key() -> None:
    with pytest.raises(ConfigurationError):
        OrchestrationSection.from_mapping(
            {"enabled": True, "barge_in": True, "default_agent": "chat", "bogus": 1}
        )


def test_section_requires_bool_enabled() -> None:
    with pytest.raises(ConfigurationError):
        OrchestrationSection.from_mapping(
            {"enabled": "yes", "barge_in": True, "default_agent": "chat"}
        )
