"""Tests for the VAD configuration section (``vad.*``)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import cast

import pytest

from jarvis.config.loader import load_defaults
from jarvis.config.schema import VadSection
from jarvis.core.exceptions import ConfigurationError


def base() -> dict[str, object]:
    return dict(cast(Mapping[str, object], load_defaults()["vad"]))


def test_defaults_validate() -> None:
    section = VadSection.from_mapping(base())
    assert section.enabled is False
    assert section.engine == "silero"
    assert section.threshold == pytest.approx(0.5)
    assert section.min_speech_ms == 250
    assert section.max_silence_ms == 500
    assert section.speech_pad_ms == 100
    assert section.max_speech_ms == 0


def test_unknown_engine_rejected() -> None:
    raw = base()
    raw["engine"] = "snowboy"
    with pytest.raises(ConfigurationError, match=r"vad\.engine"):
        VadSection.from_mapping(raw)


def test_unknown_key_rejected() -> None:
    raw = base()
    raw["threshod"] = 0.5  # typo on purpose
    with pytest.raises(ConfigurationError, match=r"vad\.threshod"):
        VadSection.from_mapping(raw)


def test_threshold_above_one_rejected() -> None:
    raw = base()
    raw["threshold"] = 1.5
    with pytest.raises(ConfigurationError, match=r"vad\.threshold"):
        VadSection.from_mapping(raw)


def test_threshold_below_zero_rejected() -> None:
    raw = base()
    raw["threshold"] = -0.1
    with pytest.raises(ConfigurationError, match=r"vad\.threshold"):
        VadSection.from_mapping(raw)


def test_min_speech_negative_rejected() -> None:
    raw = base()
    raw["min_speech_ms"] = -1
    with pytest.raises(ConfigurationError, match=r"vad\.min_speech_ms"):
        VadSection.from_mapping(raw)


def test_max_silence_zero_rejected() -> None:
    raw = base()
    raw["max_silence_ms"] = 0
    with pytest.raises(ConfigurationError, match=r"vad\.max_silence_ms"):
        VadSection.from_mapping(raw)


def test_speech_pad_negative_rejected() -> None:
    raw = base()
    raw["speech_pad_ms"] = -1
    with pytest.raises(ConfigurationError, match=r"vad\.speech_pad_ms"):
        VadSection.from_mapping(raw)


def test_max_speech_negative_rejected() -> None:
    raw = base()
    raw["max_speech_ms"] = -1
    with pytest.raises(ConfigurationError, match=r"vad\.max_speech_ms"):
        VadSection.from_mapping(raw)
