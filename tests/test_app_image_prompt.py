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

from jarvis.app.image_prompt import capabilities, expand, expand_model, has_chinese, merge


class TestItLeavesAloneWhatItShouldNotTouch:
    def test_an_english_string_comes_back_byte_identical(self) -> None:
        said = "green goblin, green skin, muscular, pointed ears, fantasy illustration"
        assert expand(said).prompt == said

    def test_an_empty_string_does_not_become_a_surprise(self) -> None:
        assert expand("   ").prompt == ""
        assert expand("").unknown == ()

    def test_nothing_matched_means_nothing_goes_out(self) -> None:
        """一个词都认不出来时回**空串**：拼半句英文糊弄难发现，送汉字进去更难发现。

        r112 之前这里回原话，理由写的是"不许拼半句英文糊弄"——那个顾虑是对的，
        但选的解法是错的：回原话等于把汉字当提示词交给只认英文的编码器。
        从那张图的 PNG 里读出来的现场是 ``…, 头 人 起 舞 头 饰 随 舞 动 …``，
        21 个汉字被逐字拆开送进 ``CLIPTextEncode``，画出来一团粉色色块，**还报了成功**。
        回空串，调用方才有机会说"这句我没听懂"而不是交一张噪声。
        """
        answer = expand("这张不画")
        assert answer.prompt == ""
        assert answer.unknown, "认不出来就要说出来，不许静默丢掉"


class TestAttributesStickToTheSubject:
    def test_color_sits_in_front_of_the_noun(self) -> None:
        assert expand("画一个绿色的哥布林").prompt.startswith("green goblin")

    def test_the_measured_goblin_line_is_byte_for_byte_what_it_was(self) -> None:
        """r105/r106 量出来的那一句一个字都不许变：这一轮所有改动都必须绕开它。"""
        assert expand("画一个绿色瘦弱的哥布林").prompt == "green goblin, green skin, thin goblin"

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


class TestNothingUntranslatedReachesTheEngine:
    """SD1.5 的文本编码器一个汉字都不认。

    r110 实拍：「在月光下偏偏起舞的猪头人」那一次，提示词尾巴上挂了 21 个被空格
    隔开的汉字，真英文标签只有 7 个 —— 噪声占四分之三，画出来是一团认不出的东西。
    这一类判据一律钉"提示词里不许有汉字"，不是钉"含不含某个词"。
    """

    PIG = "生成一张在月光下偏偏起舞的猪头人"

    def test_the_users_own_sentence_comes_back_without_a_single_han_character(self) -> None:
        answer = expand(self.PIG)
        assert not has_chinese(answer.prompt), answer.prompt

    def test_what_it_could_not_translate_is_still_said_out_loud(self) -> None:
        """不进提示词 ≠ 不吭声：用户要知道哪个词丢了，才知道该换个说法。"""
        answer = expand(self.PIG)
        assert answer.unknown, "偏偏 这种词没认出来却没报"
        assert "没有进提示词" in answer.note, answer.note
        assert "偏偏" in answer.note

    def test_the_three_things_the_user_asked_for_are_all_in_the_prompt(self) -> None:
        """猪头人 / 月光 / 起舞 —— 少任何一个都是"画得不像"，而它其实是"没翻出来"。"""
        # 判**标签表**，不判那句拼起来的串：`(pig head:1.4)` 和 `humanoid` 当成一个标签
        # 塞进去，字符串判据一样"含"，而引擎看到的是同一个 token 串
        tags = expand(self.PIG).tags
        assert "(pig head:1.4)" in tags and "humanoid" in tags
        prompt = expand(self.PIG).prompt
        assert "moonlight" in prompt
        assert "dancing" in prompt, "起舞不在词典时它整个消失，图上就没人跳舞"

    def test_a_dance_pulls_a_composition_that_can_show_it(self) -> None:
        """只写 dancing 会把人裁到腰，舞步根本看不见（4 个种子：0/4 vs 3/4）。"""
        assert "full body" in expand(self.PIG).prompt
        assert "dynamic pose" in expand(self.PIG).prompt

    def test_a_framing_the_user_named_wins_over_that_default(self) -> None:
        """用户说了"特写"就不补全身位 —— 补了就是把她的构图改掉。"""
        prompt = expand("特写：跳舞的猫耳少女").prompt
        assert "close-up" in prompt
        assert "full body" not in prompt, prompt

    def test_a_compound_is_not_eaten_by_a_single_character_inside_it(self) -> None:
        """ "篝火"里的"火"是单字条目：先吃"火"就得到一个 fiery，图上没有火堆。"""
        answer = expand("猪头人在篝火旁起舞")
        assert "bonfire" in answer.prompt
        assert "fiery" not in answer.prompt, answer.prompt

    def test_a_meta_word_does_not_become_a_body_part(self) -> None:
        """ "角色"里的"角"会被单字条目吃成 horns（角），于是谁头上都长角。"""
        assert "horns" not in expand("一个角色在月光下跳舞").prompt


class TestTheModelsTranslationDoesNotOwnTheSubject:
    """上面那一整个 `TestNothingUntranslatedReachesTheEngine` 全是绿的，图还是错的。

    为什么：那些判据量的是 ``expand(用户原话)``，而**对话那条路从来不走这一步**。
    模型自己把中文扩写成英文标签再调 ``generate_image``，进引擎的是模型那串。
    r112 从那张图的 PNG 里读出来的现场：

    ``pink pig, pink skin, glowing pig, fiery pig, stars, moonlight, dreamlike, 头 人 起 舞 …``

    模型把「猪头人」翻成了**一头粉色的猪**，还把「翩翩起舞」整个丢了。词典对同一句给的是
    ``(pig head:1.4), anthropomorphic pig, humanoid`` —— 那几个标签是对着 anythingV3 调过的。
    所以这一类判据钉的是**两串撞上时谁赢**，不是"词典翻得对不对"。
    """

    PIG = "生成一张在月光下翩翩起舞的猪头人"
    MODEL_WROTE = "pink pig, pink skin, glowing pig, fiery pig, stars, moonlight, dreamlike"
    # 模型把用户那句话拆成单字漏下来的那一段（r112 从那张图的 PNG 里读出来的现场）。
    DEBRIS = "头 人 起 舞 头 饰 随 舞 动 背 景 温 暖 篝 周 围 点 缀 为 童 话"

    def test_the_table_wins_the_subject_outright(self) -> None:
        """词典认出主体，模型那串就整份丢掉 —— 拼在一起还是一头粉猪。"""
        final = merge(expand(self.PIG), expand(self.MODEL_WROTE))
        assert "pink pig" not in final.prompt, final.prompt
        assert "fiery pig" not in final.prompt, final.prompt
        assert "(pig head:1.4)" in final.tags and "humanoid" in final.tags

    def test_what_the_model_dropped_comes_back(self) -> None:
        """模型把「翩翩起舞」整句丢了；词典认得，所以图上得有人在跳舞。"""
        final = merge(expand(self.PIG), expand(self.MODEL_WROTE))
        assert "dancing" in final.prompt
        assert "moonlight" in final.prompt

    def test_the_debris_the_model_left_never_survives_the_merge(self) -> None:
        """模型漏下来的汉字一个都不许进最终提示词 —— 它们就是那团色块。"""
        # 这里用 `expand` 而不是生产路径的 `expand_model`：前者是更严的那一条
        # （`expand_model` 已经先把汉字抹了，断言会变成走过场）。
        dirty = f"{self.MODEL_WROTE}, {self.DEBRIS}"
        final = merge(expand(self.PIG), expand(dirty))
        assert not has_chinese(final.prompt), final.prompt

    def test_a_subject_the_table_does_not_know_leaves_the_model_in_charge(self) -> None:
        """词典认出主体，也不许把模型写的场景一起丢掉。

        这条是"主体归词典"的边界，而且它抓到过一版真的错：第一版 merge 写的是
        "认出主体就整份用词典那份"，于是这一句里 ``neon lights, rain, night city``
        全被丢掉 —— 主体词典确实认得（摩托车），但模型那串**根本没搞错主体**，
        丢掉它就是把能画的变成画不了。
        """
        spoken = expand("画一台赛博朋克风格的摩托车")
        written = expand("cyberpunk motorcycle, neon lights, rain, night city")
        final = merge(spoken, written)
        assert "motorcycle" in final.tags, final.prompt
        assert "neon lights" in final.tags, final.prompt
        assert "night city" in final.tags and "rain" in final.tags, final.prompt
        # 抢主体的那条换成词典的说法，不许两个"摩托车"一起占权重
        assert "cyberpunk motorcycle" not in final.tags, final.prompt

    def test_the_table_still_adds_what_it_knows_when_it_has_no_subject(self) -> None:
        """没主体不等于没贡献：光照、动作、构图这些认出来了就补上去。"""
        spoken = expand("在月光下翩翩起舞")
        assert not spoken.subjects, spoken.subjects
        final = merge(spoken, expand("cyberpunk motorcycle, neon lights"))
        assert "cyberpunk motorcycle" in final.prompt
        assert "moonlight" in final.prompt and "dancing" in final.prompt

    def test_the_shipped_prompt_is_byte_for_byte_the_string_that_was_measured(self) -> None:
        """改完之后**真正进引擎**的那一句，正是量出 8/8 的那一句 —— 一个字都不许多。

        r112 的探针脚本一开始直接调 `expand(模型那串)`，印出来的合并结果和线上跑的不是
        同一件事（差在 `expand_model` 剥汉字那一步）。"量的东西和跑的东西不一样"比量错
        还难发现，所以把整串钉在这里当锚：谁改了词典、改了主体顺序、改了合并规矩，
        这一条立刻红。

        这串换过一次：原先钉的是 `(pig head:1.4), humanoid`（N=4 下 4/4）。后来补了一轮
        配对的 N=8（同一批全新种子、两串都跑），旧那串只有 3/8、新那串 8/8，所以换掉 ——
        锚跟着**真正量过的那一串**走，不跟着"当时看起来最好的那一串"走。
        """
        dirty = f"{self.MODEL_WROTE}, {self.DEBRIS}"
        final = merge(expand(self.PIG), expand_model(dirty))
        assert final.prompt == (
            "(pig head:1.4), anthropomorphic pig, humanoid, moonlight, full body, "
            "dynamic pose, dancing, stars, dreamlike"
        ), final.prompt

    def test_an_unrecognised_sentence_leaves_the_model_untouched(self) -> None:
        """词典一个词都没认出来时，原封不动用模型那串 —— 不许退化成空提示词。"""
        written = expand("green goblin, dark forest")
        final = merge(expand("这张不画"), written)
        assert final.prompt == "green goblin, dark forest"

    def test_the_model_does_not_paint_a_subject_the_user_left_uncoloured(self) -> None:
        """`pink skin` 不含主体词，所以第一版 merge 放它过去了 —— 可它和 `pink pig` 是同一层东西。

        模型把「猪」读成「粉猪」，顺手写下 `pink skin`；用户原话里**一个颜色都没有**。
        词典自己给会动的主体说颜色时用的正是 `{color} skin` 这个形状，所以这个位置本来就归
        词典 —— 用户没说，那就是空着。r112 量过，同一批 4 个种子：
        带 `pink skin` 的那组粉色像素 56.3%、平均饱和 0.325，去掉之后 35.8%、0.209，
        而用户报的原话就是「粉色的一团」。
        """
        final = merge(expand(self.PIG), expand(self.MODEL_WROTE))
        assert "pink" not in final.prompt, final.prompt
        assert "pig head" in final.prompt, "颜色没了，主体不许跟着没"

    def test_a_colour_the_user_did_name_is_not_stripped_from_the_model(self) -> None:
        """用户自己点了颜色，这一条就不许开火 —— 否则"修一个 bug 换一个 bug"。"""
        final = merge(expand("画一只绿猫"), expand("green cat, green fur, dark forest"))
        assert "green fur" in final.prompt, final.prompt

    def test_the_colour_it_strips_is_a_body_colour_not_a_scene_one(self) -> None:
        """只剥"长在主体身上"的颜色。`blue moonlight` 是场景的光，不是模型给主体上的色。

        r105 量的是"不许替用户编场景"（加了 dark forest, moonlight 之后整张图泛绿），
        和这一条不是一回事，别顺手一起删。
        """
        final = merge(expand(self.PIG), expand("pig head, blue moonlight, stars"))
        assert "blue moonlight" in final.prompt, final.prompt


class TestTheModelsBrokenChineseIsNotASpec:
    """模型漏下来的汉字残渣**不许被词典当正经词吃成标签**。

    r112 实拍那串的尾巴是 ``…, 头 人 起 舞 头 饰 随 舞 动 背 景 温 暖 篝 周 围 点 缀 为 童 话``
    —— 模型把用户那句话拆成了单字。里面的「头 饰」「温 暖」「童 话」被词典匹配上了，
    于是 ``hair ornament`` / ``warm lighting`` / ``fairytale`` 三个标签进了提示词。
    拆碎的噪声变成像模像样的标签，**比它原样当噪声递进去更隐蔽** —— 而且 ``warm lighting``
    还和用户真说了的 ``moonlight`` 打架。`zh` 在的时候 `prompt` 里的汉字一定是残渣，
    所以走 `expand_model`：模型写了什么英文就算什么。
    """

    DIRTY = "pink pig, stars, 头 饰 随 舞 动 背 景 温 暖 篝 周 围 点 缀 为 童 话"

    def test_no_tag_is_mined_out_of_the_debris(self) -> None:
        prompt = expand_model(self.DIRTY).prompt
        for mined in ("hair ornament", "warm lighting", "fairytale"):
            assert mined not in prompt, f"从碎汉字里挖出了 {mined}：{prompt}"

    def test_the_english_the_model_really_wrote_survives(self) -> None:
        prompt = expand_model(self.DIRTY).prompt
        assert "stars" in prompt, prompt

    def test_the_users_own_sentence_still_goes_through_the_full_table(self) -> None:
        """剥汉字只对**模型那串**做：面板把用户原话直接递进来时只走 `expand`。"""
        assert "dancing" in expand("生成一张在月光下翩翩起舞的猪头人").prompt
        assert expand_model("生成一张在月光下翩翩起舞的猪头人").prompt == ""


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
