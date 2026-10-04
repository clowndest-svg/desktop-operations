"""The editable wake word: what the panel may store, and what the engine ends up watching.

The distinction all of these tests circle is that the operator's list sits *on top of* the
configured one. Writing it must not lose the shipped homophone spellings by accident, and
clearing it must give them back -- because a machine that answers to nothing looks exactly
like a machine whose microphone is broken.
"""

from __future__ import annotations

import logging
from typing import Any

import pytest

from jarvis.app.preferences import VOICE_WAKE_KEYWORDS, Preferences
from jarvis.app.wake_keywords import (
    MAX_KEYWORD_CHARS,
    MAX_WAKE_KEYWORDS,
    MIN_KEYWORD_CHARS,
    WakeWords,
    parse_wake_keywords,
    resolve,
)

SHIPPED = ("你好小夜", "你好小智", "你好晓夜", "你好小业")


def test_the_shipped_words_are_the_homophone_spellings_not_one_phrase() -> None:
    """Guard rail for the rest of this file: four entries, one name.

    The recogniser transcribes 你好小夜 as 你好小智 often enough that the variants are load
    bearing (``scripts/verify_wake_words.py`` measured it). A test file that assumed a single
    default would quietly bless a change that loses wakes.
    """
    assert len(SHIPPED) == 4
    assert SHIPPED[0] == "你好小夜"


class TestParse:
    def test_one_text_box_splits_on_every_separator_a_person_would_type(self) -> None:
        keywords, refusal = parse_wake_keywords("你好小夜、辛苦你了，小夜 ; 起床/起来")

        assert refusal == ""
        assert keywords == ["你好小夜", "辛苦你了", "小夜", "起床", "起来"]

    def test_a_space_belongs_to_a_name_rather_than_separating_two(self) -> None:
        """「Hey Jarvis」 is one phrase; splitting it would refuse both halves."""
        keywords, refusal = parse_wake_keywords("Hey Jarvis")

        assert refusal == ""
        assert keywords == ["Hey Jarvis"]

    def test_a_list_is_accepted_because_that_is_the_shape_on_disk(self) -> None:
        keywords, refusal = parse_wake_keywords(["你好小夜", " 辛苦你了 "])

        assert refusal == ""
        assert keywords == ["你好小夜", "辛苦你了"]

    def test_two_spellings_of_one_name_count_once(self) -> None:
        keywords, refusal = parse_wake_keywords("Hey Jarvis、hey jarvis")

        assert refusal == ""
        assert keywords == ["Hey Jarvis"]

    def test_too_short_is_refused_because_it_would_wake_on_ordinary_speech(self) -> None:
        keywords, refusal = parse_wake_keywords("夜")

        assert keywords == []
        assert "太短" in refusal and str(MIN_KEYWORD_CHARS) in refusal

    def test_too_long_is_refused_because_a_sentence_is_not_a_name(self) -> None:
        long_word = "小" * (MAX_KEYWORD_CHARS + 1)

        keywords, refusal = parse_wake_keywords(long_word)

        assert keywords == []
        assert "太长" in refusal

    def test_the_boundaries_themselves_are_allowed(self) -> None:
        """The range the panel prints is the range the save enforces, endpoints included."""
        assert parse_wake_keywords("小" * MIN_KEYWORD_CHARS)[1] == ""
        assert parse_wake_keywords("小" * MAX_KEYWORD_CHARS)[1] == ""

    def test_one_word_too_many_is_refused_with_the_number_it_saw(self) -> None:
        words = [f"小夜{i}" for i in range(MAX_WAKE_KEYWORDS + 1)]

        keywords, refusal = parse_wake_keywords(words)

        assert keywords == []
        assert "最多" in refusal and str(MAX_WAKE_KEYWORDS) in refusal

    def test_an_empty_box_is_an_answer_not_a_rejection(self) -> None:
        """Clearing it means "give me back the configured words", so it has to be storable."""
        assert parse_wake_keywords("   ") == ([], "")
        assert parse_wake_keywords([]) == ([], "")
        assert parse_wake_keywords(None) == ([], "")

    def test_a_scalar_that_is_not_a_name_is_refused(self) -> None:
        keywords, refusal = parse_wake_keywords(7)

        assert keywords == []
        assert "字符串或一个列表" in refusal


class TestResolve:
    def test_the_operators_list_wins(self) -> None:
        assert resolve(["辛苦你了"], SHIPPED) == ("辛苦你了",)

    def test_an_empty_or_blank_store_falls_back_to_the_configured_words(self) -> None:
        assert resolve([], SHIPPED) == SHIPPED
        assert resolve(["", "  "], SHIPPED) == SHIPPED
        assert resolve(None, SHIPPED) == SHIPPED

    def test_the_fallback_is_stripped_too(self) -> None:
        assert resolve(None, [" 你好小夜 ", ""]) == ("你好小夜",)


class TestWakeWordsObject:
    def test_without_a_settings_store_it_is_always_the_configured_words(self) -> None:
        words = WakeWords(None, lambda: SHIPPED)

        assert words.effective() == SHIPPED
        assert words.stored() == ()
        assert words.apply(["小夜"]) == "这台机器没有可写的设置存储"

    def test_a_broken_configuration_read_is_said_out_loud_not_hidden(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        def explode() -> tuple[str, ...]:
            raise RuntimeError("config has no wakeword section")

        words = WakeWords(None, explode)

        with caplog.at_level(logging.ERROR, logger="jarvis.app.wake_keywords"):
            assert words.effective() == ()
        assert "could not read wakeword.keywords" in caplog.text

    def test_a_filed_list_that_no_longer_parses_falls_back_and_says_so(
        self, tmp_path: Any, caplog: pytest.LogCaptureFixture
    ) -> None:
        prefs = Preferences(tmp_path / "preferences.json")
        # Written straight past the panel: the shape a person editing the file by hand hits.
        assert prefs.set(VOICE_WAKE_KEYWORDS, ["夜"])

        words = WakeWords(prefs, lambda: SHIPPED)
        with caplog.at_level(logging.WARNING, logger="jarvis.app.wake_keywords"):
            effective = words.effective()

        assert effective == SHIPPED, "存了个会撞到日常说话的词，不如不存"
        assert "stored wake words rejected" in caplog.text

    def test_applying_a_list_moves_the_effective_words(self, tmp_path: Any) -> None:
        prefs = Preferences(tmp_path / "preferences.json")
        words = WakeWords(prefs, lambda: SHIPPED)

        assert words.apply("辛苦你了、小夜起床") is None

        assert words.stored() == ("辛苦你了", "小夜起床")
        assert words.effective() == ("辛苦你了", "小夜起床")

    def test_clearing_the_box_hands_the_shipped_spellings_back(self, tmp_path: Any) -> None:
        prefs = Preferences(tmp_path / "preferences.json")
        words = WakeWords(prefs, lambda: SHIPPED)
        assert words.apply(["辛苦你了"]) is None
        assert words.effective() == ("辛苦你了",)

        assert words.apply("") is None

        assert words.stored() == ()
        assert words.effective() == SHIPPED

    def test_a_refused_write_leaves_the_previous_words_alone(self, tmp_path: Any) -> None:
        prefs = Preferences(tmp_path / "preferences.json")
        words = WakeWords(prefs, lambda: SHIPPED)
        assert words.apply(["辛苦你了"]) is None

        refusal = words.apply(["夜", "辛苦你了"])

        assert refusal is not None and "太短" in refusal
        assert words.effective() == ("辛苦你了",), "一半收下比全收下更难查"

    def test_the_choice_survives_a_restart(self, tmp_path: Any) -> None:
        """A second reader of the same file -- the panel's promise, tested not asserted.

        This is the exact shape that made the model list look unsaved: written fine, read
        back wrong. Two instances over one file is the cheapest way to catch it here.
        """
        path = tmp_path / "preferences.json"
        first = WakeWords(Preferences(path), lambda: SHIPPED)
        assert first.apply(["辛苦你了", "Hey Jarvis"]) is None

        reopened = WakeWords(Preferences(path), lambda: SHIPPED)

        assert reopened.stored() == ("辛苦你了", "Hey Jarvis")
        assert reopened.effective() == ("辛苦你了", "Hey Jarvis")

    def test_the_snapshot_carries_both_lists_and_the_bounds_it_enforces(
        self, tmp_path: Any
    ) -> None:
        prefs = Preferences(tmp_path / "preferences.json")
        words = WakeWords(prefs, lambda: SHIPPED)
        assert words.apply(["辛苦你了"]) is None

        section = words.settings()

        assert section["wake_keywords"] == ["辛苦你了"]
        assert section["wake_keywords_stored"] == ["辛苦你了"]
        assert section["wake_keywords_default"] == list(SHIPPED)
        assert section["wake_keywords_max"] == MAX_WAKE_KEYWORDS
        assert section["wake_keyword_min_chars"] == MIN_KEYWORD_CHARS
        assert section["wake_keyword_max_chars"] == MAX_KEYWORD_CHARS
