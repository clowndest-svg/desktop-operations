"""``jarvis.core.text`` — the shared text primitives.

Only ``speakable`` has interesting behaviour to pin: it is the function that decides
what a voice is allowed to say, and the cases below are each a way it was wrong (or
nearly wrong) while being written. The other helpers are exercised through
``memory``/``knowledge`` retrieval tests, where their effect on ranking is the thing
that actually matters.
"""

from __future__ import annotations

import pytest

from jarvis.core.text import human_bytes, speakable


class TestSpeakable:
    def test_punctuation_becomes_a_pause_not_a_named_character(self) -> None:
        assert speakable("好的，我在。") == "好的 我在"
        assert speakable('他说"你好"，然后走了') == "他说 你好 然后走了"

    def test_percent_is_said_the_chinese_way_round(self) -> None:
        # Leaving the sign in place is how you get a voice that stalls on a glyph;
        # Chinese also says the unit *before* the number.
        assert speakable("命中率 80%") == "命中率 百分之80"
        assert speakable("成功率 99.5%！") == "成功率 百分之99.5"

    def test_digits_keep_what_holds_a_number_together(self) -> None:
        assert speakable("45.0%（12 核）") == "百分之45.0 12 核"
        assert speakable("还有 3,000 项，现在 19:32") == "还有 3,000 项 现在 19:32"

    def test_markdown_structure_goes_and_its_label_stays(self) -> None:
        assert speakable("**重要**：见 [官网](https://example.com/a)") == "重要 见 官网"
        assert speakable("- 第一项\n- 第二项\n| 表头 | 值 |") == "第一项 第二项 表头 值"

    def test_fenced_code_is_not_read_aloud(self) -> None:
        assert speakable("命令是：\n```powershell\nGet-Process | Sort CPU\n```\n就这条") == (
            "命令是 就这条"
        )

    def test_paths_and_urls_lose_their_separators(self) -> None:
        # A path is still worth saying; the backslashes between the parts are not.
        assert speakable(r"C:\Users\me\Desktop") == "C Users me Desktop"
        assert speakable("详见 https://example.com/x?y=1 页面") == "详见 页面"

    def test_emoji_and_symbols_are_silence_not_words(self) -> None:
        assert speakable("完成 ✅😀") == "完成"

    @pytest.mark.parametrize("text", ["", "   ", "\n\n", "，。！？"])
    def test_nothing_speakable_is_empty_not_whitespace(self, text: str) -> None:
        assert speakable(text) == ""

    def test_collapses_the_spaces_it_created(self) -> None:
        assert speakable("a   。  b\t\t、  c") == "a b c"


class TestHumanBytes:
    def test_one_decimal_place_above_bytes(self) -> None:
        assert human_bytes(1536) == "1.5 KB"
        assert human_bytes(500) == "500 B"

    def test_negative_clamps_instead_of_inventing_units(self) -> None:
        assert human_bytes(-1) == "0 B"
