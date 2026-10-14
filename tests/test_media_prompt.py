"""Translation must retain the full sentence, not guess from known keywords."""

from __future__ import annotations

import io
import json
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest

from jarvis.app.media_prompt import PromptTranslator, TranslationError


@pytest.mark.parametrize(
    ("source", "language", "english"),
    [
        (
            "两只红鸟在蓝猫左边，没有帽子",
            "zh-CN",
            "Two red birds to the left of a blue cat, no hats",
        ),
        ("Un chat orange court dans la neige", "fr", "An orange cat runs in the snow"),
        ("Dos gatos negros sin sombrero", "es", "Two black cats without hats"),
        ("赤い鳥が飛んでいる", "ja", "A red bird is flying"),
        ("Синяя птица летит", "ru", "A blue bird is flying"),
        ("طائر أزرق يطير", "ar", "A blue bird is flying"),
    ],
)
def test_automatic_detection_also_translates_ascii_languages(
    source: str,
    language: str,
    english: str,
) -> None:
    requests = []

    def opener(request: Any, timeout: float) -> io.BytesIO:
        requests.append(request)
        assert timeout <= 15
        query = parse_qs(urlparse(request.full_url).query)
        assert query["sl"] == ["auto"] and query["tl"] == ["en"]
        assert query["q"] == [source]
        return io.BytesIO(json.dumps([[[english, source]], None, language]).encode())

    translator = PromptTranslator(opener)
    result = translator.translate(source)
    assert result.original == source and result.english == english
    assert translator.translate(source) == result
    assert len(requests) == 1


def test_english_weights_and_punctuation_remain_exact() -> None:
    source = "(blue cat:1.3), two birds, no hats"

    def opener(request: Any, timeout: float) -> io.BytesIO:
        return io.BytesIO(json.dumps([[["rewritten", source]], None, "en"]).encode())

    assert PromptTranslator(opener).translate(source).english == source


@pytest.mark.parametrize(
    "payload",
    [
        {},
        [],
        [[["cat", "only part"]], None, "zh-CN"],
        [[["cat 猫", "猫"]], None, "zh-CN"],
        [[["", "猫"]], None, "zh-CN"],
    ],
)
def test_bad_or_incomplete_translations_stop_generation(payload: object) -> None:
    def opener(request: Any, timeout: float) -> io.BytesIO:
        return io.BytesIO(json.dumps(payload).encode())

    with pytest.raises(TranslationError):
        PromptTranslator(opener).translate("猫")


def test_network_failure_has_no_prompt_or_html_in_the_error() -> None:
    def opener(request: Any, timeout: float) -> io.BytesIO:
        raise OSError("private description and proxy credentials")

    with pytest.raises(TranslationError, match="尚未提交") as error:
        PromptTranslator(opener).translate("private description")
    assert "private description" not in str(error.value)


def test_multiple_segments_preserve_all_clauses() -> None:
    payload = [[["A blue cat. ", "蓝猫。"], ["No hat.", "没有帽子。"]], None, "zh-CN"]
    result = PromptTranslator._parse("蓝猫。没有帽子。", payload)
    assert result.english == "A blue cat. No hat."
