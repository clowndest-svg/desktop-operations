"""P1-6: she can look up what she said, and change a few things about herself.

Built on the real ``SettingsService``, the real ``TranscriptService`` and the real
``VoicePicker`` -- not stand-ins -- because the three things worth breaking here are the
whitelist, the attribution mark, and whether a refusal actually tells her why. A fake
settings object would let a tool that writes any key pass.

The whitelist is the load-bearing part. The panel can change endpoints, model rows and
API keys; none of those are reachable from a tool, and a test that only checks "the five
allowed keys work" would not catch the one that matters: an eleventh key that doesn't.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import pytest

from jarvis.app.preferences import Preferences
from jarvis.app.settings_service import SettingsService
from jarvis.app.transcript_service import TranscriptService
from jarvis.app.voice_picker import EDGE_VOICES, VoicePicker
from jarvis.config.schema import LlmSection, ToolsSection, TtsSection
from jarvis.database import SqliteStore
from jarvis.tools.builtins import selfconfig_tools
from jarvis.tools.registry import ToolRegistry


def _tools_section() -> ToolsSection:
    return ToolsSection.from_mapping(
        {
            "enabled": True,
            "confirm_dangerous": True,
            "allow_write": False,
            "allow_shell": False,
            "file_roots": [],
            "max_result_chars": 8000,
        }
    )


def _llm_section() -> LlmSection:
    return LlmSection(
        default_provider="a",
        timeout_seconds=30.0,
        max_retries=1,
        retry_backoff_seconds=0.5,
        providers={},
    )


def _tts_section() -> TtsSection:
    return TtsSection(
        enabled=True,
        engine="edge_tts",
        voice="zh-CN-XiaoxiaoNeural",
        speed=1.0,
        volume=1.0,
        device="cpu",
        model="",
    )


@pytest.fixture
def surface(tmp_path: Path) -> Iterator[dict[str, Any]]:
    preferences = Preferences(tmp_path / "prefs.json")

    settings = SettingsService(
        preferences,
        cast(Any, _NoopLlm()),
        _llm_section,
        environ={},
        persist_env=lambda _n, _v: True,
    )
    settings.start()
    store = SqliteStore(tmp_path / "chat.db", journal_mode="MEMORY")
    store.start()
    transcript = TranscriptService(store)
    transcript.start()
    picker = VoicePicker(lambda: _tts_section(), preferences)
    registry = ToolRegistry(_tools_section)
    for spec, handler in selfconfig_tools.build(
        settings=cast(Any, settings),
        voices=cast(Any, picker),
        conversations=cast(Any, transcript),
    ):
        registry.register(spec, handler)
    yield {
        "registry": registry,
        "settings": settings,
        "transcript": transcript,
        "store": store,
        "preferences": preferences,
        "picker": picker,
    }
    store.stop()


class _NoopLlm:
    """``LlmService`` only has to survive being told about an override."""

    def set_section_override(self, section: Any) -> None:
        del section


class TestRegistration:
    def test_no_backing_services_advertises_nothing(self) -> None:
        assert selfconfig_tools.build() == []

    def test_the_cli_gets_conversation_search_but_no_self_configuration(self) -> None:
        """A CLI run has no settings panel and no voice stack. Handing it
        ``settings_apply`` would be a tool that can only ever say "没有这个服务"."""
        names = {
            spec.name
            for spec, _ in selfconfig_tools.build(conversations=cast(Any, _EmptyTranscript()))
        }
        assert names == {"chat_search"}

    def test_all_four_when_the_desktop_wired_them(self, surface: dict[str, Any]) -> None:
        assert set(surface["registry"].names()) == {
            "chat_search",
            "settings_read",
            "settings_apply",
            "voice_pick",
        }

    def test_the_writes_are_caution_and_the_reads_are_safe(self, surface: dict[str, Any]) -> None:
        by_name = {spec.name: spec.risk.value for spec in surface["registry"].specs()}
        assert by_name["settings_apply"] == "caution"
        assert by_name["voice_pick"] == "caution"
        assert by_name["chat_search"] == "safe"
        assert by_name["settings_read"] == "safe"


class _EmptyTranscript:
    def search(self, query: str, *, limit: int = 8) -> list[dict[str, object]]:
        del query, limit
        return []


class TestChatSearch:
    def test_a_sentence_from_an_older_conversation_comes_back_with_its_title(
        self, surface: dict[str, Any]
    ) -> None:
        transcript: TranscriptService = surface["transcript"]
        session = transcript.new_session("部署端口的事")
        transcript.append(session, "user", "数据库端口用 5433 别写错")
        transcript.append(session, "assistant", "记下了，5433")

        result = surface["registry"].invoke("chat_search", {"query": "5433"})
        assert result.ok, result.error
        assert "5433" in result.output and "部署端口的事" in result.output

    def test_a_percent_sign_is_a_search_term_not_a_wildcard(self, surface: dict[str, Any]) -> None:
        """``LIKE`` would read ``a%`` as "anything starting with a" and return the
        greeting as a match for a string nobody typed. ``instr`` keeps the term literal,
        which is what the user actually said."""
        transcript: TranscriptService = surface["transcript"]
        session = transcript.new_session("进度")
        transcript.append(session, "user", "abc def 下载到 100% 了")

        assert transcript.search("a%") == []
        assert len(transcript.search("100%")) == 1

    def test_nothing_found_says_so_rather_than_inventing_a_match(
        self, surface: dict[str, Any]
    ) -> None:
        result = surface["registry"].invoke("chat_search", {"query": "紫水晶"})
        assert result.ok and "没有" in result.output

    def test_an_empty_query_is_refused_before_the_database_is_read(
        self, surface: dict[str, Any]
    ) -> None:
        assert "没有要搜的词" in surface["registry"].invoke("chat_search", {"query": "  "}).output


class TestSettingsWhitelist:
    def test_she_can_move_a_whitelisted_key_and_hears_the_before_and_after(
        self, surface: dict[str, Any]
    ) -> None:
        result = surface["registry"].invoke("settings_apply", {"key": "history_turns", "value": 3})
        assert result.ok, result.error
        assert "10" in result.output and "3" in result.output
        assert surface["settings"].history_turns() == 3

    def test_a_key_outside_the_whitelist_is_refused_without_touching_anything(
        self, surface: dict[str, Any]
    ) -> None:
        """The point of the whole module. ``base_url`` is a real settings key the panel
        writes, and if the check were "does the service accept it" this would succeed."""
        before = surface["settings"].snapshot()["model"]
        result = surface["registry"].invoke(
            "settings_apply", {"key": "base_url", "value": "https://evil.example/v1"}
        )
        assert result.ok and "不在她能改的范围" in result.output
        assert surface["settings"].snapshot()["model"] == before

    @pytest.mark.parametrize(
        "key",
        ["api_key", "provider", "add_model", "remove_model", "default_provider"],
    )
    def test_the_keys_that_would_redirect_or_cost_money_are_all_unreachable(
        self, surface: dict[str, Any], key: str
    ) -> None:
        result = surface["registry"].invoke("settings_apply", {"key": key, "value": "x"})
        assert "不在她能改的范围" in result.output

    def test_a_boolean_key_given_a_number_is_refused_by_type_not_stored_as_truthy(
        self, surface: dict[str, Any]
    ) -> None:
        result = surface["registry"].invoke(
            "settings_apply", {"key": "thinking_enabled", "value": 1}
        )
        assert "要的是 true 或 false" in result.output
        assert surface["settings"].thinking_enabled() is False

    def test_a_value_out_of_range_comes_back_with_the_services_reason(
        self, surface: dict[str, Any]
    ) -> None:
        result = surface["registry"].invoke(
            "settings_apply", {"key": "thinking_budget", "value": 999999}
        )
        assert "没有改成" in result.output and "64" in result.output

    def test_read_reports_the_current_values_including_which_ones_are_hers(
        self, surface: dict[str, Any]
    ) -> None:
        surface["registry"].invoke("settings_apply", {"key": "history_turns", "value": 4})
        output = surface["registry"].invoke("settings_read", {}).output
        assert "history_turns = 4" in output
        assert "ai_edited" in output and "history_turns" in output.split("ai_edited")[1]

    def test_read_never_carries_a_key_value(self, surface: dict[str, Any]) -> None:
        """Only the boolean, and only ever. A tool result goes into a prompt, and a
        prompt goes to a provider."""
        assert "api_key_set" in surface["registry"].invoke("settings_read", {}).output
        assert "api_key_variable" not in surface["registry"].invoke("settings_read", {}).output


class TestTheHumanSeesWhatSheChanged:
    def test_a_setting_she_moved_is_marked_on_the_panel(self, surface: dict[str, Any]) -> None:
        surface["registry"].invoke("settings_apply", {"key": "thinking_enabled", "value": True})
        assert surface["settings"].ai_edited() == ["thinking_enabled"]

    def test_a_setting_the_operator_saves_by_hand_loses_the_mark(
        self, surface: dict[str, Any]
    ) -> None:
        surface["registry"].invoke("settings_apply", {"key": "thinking_enabled", "value": True})
        surface["settings"].apply({"thinking_enabled": False})
        assert surface["settings"].ai_edited() == []

    def test_resending_an_unchanged_field_from_the_panel_does_not_blame_her(
        self, surface: dict[str, Any]
    ) -> None:
        """The panel posts every field on each save. Tagging those would mark half the
        configuration as hers the first time anybody pressed 保存."""
        surface["settings"].apply({"auto_speak_typed": True})
        assert surface["settings"].ai_edited() == []

    def test_a_refused_change_leaves_no_mark_behind(self, surface: dict[str, Any]) -> None:
        surface["registry"].invoke("settings_apply", {"key": "thinking_budget", "value": -5})
        assert surface["settings"].ai_edited() == []

    def test_only_whitelisted_names_can_ever_appear_in_the_mark(
        self, surface: dict[str, Any]
    ) -> None:
        """A stale or hand-edited entry in preferences.json must not make the panel claim
        she rewrote a setting she has no door to."""
        surface["preferences"].set("settings.ai_edited", ["base_url", "history_turns"])
        assert surface["settings"].ai_edited() == ["history_turns"]


class TestVoicePick:
    def test_a_real_voice_takes_effect_and_says_what_it_replaced(
        self, surface: dict[str, Any]
    ) -> None:
        target = EDGE_VOICES[1][0]
        previous = EDGE_VOICES[0][0]
        surface["picker"].pick(previous)
        result = surface["registry"].invoke("voice_pick", {"name": target})
        assert result.ok and target in result.output
        assert surface["picker"].effective_voice() == target

    def test_an_invented_voice_is_refused_with_the_list_it_will_accept(
        self, surface: dict[str, Any]
    ) -> None:
        result = surface["registry"].invoke("voice_pick", {"name": "林志玲"})
        assert "没换成" in result.output
        assert EDGE_VOICES[0][0] in result.output

    def test_a_missing_service_leaves_the_voice_tool_out_entirely(self) -> None:
        assert all(spec.name != "voice_pick" for spec, _ in selfconfig_tools.build(voices=None))
