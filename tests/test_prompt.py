"""Tests for the prompt registry (jarvis.prompt)."""

from __future__ import annotations

import pytest

from jarvis.config.schema import PromptSection
from jarvis.core.exceptions import PromptError
from jarvis.prompt import (
    BUILTIN_TEMPLATES,
    HUD_ASSISTANT,
    PromptRegistry,
    PromptService,
    PromptTemplate,
    default_registry,
    render_prompt,
)


class TestTemplate:
    def test_variables_are_discovered_in_order(self) -> None:
        template = PromptTemplate(name="t", body="你好 {name}，你有 {count} 条消息")
        assert template.variables == ("name", "count")

    def test_no_variables(self) -> None:
        assert PromptTemplate(name="t", body="你好").variables == ()

    def test_escaped_braces_are_not_variables(self) -> None:
        """A prompt that embeds a JSON example must not have its braces read as
        placeholders, or it can never be rendered."""
        template = PromptTemplate(name="t", body='输出 {{"a": 1}} 这样的 JSON')
        assert template.variables == ()
        assert template.render() == '输出 {"a": 1} 这样的 JSON'

    def test_dotted_paths_report_the_root(self) -> None:
        template = PromptTemplate(name="t", body="值：{plan.goal}")
        assert template.variables == ("plan",)

    def test_render_fills_values(self) -> None:
        template = PromptTemplate(name="t", body="目标：{goal}")
        assert template.render({"goal": "备份"}) == "目标：备份"

    def test_missing_variable_is_an_error(self) -> None:
        """Otherwise the model is shown a literal ``{user}``."""
        template = PromptTemplate(name="t", body="你好 {name}")
        with pytest.raises(PromptError, match="缺少变量"):
            template.render({})

    def test_unknown_variable_is_an_error(self) -> None:
        """A caller that believes it is influencing the prompt when it is not is
        the failure mode that makes prompt bugs hard to find."""
        template = PromptTemplate(name="t", body="你好")
        with pytest.raises(PromptError, match="未声明的变量"):
            template.render({"extra": 1})

    def test_empty_name_is_refused(self) -> None:
        with pytest.raises(PromptError, match="名称不能为空"):
            PromptTemplate(name="  ", body="x")

    def test_empty_body_is_refused(self) -> None:
        with pytest.raises(PromptError, match="内容不能为空"):
            PromptTemplate(name="t", body="   ")

    def test_version_must_be_positive(self) -> None:
        with pytest.raises(PromptError, match="版本号"):
            PromptTemplate(name="t", body="x", version=0)

    def test_to_dict_hides_the_body(self) -> None:
        """The listing is shown in the UI; shipping every prompt body over the
        bridge to draw a table row is a waste."""
        payload = PromptTemplate(name="t", body="x" * 100).to_dict()
        assert "body" not in payload
        assert payload["length"] == 100


class TestRegistry:
    def test_register_and_get(self) -> None:
        registry = PromptRegistry()
        registry.register(PromptTemplate(name="a", body="A"))
        assert registry.get("a").body == "A"

    def test_unknown_name_lists_what_exists(self) -> None:
        """A typo in a prompt name should cost one read, not a debugging session."""
        registry = PromptRegistry([PromptTemplate(name="a", body="A")])
        with pytest.raises(PromptError, match="未注册的提示词") as info:
            registry.get("b")
        assert info.value.details["available"] == ["a"]

    def test_register_replaces(self) -> None:
        registry = PromptRegistry([PromptTemplate(name="a", body="A")])
        registry.register(PromptTemplate(name="a", body="B", version=2))
        assert registry.get("a").body == "B"

    def test_find_returns_none_for_unknown(self) -> None:
        assert PromptRegistry().find("nope") is None

    def test_unregister(self) -> None:
        registry = PromptRegistry([PromptTemplate(name="a", body="A")])
        assert registry.unregister("a") is True
        assert registry.unregister("a") is False

    def test_names_are_sorted(self) -> None:
        registry = PromptRegistry(
            [PromptTemplate(name=n, body="x") for n in ("zeta", "alpha", "mid")]
        )
        assert registry.names() == ["alpha", "mid", "zeta"]

    def test_render_by_name(self) -> None:
        registry = PromptRegistry([PromptTemplate(name="a", body="你好 {who}")])
        assert registry.render("a", who="世界") == "你好 世界"

    def test_stats_shape(self) -> None:
        registry = PromptRegistry([PromptTemplate(name="a", body="A")])
        stats = registry.stats()
        assert stats["count"] == 1
        assert isinstance(stats["templates"], list)


class TestBuiltinTemplates:
    def test_names_are_unique(self) -> None:
        names = [template.name for template in BUILTIN_TEMPLATES]
        assert len(names) == len(set(names))

    def test_every_template_renders_with_its_declared_variables(self) -> None:
        """A template that cannot render itself is a template that will raise on
        the voice path, where the failure looks like the assistant going silent."""
        for template in BUILTIN_TEMPLATES:
            values = dict.fromkeys(template.variables, "示例")
            assert template.render(values).strip()

    def test_every_template_has_a_description(self) -> None:
        """The description is the only record of *why* a prompt is worded the way
        it is; without it the next person edits it blind."""
        for template in BUILTIN_TEMPLATES:
            assert template.description.strip(), template.name

    def test_default_registry_holds_every_builtin(self) -> None:
        """Adding a prompt without adding it to BUILTIN_TEMPLATES would make it
        silently unavailable, so it is checked rather than assumed."""
        registry = default_registry()
        for template in BUILTIN_TEMPLATES:
            assert registry.find(template.name) is not None, template.name

    def test_render_prompt_reads_the_default_registry(self) -> None:
        assert render_prompt("agent_conversational") == render_prompt("agent_conversational")

    def test_hud_persona_still_forbids_claiming_unperformed_actions(self) -> None:
        """The panel next to the chat really can delete files; the model must not
        bluff. This is the one line in the persona that is a safety property."""
        assert "不要声称你执行了没有执行的操作" in HUD_ASSISTANT.body


class TestPromptService:
    def _service(self, **overrides: str) -> tuple[PromptService, PromptRegistry]:
        registry = PromptRegistry()
        section = PromptSection.from_mapping({"overrides": overrides})
        return PromptService(lambda: section, registry=registry), registry

    def test_start_registers_every_builtin(self) -> None:
        service, registry = self._service()
        service.start()
        assert len(registry.names()) == len(BUILTIN_TEMPLATES)
        assert service.running is True

    def test_start_is_idempotent(self) -> None:
        service, registry = self._service()
        service.start()
        service.start()
        assert len(registry.names()) == len(BUILTIN_TEMPLATES)

    def test_override_replaces_the_body(self) -> None:
        service, registry = self._service(hud_assistant="你是小夜。只说结论。")
        service.start()
        assert registry.get("hud_assistant").body == "你是小夜。只说结论。"
        assert service.overridden == ("hud_assistant",)

    def test_override_bumps_the_version(self) -> None:
        """The version is what makes "the persona changed" reviewable."""
        service, registry = self._service(hud_assistant="新的措辞")
        service.start()
        assert registry.get("hud_assistant").version == HUD_ASSISTANT.version + 1

    def test_unknown_override_is_skipped_not_fatal(self, caplog: pytest.LogCaptureFixture) -> None:
        """A typo in a config file should leave the assistant working with the
        shipped prompt, not stop it from starting."""
        service, registry = self._service(nope="x")
        with caplog.at_level("WARNING"):
            service.start()
        assert service.running is True
        assert service.overridden == ()
        assert registry.get("hud_assistant").body == HUD_ASSISTANT.body
        assert "nope" in caplog.text

    def test_stop_restores_the_shipped_text(self) -> None:
        """Leaving an override in place after stop() would make a
        restart-without-overrides test pass for the wrong reason."""
        service, registry = self._service(hud_assistant="新的措辞")
        service.start()
        service.stop()
        assert registry.get("hud_assistant").body == HUD_ASSISTANT.body
        assert service.overridden == ()

    def test_stats_reports_overrides(self) -> None:
        service, _ = self._service(hud_assistant="x")
        service.start()
        stats = service.stats()
        assert stats["running"] is True
        assert stats["overridden"] == ["hud_assistant"]

    def test_stats_works_before_start(self) -> None:
        service, _ = self._service()
        assert service.stats()["running"] is False

    def test_render_and_get_delegate_to_the_registry(self) -> None:
        service, _ = self._service()
        service.start()
        assert service.render("agent_conversational").strip()
        assert service.get("agent_conversational").name == "agent_conversational"
        assert "agent_conversational" in service.names()
        assert len(service.templates()) == len(BUILTIN_TEMPLATES)
