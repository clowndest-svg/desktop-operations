"""Tests for the wakeword configuration section."""

from __future__ import annotations

from collections.abc import Mapping
from typing import cast

import pytest

from jarvis.config.loader import load_defaults
from jarvis.config.schema import WakeWordSection
from jarvis.core.exceptions import ConfigurationError


def base() -> dict[str, object]:
    return dict(cast(Mapping[str, object], load_defaults()["wakeword"]))


def test_defaults_validate() -> None:
    section = WakeWordSection.from_mapping(base())
    assert section.enabled is False
    assert section.engine == "asr"
    assert section.keywords == ("你好小夜", "你好小叶", "你好晓叶", "你好小业")
    assert section.threshold == pytest.approx(0.5)
    assert section.cooldown_seconds == pytest.approx(2.0)
    assert section.porcupine.access_key_env == "PICOVOICE_ACCESS_KEY"
    assert section.porcupine.sensitivity == pytest.approx(0.5)


def test_the_default_engine_understands_the_default_keywords() -> None:
    """Engine and keywords ship as a pair; a mismatch wakes nothing at all.

    ``hey_jarvis`` is an OpenWakeWord *model name*; to the ``asr`` engine it is
    just a string to find in a Chinese transcript. Swapping one half of the pair
    without the other produces an assistant that never answers to its name, with
    nothing in the logs to say why.
    """
    section = WakeWordSection.from_mapping(base())
    ascii_only = all(word.isascii() for word in section.keywords)

    if section.engine == "asr":
        assert not ascii_only, "the asr engine wakes on transcribed phrases"
    else:
        assert ascii_only, f"{section.engine} needs model names, not free text"


def test_unknown_engine_rejected() -> None:
    raw = base()
    raw["engine"] = "snowboy"
    with pytest.raises(ConfigurationError, match=r"wakeword\.engine"):
        WakeWordSection.from_mapping(raw)


def test_unknown_key_rejected() -> None:
    raw = base()
    raw["sensitivty"] = 0.5  # typo on purpose
    with pytest.raises(ConfigurationError, match=r"wakeword\.sensitivty"):
        WakeWordSection.from_mapping(raw)


def test_empty_keywords_rejected() -> None:
    raw = base()
    raw["keywords"] = []
    with pytest.raises(ConfigurationError, match=r"wakeword\.keywords"):
        WakeWordSection.from_mapping(raw)


def test_non_string_keyword_rejected_with_index() -> None:
    raw = base()
    raw["keywords"] = ["ok", 42]
    with pytest.raises(ConfigurationError, match=r"wakeword\.keywords\[1\]"):
        WakeWordSection.from_mapping(raw)


def test_threshold_above_one_rejected() -> None:
    raw = base()
    raw["threshold"] = 1.5
    with pytest.raises(ConfigurationError, match=r"wakeword\.threshold"):
        WakeWordSection.from_mapping(raw)


def test_negative_cooldown_rejected() -> None:
    raw = base()
    raw["cooldown_seconds"] = -1
    with pytest.raises(ConfigurationError, match=r"wakeword\.cooldown_seconds"):
        WakeWordSection.from_mapping(raw)


def test_porcupine_sensitivity_out_of_range_rejected() -> None:
    raw = base()
    porcupine = dict(cast(Mapping[str, object], raw["porcupine"]))
    porcupine["sensitivity"] = 2.0
    raw["porcupine"] = porcupine
    with pytest.raises(ConfigurationError, match=r"wakeword\.porcupine\.sensitivity"):
        WakeWordSection.from_mapping(raw)


def test_missing_porcupine_section_rejected() -> None:
    raw = base()
    del raw["porcupine"]
    with pytest.raises(ConfigurationError, match="porcupine"):
        WakeWordSection.from_mapping(raw)
