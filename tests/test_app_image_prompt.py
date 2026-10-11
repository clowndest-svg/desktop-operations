"""中文那句"画什么"→ 引擎听得懂的英文标签：`jarvis/app/image_prompt` 的规矩。

这一层是纯函数，所以全部判据都能钉死，不需要引擎。真正要钉的是**三条决定**：

1. **没有中文就一个字都不许动** —— 对话里模型已经把中文扩写成英文标签了，再被这层
   改一次就是外行指导内行；
2. **属性要带着主体再说一遍**（``thin goblin``）—— 这是 24 张真图量出来的：中心绿
   0.52 / 外圈 0.17，而只写 ``thin`` 的那一组是 0.05~0.16 且不比背景更绿；
3. **不许替用户编场景和风格** —— 加了 "dark forest, moonlight" 的那一组整张图均匀泛绿
   （外圈 0.77 比中心 0.69 还绿），量的是背景被染色，不是主体变绿。
"""

from __future__ import annotations

from jarvis.app.image_prompt import capabilities, expand, has_chinese


class TestItLeavesAloneWhatItShouldNotTouch:
    def test_an_english_string_comes_back_byte_identical(self) -> None:
        said = "green goblin, green skin, muscular, pointed ears, fantasy illustration"
        assert expand(said).prompt == said

    def test_an_empty_string_does_not_become_a_surprise(self) -> None:
        assert expand("   ").prompt == ""
        assert expand("").unknown == ()

    def test_nothing_matched_means_nothing_invented(self) -> None:
        """一个词都认不出来时原样退回：拼半句英文糊弄，比画错更难被发现。"""
        assert expand("这张不画").prompt == "这张不画"


class TestAttributesStickToTheSubject:
    def test_color_sits_in_front_of_the_noun(self) -> None:
        assert expand("画一个绿色的哥布林").prompt.startswith("green goblin")

    def test_each_attribute_repeats_the_subject(self) -> None:
        answer = expand("画一个绿色瘦弱的哥布林")
        assert "thin goblin" in answer.prompt, "只写 thin 的那一组量出来主体并不更绿"
        assert "green skin" in answer.prompt

    def test_repetition_stops_at_three_attributes(self) -> None:
        """每个属性都重复一遍会把名词本身淹掉 —— 淹掉之后谁都不占权重。"""
        answer = expand("画一个绿色瘦弱高大凶猛的哥布林")
        assert answer.prompt.count("goblin") <= 4, answer.prompt

    def test_a_single_character_color_still_composes(self) -> None:
        assert expand("画一只白猫").prompt.startswith("white cat")

    def test_a_non_living_subject_does_not_get_a_skin(self) -> None:
        """ "green skin" 只对会动的东西说：给一座山写皮肤，是在往提示词里塞噪声。

        钉整串而不是"含不含"：变异验证里有一条（去重只去完全相同的）就是被
        `"green mountain" in prompt` 这种弱判据放过去的 —— 它给出的
        "green mountain, mountain" 一样"含"。
        """
        assert expand("画一座绿色的山").prompt == "green mountain"


class TestItDoesNotInventWhatTheUserDidNotSay:
    def test_no_scene_words_appear_when_the_user_named_none(self) -> None:
        prompt = expand("画一个绿色的哥布林").prompt
        for invented in ("forest", "moonlight", "night", "illustration"):
            assert invented not in prompt, f"替用户编了 {invented}：那是背景在泛绿，不是主体"

    def test_a_scene_the_user_did_name_is_kept(self) -> None:
        assert "moonlight" in expand("月光下的哥布林").prompt


class TestWordsItCannotKnowAreSaidOutLoud:
    def test_unknown_words_are_reported_not_dropped(self) -> None:
        answer = expand("画一只喷火的麒麟兽")
        assert "qilin" in answer.prompt
        assert answer.unknown and "没翻出来" in answer.note

    def test_a_suffix_that_would_read_as_a_noun_is_eaten_whole(self) -> None:
        """ "插画风"要整个匹配：先吃"插画"会剩一个"风"，而风在英文里是 windy。"""
        answer = expand("一只橘猫，插画风")
        assert "illustration" in answer.prompt
        assert not any("风" in word for word in answer.unknown)

    def test_measure_words_never_become_part_of_the_picture(self) -> None:
        prompt = expand("帮我画一张壁纸").prompt
        assert "wallpaper" in prompt
        assert not has_chinese(prompt)


class TestTheTableItself:
    def test_capabilities_quotes_a_real_count(self) -> None:
        """界面上那句"内置 N 个词"必须是数出来的，不是手写的。"""
        from jarvis.app.image_prompt import GROUPS

        words = sum(len(table) for table in GROUPS.values())
        assert str(words) in capabilities()

    def test_the_bare_noun_is_dropped_once_a_compound_said_it(self) -> None:
        """被包住的裸标签要去掉："green mountain" 已经说了 mountain，再补一个就是重复占权重。

        判据是整串相等。写 `"green mountain" in prompt` 的那一版放走了一条真变异
        （去重只去完全相同的 → "green mountain, mountain"），这就是"含不含"和
        "是不是"的差别。
        """
        assert expand("画一座绿色的山").prompt == "green mountain"
        assert expand("画一只白色的大鸟").prompt.count("bird") == 1

    def test_no_entry_maps_to_itself(self) -> None:
        from jarvis.app.image_prompt import GROUPS

        for group, table in GROUPS.items():
            for zh, en in table.items():
                assert en.strip() and not has_chinese(en), f"{group} 里 {zh} 翻出了中文"
