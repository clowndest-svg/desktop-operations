"""Tests for the asr configuration section."""

from __future__ import annotations

from collections.abc import Mapping
from typing import cast

import pytest

from jarvis.config.loader import load_defaults
from jarvis.config.schema import AppConfig, AsrSection
from jarvis.core.exceptions import ConfigurationError


def base() -> dict[str, object]:
    return dict(cast(Mapping[str, object], load_defaults()["asr"]))


def test_defaults_validate() -> None:
    section = AsrSection.from_mapping(base())
    assert section.enabled is False
    assert section.engine == "sensevoice"
    assert section.model == "iic/SenseVoiceSmall"
    assert section.language == "auto"
    assert section.temperature == pytest.approx(0.0)
    assert section.beam_size == 5
    assert section.device == "cpu"


def test_unknown_engine_rejected() -> None:
    raw = base()
    raw["engine"] = "whisper-x"
    with pytest.raises(ConfigurationError, match=r"asr\.engine"):
        AsrSection.from_mapping(raw)


def test_unknown_key_rejected() -> None:
    raw = base()
    raw["languge"] = "zh"  # typo on purpose
    with pytest.raises(ConfigurationError, match=r"asr\.languge"):
        AsrSection.from_mapping(raw)


def test_temperature_above_one_rejected() -> None:
    raw = base()
    raw["temperature"] = 1.5
    with pytest.raises(ConfigurationError, match=r"asr\.temperature"):
        AsrSection.from_mapping(raw)


def test_negative_temperature_rejected() -> None:
    raw = base()
    raw["temperature"] = -0.1
    with pytest.raises(ConfigurationError, match=r"asr\.temperature"):
        AsrSection.from_mapping(raw)


def test_beam_size_below_one_rejected() -> None:
    raw = base()
    raw["beam_size"] = 0
    with pytest.raises(ConfigurationError, match=r"asr\.beam_size"):
        AsrSection.from_mapping(raw)


def test_empty_model_rejected() -> None:
    raw = base()
    raw["model"] = "   "
    with pytest.raises(ConfigurationError, match=r"asr\.model"):
        AsrSection.from_mapping(raw)


def test_empty_device_rejected() -> None:
    raw = base()
    raw["device"] = ""
    with pytest.raises(ConfigurationError, match=r"asr\.device"):
        AsrSection.from_mapping(raw)


def test_default_config_asr_is_disabled() -> None:
    config = AppConfig.from_mapping(load_defaults())
    assert isinstance(config.asr, AsrSection)
    assert config.asr.enabled is False
    assert config.asr.engine == "sensevoice"
