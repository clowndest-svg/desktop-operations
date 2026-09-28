"""Tests for the tts configuration section."""

from __future__ import annotations

from collections.abc import Mapping
from typing import cast

import pytest

from jarvis.config.loader import load_defaults
from jarvis.config.schema import AppConfig, TtsSection
from jarvis.core.exceptions import ConfigurationError


def base() -> dict[str, object]:
    return dict(cast(Mapping[str, object], load_defaults()["tts"]))


def test_defaults_validate() -> None:
    section = TtsSection.from_mapping(base())
    assert section.enabled is False
    assert section.engine == "edge_tts"
    assert section.voice == "zh-CN-XiaoxiaoNeural"
    assert section.speed == pytest.approx(1.0)
    assert section.volume == pytest.approx(1.0)
    assert section.device == "cpu"
    assert section.model == "iic/CosyVoice2-0.5B"


def test_unknown_engine_rejected() -> None:
    raw = base()
    raw["engine"] = "gpt-sovits"
    with pytest.raises(ConfigurationError, match=r"tts\.engine"):
        TtsSection.from_mapping(raw)


def test_unknown_key_rejected() -> None:
    raw = base()
    raw["voce"] = "中文女"  # typo on purpose
    with pytest.raises(ConfigurationError, match=r"tts\.voce"):
        TtsSection.from_mapping(raw)


def test_speed_below_zero_rejected() -> None:
    raw = base()
    raw["speed"] = -0.1
    with pytest.raises(ConfigurationError, match=r"tts\.speed"):
        TtsSection.from_mapping(raw)


def test_volume_above_one_rejected() -> None:
    raw = base()
    raw["volume"] = 1.5
    with pytest.raises(ConfigurationError, match=r"tts\.volume"):
        TtsSection.from_mapping(raw)


def test_empty_voice_rejected() -> None:
    raw = base()
    raw["voice"] = "   "
    with pytest.raises(ConfigurationError, match=r"tts\.voice"):
        TtsSection.from_mapping(raw)


def test_empty_device_rejected() -> None:
    raw = base()
    raw["device"] = ""
    with pytest.raises(ConfigurationError, match=r"tts\.device"):
        TtsSection.from_mapping(raw)


def test_empty_model_rejected() -> None:
    raw = base()
    raw["model"] = "   "
    with pytest.raises(ConfigurationError, match=r"tts\.model"):
        TtsSection.from_mapping(raw)


def test_default_config_tts_is_disabled() -> None:
    config = AppConfig.from_mapping(load_defaults())
    assert isinstance(config.tts, TtsSection)
    assert config.tts.enabled is False
    assert config.tts.engine == "edge_tts"
