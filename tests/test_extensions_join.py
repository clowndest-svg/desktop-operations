"""端到端：真起一个 stdio MCP 子进程，走真 transport，把它的工具调到答出话为止。

这条测试存在的全部理由是**别用手搓的接线**：`jarvis/mcp/` 和子进程之间是行分隔的 JSON 帧、
真实 stdin/stdout、stderr 要有人抽、进程可能压根起不来。任何 dict 假体都会跳过这些里的一切，
而跳过的正好是会在买家机器上坏掉的那部分（提醒那条链就这么哑过一次：测试全绿、真机不响）。

技能那两半也在这儿合起来验：说明书进系统提示的是**索引**，正文由 `use_skill` 现取；
以及随包那个 4K 模型那一侧 —— 扩展工具必须被带进表里，否则"装完用不上"且没有任何报错。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from jarvis.app.chat_service import ChatService
from jarvis.app.chat_service import _Pair as ChatServicePair
from jarvis.app.tool_routing import EXTENSION_PREFIXES, select_tool_names
from jarvis.config.schema import McpSection, ToolsSection
from jarvis.extensions.servers import load, path_for, save, set_layer_enabled, upsert
from jarvis.mcp.service import McpService
from jarvis.skills.loader import SKILL_FILENAME
from jarvis.skills.service import SkillService
from jarvis.tools.registry import ToolRegistry
from tests._fakes import FakeLlmClient

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "mcp_probe_server.py"

SKILL_TEXT = (
    "---\nname: poster\ndescription: 做海报用的说明书，先想清楚再画\n---\n\n# 步骤\n先想，再画。\n"
)


def _tools_section() -> ToolsSection:
    return ToolsSection(
        enabled=True,
        confirm_dangerous=True,
        allow_write=False,
        allow_shell=False,
        file_roots=(),
        max_result_chars=20_000,
    )


def _empty_mcp() -> McpSection:
    return McpSection(
        enabled=False, connect_timeout_seconds=20.0, call_timeout_seconds=20.0, servers={}
    )


@pytest.fixture()
def probe(tmp_path: Path) -> dict[str, Any]:
    """写好 servers.json 的一台"刚装完一条本地 stdio 服务"的机器。"""
    mark = tmp_path / "mark.txt"
    extensions = tmp_path / "扩展"
    extensions.mkdir(parents=True, exist_ok=True)
    data, name = upsert(
        load(path_for(extensions)),
        {
            "name": "probe",
            "transport": "stdio",
            "command": sys.executable,
            "args": [str(FIXTURE), "-u"],
            "env_from": ["XIAOYE_PROBE_ENV", "XIAOYE_PROBE_MARK"],
        },
    )
    assert name == "probe"
    save(path_for(extensions), set_layer_enabled(data, True))
    # 两个值都只走"环境变量名"这条路：子进程拿得到，servers.json 里一个字都没有。
    environ = {
        "XIAOYE_PROBE_ENV": "从环境里带来的值",
        "XIAOYE_PROBE_MARK": str(mark),
        "SYSTEMROOT": str(Path(sys.executable).drive or "/"),
    }
    return {"extensions": extensions, "mark": mark, "environ": environ}


def _start(probe: dict[str, Any]) -> tuple[McpService, ToolRegistry]:
    from jarvis.extensions.servers import merge

    registry = ToolRegistry(_tools_section)
    section, missing, error = merge(_empty_mcp(), path_for(probe["extensions"]), probe["environ"])
    assert error == "", error
    assert missing == {}, missing
    service = McpService(lambda: section, lambda: registry)
    service.start()
    return service, registry


class TestARealServerComesUp:
    def test_the_child_process_really_ran(self, probe: dict[str, Any]) -> None:
        service, _registry = _start(probe)
        try:
            assert probe["mark"].read_text(encoding="utf-8").strip() == "started"
        finally:
            service.stop()

    def test_its_tool_is_registered_under_the_qualified_name(self, probe: dict[str, Any]) -> None:
        service, registry = _start(probe)
        try:
            assert "mcp__probe__probe_echo" in registry.names()
        finally:
            service.stop()

    def test_calling_it_goes_through_the_wire_and_comes_back_text(
        self, probe: dict[str, Any]
    ) -> None:
        service, registry = _start(probe)
        try:
            answer = registry.invoke("mcp__probe__probe_echo", {"text": "你好"})
            assert answer.ok, answer.error
            assert "echo:你好" in answer.output
        finally:
            service.stop()

    def test_env_from_reaches_the_child_without_the_secret_touching_the_file(
        self, probe: dict[str, Any]
    ) -> None:
        service, registry = _start(probe)
        try:
            answer = registry.invoke("mcp__probe__probe_echo", {"text": "x"})
            assert "env:从环境里带来的值" in answer.output
            text = path_for(probe["extensions"]).read_text(encoding="utf-8")
            assert "从环境里带来的值" not in text and "XIAOYE_PROBE_ENV" in text
        finally:
            service.stop()

    def test_a_server_side_error_becomes_a_sentence_not_an_exception(
        self, probe: dict[str, Any]
    ) -> None:
        """工具自己报 isError：模型要拿到那句，而不是这一轮炸掉。"""
        service, registry = _start(probe)
        try:
            weird = [name for name in registry.names() if name.startswith("mcp__probe__")]
            target = next(name for name in weird if "probe_echo" not in name)
            answer = registry.invoke(target, {})
            assert not answer.ok or "未知工具" in answer.output
        finally:
            service.stop()

    def test_stopping_takes_its_tools_out_of_the_table(self, probe: dict[str, Any]) -> None:
        service, registry = _start(probe)
        assert any(name.startswith("mcp__probe__") for name in registry.names())
        service.stop()
        assert not any(name.startswith("mcp__probe__") for name in registry.names())

    def test_a_server_that_cannot_start_is_reported_and_the_rest_survives(
        self, tmp_path: Path
    ) -> None:
        extensions = tmp_path / "扩展"
        extensions.mkdir(parents=True, exist_ok=True)
        data, _ = upsert(
            load(path_for(extensions)),
            {"name": "broken", "command": "这一台机器上没有的那个命令", "args": []},
        )
        save(path_for(extensions), set_layer_enabled(data, True))
        from jarvis.extensions.servers import merge

        section, _missing, error = merge(_empty_mcp(), path_for(extensions), {})
        assert error == ""
        registry = ToolRegistry(_tools_section)
        service = McpService(lambda: section, lambda: registry)
        service.start()
        try:
            stats = service.stats()
            assert stats["connected"] == 0 and stats["errors"], "起不来必须留下原文，不能静默"
            assert registry.names() == []
        finally:
            service.stop()

    def test_a_closed_layer_spawns_nothing_at_all(self, probe: dict[str, Any]) -> None:
        """没开这一层 = 一个子进程都不该有：CPU、内存、以及"我机器上怎么多了个黑框"。"""
        from jarvis.extensions.servers import merge

        data = load(path_for(probe["extensions"]))
        save(path_for(probe["extensions"]), set_layer_enabled(data, False))
        section, _missing, _ = merge(
            McpSection(
                enabled=False, connect_timeout_seconds=5.0, call_timeout_seconds=5.0, servers={}
            ),
            path_for(probe["extensions"]),
            probe["environ"],
        )
        # 关掉总开关时，合并出来的这一段也不该带着 server 去连：装配点走的是 enabled 那一支
        registry = ToolRegistry(_tools_section)
        service = McpService(lambda: section, lambda: registry)
        service.start()
        try:
            assert not probe["mark"].exists() or "started" not in probe["mark"].read_text(
                encoding="utf-8"
            )
        finally:
            service.stop()


class TestTheModelActuallyGetsThem:
    def test_extension_tools_are_carried_for_the_bundled_local_model(self) -> None:
        """随包 4B 走关键词路由，而 `mcp__github__create_issue` 这种名字不在任何包里。

        不带 = 用户装完、页面说就绪、模型那侧的表里根本没有它，表现是「这个我做不了」且
        没有任何报错。这和 r105 那次"生成一个绿色的哥布林"没带生图工具是同一类静默消失。
        """
        available = ["current_time", "calculate", "mcp__probe__probe_echo", "use_skill"]
        plain = select_tool_names("system", available)
        with_extra = select_tool_names(
            "system", available, extra=("mcp__probe__probe_echo", "use_skill")
        )
        assert "mcp__probe__probe_echo" not in plain, "前提：不带就真的没有"
        assert {"mcp__probe__probe_echo", "use_skill"} <= set(with_extra)

    def test_the_filter_only_admits_tools_that_survived_the_policy_screen(self) -> None:
        """`to_openai_tools()` 已经按策略筛过；绕过它去读 specs() 会把被挡掉的工具请回来。"""
        specs = [
            SimpleNamespace(name="mcp__probe__probe_echo", source="mcp:probe"),
            SimpleNamespace(name="mcp__gone__x", source="mcp:gone"),
            SimpleNamespace(name="use_skill", source="skill:installed"),
            SimpleNamespace(name="read_file", source="builtin"),
            SimpleNamespace(name="extensions_list", source="extension:market"),
        ]
        tools = SimpleNamespace(specs=lambda: specs)
        full = [
            {"function": {"name": "mcp__probe__probe_echo"}},
            {"function": {"name": "use_skill"}},
            {"function": {"name": "read_file"}},
        ]
        chosen = ChatService._extension_tool_names(tools, full)
        assert sorted(chosen) == ["mcp__probe__probe_echo", "use_skill"]
        assert "mcp__gone__x" not in chosen and "read_file" not in chosen

    def test_the_prefixes_the_router_recognises_are_the_ones_the_layers_use(self) -> None:
        """名字连接要有端到端断言：三处前缀漂一处，扩展工具就永远进不了表。"""
        assert EXTENSION_PREFIXES == ("mcp:", "skill:", "plugin:", "extension:")


class TestSkillsReachThePrompt:
    def _skills(self, root: Path) -> SkillService:
        (root / "skills" / "poster").mkdir(parents=True)
        (root / "skills" / "poster" / SKILL_FILENAME).write_text(SKILL_TEXT, encoding="utf-8")
        service = SkillService(directory_provider=lambda: root / "skills", preferences=None)
        service.start()
        return service

    def test_the_index_lands_in_the_system_prompt_and_the_body_does_not(
        self, tmp_path: Path
    ) -> None:
        skills = self._skills(tmp_path)
        client = FakeLlmClient("好")
        service = ChatService(lambda: client, skills_index_provider=skills.prompt_index)
        service.start()
        service.ask("帮我做张海报")
        system = "".join(
            message.content for message in client.calls[0] if message.role.value == "system"
        )
        assert "poster" in system and "做海报用的说明书" in system
        assert "先想，再画" not in system, "正文不该被塞进提示 —— 那是 use_skill 现取的东西"

    def test_no_skills_no_lines(self, tmp_path: Path) -> None:
        skills = SkillService(directory_provider=lambda: tmp_path / "空", preferences=None)
        skills.start()
        client = FakeLlmClient("好")
        service = ChatService(lambda: client, skills_index_provider=skills.prompt_index)
        service.start()
        service.ask("现在几点")
        system = "".join(
            message.content for message in client.calls[0] if message.role.value == "system"
        )
        assert "可用的技能" not in system

    def test_a_provider_that_throws_does_not_eat_the_answer(self, tmp_path: Path) -> None:
        def boom() -> str:
            raise RuntimeError("读盘炸了")

        client = FakeLlmClient("照样答")
        service = ChatService(lambda: client, skills_index_provider=boom)
        service.start()
        reply = service.ask("现在几点")
        assert reply.answer == "照样答" and reply.error == ""

    def test_use_skill_is_the_way_the_body_gets_read(self, tmp_path: Path) -> None:
        from jarvis.skills.service import USE_SKILL

        registry = ToolRegistry(_tools_section)
        skills = SkillService(
            directory_provider=lambda: tmp_path / "skills",
            registry_provider=lambda: registry,
            preferences=None,
        )
        (tmp_path / "skills" / "poster").mkdir(parents=True)
        (tmp_path / "skills" / "poster" / SKILL_FILENAME).write_text(SKILL_TEXT, encoding="utf-8")
        skills.start()
        assert USE_SKILL in registry.names()
        answer = registry.invoke(USE_SKILL, {"name": "poster"})
        assert answer.ok and "先想，再画" in answer.output
        assert json.dumps(answer.to_dict(), ensure_ascii=False)


class TestTheBundledTurnAsksForThem:
    """判据必须走真的 ChatService._tool_schemas，不能只调 select_tool_names。

    本轮第一版只调了那个纯函数，于是把 ChatService 里 `extra=` 那一整段删掉，测试仍然全绿
    ——「接了但没带上」这个失败面就正好在断言底下。和提醒那条链同一个坑。
    """

    class _Tools:
        """ToolService 的那一面：`specs()` 是**方法**，写成属性会在调用点炸。"""

        enabled = True

        def __init__(self) -> None:
            self._specs = [
                SimpleNamespace(name="current_time", source="builtin"),
                SimpleNamespace(name="use_skill", source="skill:installed"),
                SimpleNamespace(name="mcp__probe__probe_echo", source="mcp:probe"),
            ]

        def specs(self) -> list[SimpleNamespace]:
            return list(self._specs)

        def to_openai_tools(self) -> list[dict[str, object]]:
            return [{"function": {"name": one.name}} for one in self._specs]

    def _table(self, provider: str) -> tuple[list[str], str]:
        service = ChatService(
            lambda: FakeLlmClient("x"), tool_provider=lambda: cast(Any, self._Tools())
        )
        service.start()
        pair = ChatServicePair(provider=provider, model="Qwen3-4B", chosen=True)
        tools, pack = service._tool_schemas(pair, intents=())
        # 这份表的结构就是 OpenAI 那一段的形状，读它不值得为测试再编一套类型。
        return [str(tool["function"]["name"]) for tool in tools], pack  # type: ignore[index]

    def test_the_bundled_turn_carries_mcp_and_skill_tools(self) -> None:
        names, pack = self._table("local")
        assert pack, "随包那一档必须说清带了哪个包，界面上才写得出来"
        assert {"use_skill", "mcp__probe__probe_echo"} <= set(
            names
        ), f"装好了、表里却没有它：{names}"

    def test_a_cloud_turn_still_gets_the_whole_table(self) -> None:
        """云端拿全表、不分包 —— 这一条是给上面那条当对照：包标签为空是**设计**，不是漏。"""
        names, pack = self._table("deepseek")
        assert len(names) == 3 and pack == ""


class TestTheAssemblyActuallyPassesTheObjects:
    """策略写在类里、单测全绿，也不等于装配表把它传进来了。

    这条是对着 `desktop.py` 的构造函数签名读的：上一轮 `ShellLifecycle.set_audio_owner`
    就是这么哑掉的（回调从来没传进去），所以凡是"新加一层、界面要能看见"的改动，
    都要有一条这样的读装配断言。
    """

    def test_the_desktop_run_signature_carries_the_three_new_surfaces(self) -> None:
        import inspect

        from jarvis.ui import desktop

        parameters = inspect.signature(desktop.run).parameters
        for name in ("skills", "extensions", "mcp", "growth"):
            assert name in parameters, f"run() 少了 {name}：那一层在界面上就是看不见的"

    def test_the_bridge_holds_every_extension_door(self) -> None:
        from jarvis.ui.desktop import HudBridge

        for name in (
            "extensions_state",
            "extensions_search",
            "extensions_install",
            "extensions_add_http",
            "extensions_layer_set",
            "extensions_server_set",
            "extensions_server_remove",
            "extensions_reconnect",
            "skill_set",
            "skill_remove",
        ):
            assert callable(getattr(HudBridge, name, None)), name

    def test_the_build_capabilities_wiring_passes_them_into_the_bundle(self) -> None:
        source = (Path(__file__).resolve().parents[1] / "jarvis" / "__main__.py").read_text(
            encoding="utf-8"
        )
        assert "skills=capabilities.skills" in source
        assert "extensions=capabilities.extensions" in source
        assert "mcp=capabilities.mcp" in source
        assert "skills_index_provider=_skills_index(" in source
        assert source.count("skills_index_provider=_skills_index(") == 2, "命令行那一侧也要接上"
