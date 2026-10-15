"""Capability-pack routing: which tools go out with which sentence.

The reason this file exists at all is measured, not theoretical: the built-in table
is 3945 tokens against the shipped tokenizer and the bundled Qwen3-4B is commonly run
at a 4096 window, so the table alone left nothing for the question. Narrowing it is
the difference between a local model that can act and one that cannot.

The guard tests at the bottom are the part to keep. A pack that omits a tool nobody
noticed is a capability that silently disappears, and nothing in the stack complains
when a tool simply is not in the request -- so it has to fail here instead.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from jarvis.app.tool_routing import (
    CORE,
    GENERAL,
    PACKS,
    classify_tool_intent,
    select_tool_names,
)
from jarvis.config.schema import ToolsSection
from jarvis.tools.builtins import build_builtin_tools
from jarvis.tools.policy import ToolPolicy
from jarvis.tools.registry import ToolRegistry


def _intent(text: str) -> str:
    return "+".join(classify_tool_intent(text))


def _registered() -> set[str]:
    """Every tool name the desktop build can produce, from the real assembler."""
    stub = MagicMock()
    section = ToolsSection(
        enabled=True,
        confirm_dangerous=True,
        allow_write=True,
        allow_shell=True,
        file_roots=(),
        max_result_chars=8000,
    )
    policy = ToolPolicy(lambda: section)
    # The registry takes the *settings provider*, not the policy: passing the policy
    # here made every registry-level check read a ToolsSection that was never there.
    registry = ToolRegistry(lambda: section)
    for spec, handler in build_builtin_tools(
        policy,
        monitor_factory=lambda: stub,
        cleaner_factory=lambda: stub,
        computer_factory=lambda: stub,
        shell_gate_factory=lambda: stub,
        speaker=stub,
        reminders=stub,
        memory=stub,
        knowledge=stub,
        monitor=stub,
        usage=stub,
        proposals=stub,
        settings=stub,
        voices=stub,
        conversations=stub,
        alerts=stub,
        mirror=stub,
        images=stub,
        videos=stub,
        documents=stub,
        # 检查更新这条只在装配层真注入了取数函数时才注册 —— 不传就是没有，
        # 而"包里挂了一条产不出来的工具"正是本文件那条守卫要抓的。
        release_fetch=lambda: {"ok": True, "newer": False, "current": "0", "latest": "0"},
        # The read-the-phone-screen tool hangs off the same pack, and it is only built
        # when there is a reader behind it. Leaving it out here made this guard report a
        # live route as a stale name; the point of the guard is the *other* direction.
        phone=stub,
    ):
        registry.register(spec, handler)
    return {spec.name for spec in registry.specs()}


class TestRouting:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("检查本机 JDK 版本", "system+runtime"),
            ("查看 CPU 和内存", "system"),
            ("打开记事本", "open_app"),
            ("搜索今天的天气", "network"),
            ("读取项目 README", "files"),
            ("这台电脑卡不卡", "system"),
            ("git 是什么版本", "runtime"),
            ("十分钟后提醒我喝水", "reminders"),
            ("念给我听", "voice"),
            ("我上次说的那个方案", "memory"),
            ("结束掉卡死的进程", "system+process"),
        ],
    )
    def test_a_concrete_ask_finds_its_pack(self, text: str, expected: str) -> None:
        assert _intent(text) == expected

    @pytest.mark.parametrize(
        "text",
        [
            "你好啊",
            "现在几点了",
            "这套系统的架构设计怎么样",
            "讲个笑话",
            "",
            "   ",
        ],
    )
    def test_ordinary_conversation_does_not_reach_for_the_machine(self, text: str) -> None:
        """「系统设计」 is not a request for telemetry.

        Bare 系统 used to match it, loading four monitoring tools into a question about
        software design -- on a 4K window that pushes out the tools that *were* needed.
        """
        assert classify_tool_intent(text) == ()

    def test_two_asks_in_one_sentence_get_both_packs(self) -> None:
        """「打开浏览器搜一下 X」 needs both halves; picking one makes the other unanswerable."""
        assert _intent("打开浏览器搜一下 Qwen3") == "network+open_app"

    def test_matching_ignores_case(self) -> None:
        assert _intent("CPU 占用") == _intent("cpu 占用") == "system"

    def test_pack_order_is_stable_so_the_label_does_not_flicker(self) -> None:
        assert classify_tool_intent("内存 和 JDK") == classify_tool_intent("JDK 和 内存")

    def test_mixing_chinese_and_english_in_one_ask(self) -> None:
        assert set(classify_tool_intent("帮我看下 node 的版本")) >= {"runtime"}

    @pytest.mark.parametrize(
        "text",
        [
            # 这四句是用户实拍里真说过的话。前三句在 r104 之前一条都没命中 image 包，
            # 于是 generate_image 根本没进请求，她回「当前我无法直接生成图片」——
            # 而那台机器上 ComfyUI 正在 8188 上服务。
            "嗯，随便生成有张图片给我",
            "帮我做一张壁纸",
            "来张图",
            "生成一张橘猫的图",
        ],
    )
    def test_the_way_a_person_actually_asks_for_a_picture(self, text: str) -> None:
        assert "image" in classify_tool_intent(text)

    def test_a_pictorial_ask_with_no_picture_word_still_reaches_the_tool(self) -> None:
        """「生成一个绿色的哥布林」里没有「图」这个字，任何关键词表都抓不住它。

        所以 generate_image 进了兜底包 —— 和 r89 把 ``remember`` 放进兜底包是同一条理由：
        省 token 的规矩不该决定"她能不能被告诉一件事"。
        """
        assert classify_tool_intent("生成一个绿色的哥布林") == ()
        offered = select_tool_names((), sorted(_registered()))
        assert "generate_image" in offered, "兜底包里少了它，用户只能听见「我画不了」"

    def test_being_told_to_remember_reaches_the_memory_pack(self) -> None:
        """r89 measured 「记一下：我的部署端口是 8199」 falling to 兜底 with no ``remember``."""
        for text in (
            "记一下：我的部署端口是 8199",
            "帮我把这个记下来，以后都要用",
            "别忘了我说过的话",
            "记下来，数据库叫 jarvis_prod",
        ):
            assert "memory" in classify_tool_intent(text), text

    def test_pressing_a_key_reaches_the_desktop_pack(self) -> None:
        """「按一下 esc 键」 missed in r89: the rule wanted 按 immediately before the key."""
        for text in ("按一下 esc 键", "按下回车", "按 ctrl", "按 alt+tab", "敲一下空格键"):
            assert "desktop" in classify_tool_intent(text), text

    def test_an_ambiguous_mention_of_a_key_loads_the_pack_and_that_is_the_end_of_it(
        self,
    ) -> None:
        """「快捷键的含义」 is a question, not a request to press one -- and it still matches.

        Recorded rather than fixed: separating the two needs syntax, not keywords, and
        the whole cost of the false positive is five desktop tools (~600 tokens) in a
        window that has room for them since the table stopped being the whole table.
        A rule that silently dropped 「按一下 esc」 to avoid this would be the worse bug.
        """
        assert classify_tool_intent("帮我解释一下快捷键的含义是啥") == ("desktop",)
        assert len(select_tool_names(("desktop",), tuple(PACKS["desktop"]))) <= 6


class TestSelection:
    def test_only_registered_tools_are_offered(self) -> None:
        """A pack naming a tool the build left out contributes nothing.

        ``run_powershell`` is absent when no command level is configured; advertising it
        anyway teaches the model a capability exists and then only ever refuses.
        """
        chosen = select_tool_names(("shell",), ("current_time", "calculate", "run_shell"))
        assert "run_powershell" not in chosen
        assert "run_shell" in chosen

    def test_the_always_on_tools_come_with_every_pack(self) -> None:
        for intent in (*PACKS, ()):
            chosen = select_tool_names(intent, (*CORE, "system_report"))
            assert set(CORE) <= set(chosen)

    def test_no_intent_still_offers_the_conversational_minimum(self) -> None:
        available = ("current_time", "calculate", "knowledge_search", "memory_search")
        assert select_tool_names((), available) == (
            "current_time",
            "calculate",
            "memory_search",
            "knowledge_search",
        )

    def test_selection_never_invents_a_name(self) -> None:
        assert select_tool_names(("system", "runtime"), ()) == ()

    def test_a_tool_shared_by_two_packs_appears_once(self) -> None:
        """``system_report`` sits in both packs; sending it twice costs tokens for nothing."""
        chosen = select_tool_names(("system", "runtime"), ("system_report", "runtime_versions"))
        assert list(chosen).count("system_report") == 1
        assert chosen == ("system_report", "runtime_versions")

    def test_a_string_intent_is_accepted(self) -> None:
        assert select_tool_names("open_app", ("open_app",)) == ("open_app",)


class TestPacksStayWhole:
    def test_every_registered_tool_is_reachable_by_some_pack(self) -> None:
        """The guard: a new tool nobody routed is a capability that quietly vanished."""
        unreachable = _registered() - {t for pack in PACKS.values() for t in pack} - set(CORE)
        assert unreachable == set()

    def test_no_pack_names_a_tool_the_assembler_cannot_produce(self) -> None:
        """The other direction: a stale name in a pack is a typo, not a fallback."""
        registered = _registered()
        stale = {
            (pack, tool)
            for pack, tools in PACKS.items()
            for tool in tools
            if tool not in registered
        }
        assert stale == set()

    def test_the_general_pack_stays_small_enough_for_a_4k_window(self) -> None:
        """No match is the common case, so the fallback is the pack that has to fit.

        Measured r88: the whole table is 3945 tokens and the largest single pack 849,
        against a 4096 window. A count ceiling is the cheap stand-in for that -- the
        thing being guarded is the fallback growing a tool at a time until it is the
        whole table again, which is how this file came to exist.

        r119 raised 8 -> 9 the same way the ceiling was first drawn: 3945 / 35 tools
        is ~113 tokens a tool, so the ninth costs about 2.8% of the window and the
        pack still sits at a quarter of the table.
        """
        general = set(PACKS[GENERAL]) | set(CORE)
        assert len(general) <= 9, f"兜底包已经 {len(general)} 个工具：{sorted(general)}"
        assert len(general) < len({t for pack in PACKS.values() for t in pack})

    def test_the_fallback_carries_both_directions_of_looking_things_up(self) -> None:
        """联网查外面的世界，和翻我们俩聊过的话 —— 少一条，那条能力就等于不存在。

        r119 的实测形状：为了让没命中任何关键词的问句也能自动联网（``chat_service`` 只在
        ``search_web`` 真的随请求出去时才检索），兜底包换进了 ``search_web``，代价是
        ``chat_search`` 掉出兜底 —— 于是「我们聊过的那个方案」这类没踩到 上次/之前说 的句子，
        历史检索静默消失。两条都留。
        """
        for tool in ("search_web", "chat_search"):
            assert tool in PACKS[GENERAL], f"兜底包丢了 {tool}"
        offered = set(select_tool_names((), sorted(_registered()))) | set(CORE)
        assert {"search_web", "chat_search"} <= offered


def test_the_picture_word_may_sit_a_few_chars_behind_the_verb() -> None:
    """中文没有词边界：「随便生成有张图片给我」里，动词和"图"字隔了两个字。

    单独一条不参数化的测试是给变异验证用的 —— 参数化那一条一个 nodeid 收四例，
    "点不到恰好一条"的判据就没法用它。
    """
    for text in ("嗯，随便生成有张图片给我", "帮我做一张壁纸", "来张图", "给我整个头像"):
        assert "image" in classify_tool_intent(text), text
