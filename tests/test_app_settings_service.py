"""Tests for :class:`jarvis.app.settings_service.SettingsService`.

The interesting assertions here are not the happy path. They are the ones about
what must *not* happen: a key that reaches a file, a snapshot, or a log line; an
invalid endpoint that saves anyway and turns into "the assistant is broken" three
clicks later; an override that is remembered but never applied.
"""

from __future__ import annotations

import logging
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from jarvis.__main__ import _tuning_reader
from jarvis.app.alerts import AlertService
from jarvis.app.chat_service import ConversationTuning
from jarvis.app.preferences import (
    LLM_BASE_URL,
    LLM_HIDDEN,
    LLM_MODEL,
    LLM_OVERRIDES,
    LLM_PROVIDER,
    TELEMETRY_INTERVAL_MS,
    VOICE_AUTO_ARM,
    VOICE_AUTO_SPEAK_TYPED,
    Preferences,
)
from jarvis.app.settings_service import SettingsService
from jarvis.app.wake_keywords import MIN_KEYWORD_CHARS, WakeWords
from jarvis.config.schema import LlmSection, ModelSpec, ProviderSection
from jarvis.core.exceptions import ConfigurationError
from jarvis.llm.service import LlmService

SECRET = "sk-DO-NOT-LEAK-9f2c"


def test_portrait_settings_survive_service_restart(prefs: Preferences, env: dict[str, str]) -> None:
    service, _, _ = _service(prefs, env)
    result = service.apply(
        {
            "portrait_preferences": {
                "character": "p17",
                "gaze": False,
                "bindings": {"p17": "zh-CN-XiaoyiNeural"},
            }
        }
    )
    assert result["error"] == ""
    restarted, _, _ = _service(prefs, env)
    assert restarted.snapshot()["portrait_preferences"]["character"] == "p17"
    assert restarted.snapshot()["portrait_preferences"]["bindings"]["p17"] == "zh-CN-XiaoyiNeural"


def _section(**overrides: Any) -> LlmSection:
    base = {
        "default_provider": "alpha",
        "timeout_seconds": 30.0,
        "max_retries": 1,
        "retry_backoff_seconds": 0.0,
        "providers": {
            "alpha": ProviderSection(
                name="alpha",
                base_url="https://alpha.example/v1",
                models=(ModelSpec(id="alpha-1"),),
                default_model="alpha-1",
                api_key_env="ALPHA_API_KEY",
                cost_input_per_1m=0.0,
                cost_output_per_1m=0.0,
            ),
            "beta": ProviderSection(
                name="beta",
                base_url="https://beta.example/v1",
                models=(ModelSpec(id="beta-1"),),
                default_model="beta-1",
                api_key_env="BETA_API_KEY",
                cost_input_per_1m=0.0,
                cost_output_per_1m=0.0,
            ),
        },
    }
    base.update(overrides)
    return LlmSection(**base)  # type: ignore[arg-type]


class _FakeLlm:
    """Stands in for LlmService: records overrides, says when the cache was cleared."""

    def __init__(self) -> None:
        self.override: LlmSection | None = None
        self.calls = 0

    def set_section_override(self, section: LlmSection | None) -> None:
        self.override = section
        self.calls += 1


class _Recorder:
    def __init__(self, result: bool = True) -> None:
        self.writes: list[tuple[str, str | None]] = []
        self.result = result

    def __call__(self, variable: str, value: str | None) -> bool:
        self.writes.append((variable, value))
        return self.result


@pytest.fixture()
def env() -> dict[str, str]:
    return {}


@pytest.fixture()
def prefs(tmp_path: Path) -> Preferences:
    return Preferences(tmp_path / "preferences.json")


def _service(
    prefs: Preferences,
    env: dict[str, str],
    *,
    section: LlmSection | None = None,
    llm: Any | None = None,
    persist: Any | None = None,
    alerts: Any | None = None,
    wake_words: Any | None = None,
) -> tuple[SettingsService, Any, Any]:
    fake_llm = llm if llm is not None else _FakeLlm()
    recorder = persist if persist is not None else _Recorder()
    service = SettingsService(
        prefs,
        fake_llm,  # type: ignore[arg-type]
        lambda: section or _section(),
        environ=env,
        persist_env=recorder,
        alerts=alerts,
        wake_words=wake_words,
    )
    return service, fake_llm, recorder


class TestSnapshot:
    def test_it_reports_the_variable_name_and_never_the_value(
        self, prefs: Preferences, env: dict[str, str]
    ) -> None:
        env["ALPHA_API_KEY"] = SECRET
        service, _, _ = _service(prefs, env)

        snapshot = service.snapshot()

        assert snapshot["api_key_variable"] == "ALPHA_API_KEY"
        assert snapshot["api_key_set"] is True
        assert SECRET not in repr(snapshot)

    def test_a_missing_key_is_said_so_rather_than_shown_as_blank(
        self, prefs: Preferences, env: dict[str, str]
    ) -> None:
        service, _, _ = _service(prefs, env)

        assert service.snapshot()["api_key_set"] is False

    def test_defaults_are_shown_when_nothing_was_saved(
        self, prefs: Preferences, env: dict[str, str]
    ) -> None:
        service, _, _ = _service(prefs, env)
        snapshot = service.snapshot()

        assert snapshot["provider"] == "alpha"
        assert snapshot["base_url"] == "https://alpha.example/v1"
        assert snapshot["model"] == "alpha-1"
        assert snapshot["overrides_active"] == []

    def test_a_broken_config_still_opens_the_panel(
        self, prefs: Preferences, env: dict[str, str]
    ) -> None:
        """The settings screen is most needed exactly when configuration is bad."""

        def boom() -> LlmSection:
            raise ConfigurationError("llm.providers is empty")

        service = SettingsService(
            prefs, _FakeLlm(), boom, environ=env, persist_env=_Recorder()  # type: ignore[arg-type]
        )
        snapshot = service.snapshot()

        assert snapshot["providers"] == []
        assert snapshot["api_key_set"] is False


class TestValidation:
    def test_an_endpoint_that_is_not_a_url_is_refused_with_a_reason(
        self, prefs: Preferences, env: dict[str, str]
    ) -> None:
        service, _, _ = _service(prefs, env)

        result = service.apply({"base_url": "api.example.com/v1"})

        assert "base_url" in result["problems"]
        assert prefs.get(LLM_BASE_URL) is None

    def test_a_model_name_with_a_space_is_refused(
        self, prefs: Preferences, env: dict[str, str]
    ) -> None:
        service, _, _ = _service(prefs, env)

        assert "model" in service.apply({"model": "gpt 5 turbo"})["problems"]

    def test_the_poll_interval_cannot_be_set_to_a_machine_melting_value(
        self, prefs: Preferences, env: dict[str, str]
    ) -> None:
        service, _, _ = _service(prefs, env)

        assert "telemetry_interval_ms" in service.apply({"telemetry_interval_ms": 5})["problems"]
        assert "telemetry_interval_ms" in service.apply({"telemetry_interval_ms": "x"})["problems"]
        assert service.apply({"telemetry_interval_ms": 2000})["problems"] == {}
        assert prefs.number(TELEMETRY_INTERVAL_MS) == 2000

    def test_an_unknown_field_is_named_not_ignored(
        self, prefs: Preferences, env: dict[str, str]
    ) -> None:
        service, _, _ = _service(prefs, env)

        assert "database_password" in service.apply({"database_password": "x"})["problems"]

    def test_a_valid_save_reports_both_verdicts_together(
        self, prefs: Preferences, env: dict[str, str]
    ) -> None:
        service, _, _ = _service(prefs, env)

        result = service.apply({"model": "alpha-2", "base_url": "nope"})

        assert result["applied"] == {"model": "alpha-2"}
        assert "base_url" in result["problems"]
        assert result["error"], "the screen needs a sentence, not just a dict"


class TestOverrideReachesTheClient:
    def test_saving_an_endpoint_rebuilds_the_section(
        self, prefs: Preferences, env: dict[str, str]
    ) -> None:
        service, fake_llm, _ = _service(prefs, env)

        service.apply({"base_url": "https://private.example/v1"})

        assert fake_llm.override is not None
        provider = fake_llm.override.providers["alpha"]
        assert provider.base_url == "https://private.example/v1"
        assert provider.model == "alpha-1", "one field must not clobber the other"

    def test_a_real_llm_service_hands_out_a_client_built_on_the_new_model(
        self, prefs: Preferences, env: dict[str, str]
    ) -> None:
        """The point of the override is that requests actually change.

        Checked against the real LlmService because the bug this guards against --
        a client cached before the change and reused after it -- lives exactly in
        that interaction, and a fake would agree with any implementation.
        """
        llm = LlmService(lambda: _section())
        llm.start()
        assert llm.client.model == "alpha-1"

        service, _, _ = _service(prefs, env, llm=llm)
        service.apply({"model": "alpha-9"})

        assert llm.client.model == "alpha-9"

    def test_switching_provider_keeps_each_row_s_own_edit(
        self, prefs: Preferences, env: dict[str, str]
    ) -> None:
        """An edit belongs to the row it was typed into, not to "whatever is selected".

        The old behaviour deleted the edit on switch, which is how "I set deepseek's
        address and then looked at kimi and deepseek's address was gone" happens.
        What must still never happen is one row's address landing on another row.
        """
        service, fake_llm, _ = _service(prefs, env)
        service.apply({"target": "alpha", "base_url": "https://private.example/v1"})

        service.apply({"provider": "beta"})

        assert fake_llm.override is not None
        assert fake_llm.override.default_provider == "beta"
        assert fake_llm.override.providers["beta"].base_url == "https://beta.example/v1"
        assert fake_llm.override.providers["beta"].model == "beta-1"
        assert fake_llm.override.providers["alpha"].base_url == "https://private.example/v1"

    def test_clearing_every_override_gives_the_config_tree_back(
        self, prefs: Preferences, env: dict[str, str]
    ) -> None:
        service, fake_llm, _ = _service(prefs, env)
        service.apply({"target": "alpha", "model": "alpha-9"})
        assert fake_llm.override is not None

        prefs.forget(LLM_OVERRIDES)
        prefs.forget(LLM_PROVIDER)
        service.start()

        assert fake_llm.override is None


class TestApiKeyStorage:
    def test_the_key_goes_to_the_environment_and_the_registry_only(
        self, prefs: Preferences, env: dict[str, str], tmp_path: Path
    ) -> None:
        recorder = _Recorder()
        service, _, _ = _service(prefs, env, persist=recorder)

        result = service.apply({"api_key": SECRET})

        assert env["ALPHA_API_KEY"] == SECRET
        assert recorder.writes == [("ALPHA_API_KEY", SECRET)]
        assert result["applied"]["api_key"] == "set"
        assert SECRET not in repr(result)
        # The whole point: nothing on disk knows about it.
        saved = tmp_path / "preferences.json"
        on_disk = saved.read_text(encoding="utf-8") if saved.exists() else ""
        assert SECRET not in on_disk

    def test_a_registry_failure_says_so_instead_of_claiming_persistence(
        self, prefs: Preferences, env: dict[str, str]
    ) -> None:
        service, _, _ = _service(prefs, env, persist=_Recorder(result=False))

        result = service.apply({"api_key": SECRET})

        assert result["applied"]["api_key"] == "set_for_this_run_only"
        assert env["ALPHA_API_KEY"] == SECRET, "this run still works; only the reboot is lost"

    def test_an_empty_value_clears_it_everywhere(
        self, prefs: Preferences, env: dict[str, str]
    ) -> None:
        env["ALPHA_API_KEY"] = SECRET
        service, _, recorder = _service(prefs, env)

        result = service.apply({"api_key": "   "})

        assert "ALPHA_API_KEY" not in env
        assert recorder.writes == [("ALPHA_API_KEY", None)]
        assert result["applied"]["api_key"] == "cleared"

    def test_nothing_about_the_key_is_logged(
        self, prefs: Preferences, env: dict[str, str], caplog: pytest.LogCaptureFixture
    ) -> None:
        service, _, _ = _service(prefs, env)

        with caplog.at_level(logging.DEBUG):
            service.apply({"api_key": SECRET})

        assert SECRET not in caplog.text
        assert "ALPHA_API_KEY" in caplog.text, "the name is useful; the value never is"


class TestBootsFromSavedChoices:
    def test_start_reapplies_what_was_saved_last_time(
        self, prefs: Preferences, env: dict[str, str]
    ) -> None:
        prefs.set(LLM_BASE_URL, "https://saved.example/v1")
        prefs.set(LLM_PROVIDER, "beta")
        service, fake_llm, _ = _service(prefs, env)

        service.start()

        assert fake_llm.override is not None
        assert fake_llm.override.default_provider == "beta"
        assert fake_llm.override.providers["beta"].base_url == "https://saved.example/v1"

    def test_the_microphone_consent_still_round_trips(
        self, prefs: Preferences, env: dict[str, str]
    ) -> None:
        """The settings file is shared with the voice consent; neither may eat the other."""
        service, _, _ = _service(prefs, env)
        prefs.set_flag(VOICE_AUTO_ARM, True)
        service.apply({"auto_speak_typed": False})

        assert prefs.flag(VOICE_AUTO_ARM) is True
        assert prefs.flag(VOICE_AUTO_SPEAK_TYPED, default=True) is False


class TestReadAloudQuestion:
    """The bridge asks this once per typed answer, and the answer must be current."""

    def test_a_fresh_install_reads_typed_answers_aloud(
        self, prefs: Preferences, env: dict[str, str]
    ) -> None:
        service, _, _ = _service(prefs, env)

        assert service.speaks_typed() is True

    def test_turning_it_off_is_visible_on_the_next_question(
        self, prefs: Preferences, env: dict[str, str]
    ) -> None:
        service, _, _ = _service(prefs, env)
        service.apply({"auto_speak_typed": False})

        assert service.speaks_typed() is False


class TestThinkingLoader:
    """The animation next to 「思考中」 -- and the refusal that keeps a typo from "saving"."""

    def test_a_fresh_install_gets_the_dots(self, prefs: Preferences, env: dict[str, str]) -> None:
        service, _, _ = _service(prefs, env)

        assert service.thinking_loader() == "dots"
        assert service.snapshot()["thinking_loader_choices"] == ["dots", "matrix", "ring", "bars"]

    def test_a_choice_round_trips_into_the_snapshot(
        self, prefs: Preferences, env: dict[str, str]
    ) -> None:
        service, _, _ = _service(prefs, env)

        problems = service.apply({"thinking_loader": "ring"})["problems"]

        assert problems == {}, problems
        assert service.thinking_loader() == "ring"
        assert service.snapshot()["thinking_loader"] == "ring"

    def test_a_kind_the_pages_cannot_draw_is_refused_not_stored(
        self, prefs: Preferences, env: dict[str, str]
    ) -> None:
        """A write that reports success while both surfaces keep the default is the bug."""
        service, _, _ = _service(prefs, env)

        answer = service.apply({"thinking_loader": "confetti"})

        assert "confetti" in answer["problems"]["thinking_loader"], answer
        assert service.thinking_loader() == "dots", "被拒的值不许留下半个"


class TestModelChoices:
    """The chat's model picker, which is the settings provider field by another name."""

    def test_every_configured_model_is_listed_with_the_active_one_marked(
        self, prefs: Preferences, env: dict[str, str]
    ) -> None:
        service, _, _ = _service(prefs, env)

        listing = service.model_choices()

        assert sorted(entry["name"] for entry in listing["providers"]) == sorted(
            service.snapshot()["providers"]
        )
        assert next(e for e in listing["providers"] if e["current"])["name"] == listing["provider"]

    def test_a_model_without_a_key_is_still_offered_but_flagged(
        self, prefs: Preferences, env: dict[str, str]
    ) -> None:
        """Hiding it turns "where did my other model go" into the bug report; the
        real answer is one environment variable the operator can set themselves."""
        service, _, _ = _service(prefs, env)

        entry = service.model_choices()["providers"][0]

        assert entry["key_set"] is False
        assert entry["key_variable"], "the name is what the user needs, never the value"
        assert "api_key" not in str(entry)

    def test_choosing_a_model_writes_the_same_preference_the_panel_writes(
        self, prefs: Preferences, env: dict[str, str]
    ) -> None:
        service, _, _ = _service(prefs, env)
        other = next(
            entry["name"] for entry in service.model_choices()["providers"] if not entry["current"]
        )

        listing = service.choose_model(other)

        assert listing["error"] == ""
        assert listing["provider"] == other
        assert prefs.text(LLM_PROVIDER) == other
        assert (
            next(e for e in service.model_choices()["providers"] if e["current"])["name"] == other
        )

    def test_an_unknown_model_is_refused_loudly_and_changes_nothing(
        self, prefs: Preferences, env: dict[str, str]
    ) -> None:
        service, _, _ = _service(prefs, env)
        before = service.model_choices()["provider"]

        listing = service.choose_model("gpt-nope")

        assert listing["error"], "a picker that silently keeps the old model is a lie"
        assert service.model_choices()["provider"] == before

    def test_a_blank_choice_is_refused(self, prefs: Preferences, env: dict[str, str]) -> None:
        service, _, _ = _service(prefs, env)

        assert service.choose_model("  ")["error"]


class TestModelList:
    """The settings panel's model list: rows from config plus rows from the window.

    The defect this class exists for: editing one row's address and then looking at
    another row used to *delete* the edit, because the override was global to the
    selected provider. Rows are per-provider now, and a window-added row is a first
    class citizen of the same list the chat dropdown reads.
    """

    def test_an_edited_row_keeps_its_edit_when_another_row_is_selected(
        self, prefs: Preferences, env: dict[str, str]
    ) -> None:
        service, _, _ = _service(prefs, env)

        service.apply({"target": "alpha", "base_url": "https://edited.example/v1"})
        service.apply({"provider": "beta"})

        section = service.model_choices()
        alpha = next(entry for entry in section["providers"] if entry["name"] == "alpha")
        assert alpha["default_model"] == "alpha-1"
        snapshot = service.snapshot()
        row = next(entry for entry in snapshot["models"] if entry["name"] == "alpha")
        assert row["base_url"] == "https://edited.example/v1"
        assert row["edited"] is True

    def test_a_window_added_model_appears_in_the_chat_dropdown(
        self, prefs: Preferences, env: dict[str, str]
    ) -> None:
        service, fake_llm, _ = _service(prefs, env)

        outcome = service.apply(
            {
                "add_model": {
                    "name": "gamma",
                    "base_url": "https://gamma.example/v1",
                    "model": "gamma-2",
                }
            }
        )

        assert outcome["problems"] == {}
        names = [entry["name"] for entry in service.model_choices()["providers"]]
        assert "gamma" in names
        row = next(entry for entry in outcome["models"] if entry["name"] == "gamma")
        assert row["source"] == "界面添加"
        assert row["key_env"] == "GAMMA_API_KEY"
        assert fake_llm.override is not None
        assert fake_llm.override.providers["gamma"].base_url == "https://gamma.example/v1"

    def test_the_added_model_can_be_chosen_and_answered_by_name(
        self, prefs: Preferences, env: dict[str, str]
    ) -> None:
        service, _, _ = _service(prefs, env)
        service.apply(
            {"add_model": {"name": "gamma", "base_url": "https://g.example/v1", "model": "g-1"}}
        )

        listing = service.choose_model("gamma")

        assert listing["error"] == ""
        assert listing["provider"] == "gamma"
        assert listing["model"] == "g-1"

    def test_a_config_row_cannot_be_deleted_from_the_window(
        self, prefs: Preferences, env: dict[str, str]
    ) -> None:
        service, _, _ = _service(prefs, env)

        outcome = service.apply({"remove_model": "alpha"})

        assert outcome["problems"]["remove_model"]
        assert "alpha" in [entry["name"] for entry in service.snapshot()["models"]]

    def test_a_window_row_can_be_deleted_and_its_edits_go_with_it(
        self, prefs: Preferences, env: dict[str, str]
    ) -> None:
        service, _, _ = _service(prefs, env)
        service.apply(
            {"add_model": {"name": "gamma", "base_url": "https://g.example/v1", "model": "g-1"}}
        )

        service.apply({"remove_model": "gamma"})

        assert "gamma" not in [entry["name"] for entry in service.snapshot()["models"]]

    def test_a_duplicate_name_is_refused_rather_than_shadowing_config(
        self, prefs: Preferences, env: dict[str, str]
    ) -> None:
        service, _, _ = _service(prefs, env)

        outcome = service.apply(
            {"add_model": {"name": "alpha", "base_url": "https://x.example/v1", "model": "x"}}
        )

        assert outcome["problems"]["add_model"]

    def test_a_second_window_row_survives_the_first_one(
        self, prefs: Preferences, env: dict[str, str]
    ) -> None:
        """The row already stored must not poison the write of the next one.

        ``_extras`` used to hand ``ModelSpec`` objects back, and ``Preferences.set``
        refuses a value it cannot serialise *before* touching anything -- so the second
        add of a session was a total no-op while the panel reported 「已保存并立即生效」.
        Every existing test added exactly one row, so none of them ever saw it.
        """
        service, _, _ = _service(prefs, env)
        service.apply(
            {"add_model": {"name": "gamma", "base_url": "https://g.example/v1", "model": "g-1"}}
        )

        outcome = service.apply(
            {"add_model": {"name": "delta", "base_url": "https://d.example/v1", "model": "d-1"}}
        )

        assert outcome["problems"] == {}
        assert {"gamma", "delta"} <= {entry["name"] for entry in outcome["models"]}
        # And it is on disk, not just in this process: "I had to reopen 小夜 to see it"
        # is only fixed if a fresh reader finds both rows.
        restarted, _, _ = _service(Preferences(prefs.path), env)
        assert {"gamma", "delta"} <= {entry["name"] for entry in restarted.snapshot()["models"]}

    def test_several_providers_add_in_one_save(
        self, prefs: Preferences, env: dict[str, str]
    ) -> None:
        """The queue the panel builds: three rows, one write, all of them live."""
        service, fake_llm, _ = _service(prefs, env)

        outcome = service.apply(
            {
                "add_model": [
                    {"name": "one", "base_url": "https://1.example/v1", "model": "m-1"},
                    {"name": "two", "base_url": "https://2.example/v1", "model": "m-2"},
                    {"name": "three", "base_url": "https://3.example/v1", "model": "m-3"},
                ]
            }
        )

        assert outcome["problems"] == {}
        names = {entry["name"] for entry in outcome["models"]}
        assert {"one", "two", "three"} <= names
        assert fake_llm.override is not None
        assert {"one", "two", "three"} <= set(fake_llm.override.providers)

    def test_a_duplicate_inside_the_batch_writes_nothing(
        self, prefs: Preferences, env: dict[str, str]
    ) -> None:
        """All or nothing: two stored and one lost under one 「已保存」 is the worst shape."""
        service, _, _ = _service(prefs, env)

        outcome = service.apply(
            {
                "add_model": [
                    {"name": "one", "base_url": "https://1.example/v1", "model": "m-1"},
                    {"name": "one", "base_url": "https://2.example/v1", "model": "m-2"},
                ]
            }
        )

        assert outcome["problems"]["add_model"]
        assert "one" not in {entry["name"] for entry in service.snapshot()["models"]}

    def test_a_refused_write_is_said_rather_than_reported_as_saved(
        self,
        prefs: Preferences,
        env: dict[str, str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The verdict of the write is the only thing standing between this line and a lie."""
        service, _, _ = _service(prefs, env)
        monkeypatch.setattr(prefs, "set", lambda *args, **kwargs: False)

        outcome = service.apply(
            {"add_model": {"name": "gamma", "base_url": "https://g.example/v1", "model": "g-1"}}
        )

        assert "add_model" in outcome["problems"]
        assert "add_model" not in outcome["applied"]
        assert "gamma" not in {entry["name"] for entry in service.snapshot()["models"]}

    def test_the_key_of_an_added_provider_reaches_its_own_variable(
        self, prefs: Preferences, env: dict[str, str]
    ) -> None:
        service, _, _ = _service(prefs, env)

        service.apply(
            {
                "add_model": {
                    "name": "gamma",
                    "base_url": "https://g.example/v1",
                    "model": "g-1",
                    "api_key": SECRET,
                }
            }
        )

        assert env["GAMMA_API_KEY"] == SECRET
        row = next(entry for entry in service.snapshot()["models"] if entry["name"] == "gamma")
        assert row["key_set"] is True

    def test_a_refused_delete_is_said_rather_than_reported_as_saved(
        self,
        prefs: Preferences,
        env: dict[str, str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        service, _, _ = _service(prefs, env)
        service.apply(
            {"add_model": {"name": "gamma", "base_url": "https://g.example/v1", "model": "g-1"}}
        )
        monkeypatch.setattr(prefs, "set", lambda *args, **kwargs: False)

        outcome = service.apply({"remove_model": "gamma"})

        assert "remove_model" in outcome["problems"]
        assert "gamma" in {entry["name"] for entry in service.snapshot()["models"]}

    def test_a_key_that_found_no_variable_says_so(
        self,
        prefs: Preferences,
        env: dict[str, str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The row went in; the Key did not. Silence here is a lost credential."""
        service, _, _ = _service(prefs, env)
        monkeypatch.setattr(service, "_store_api_key", lambda value, target=None: None)

        outcome = service.apply(
            {
                "add_model": {
                    "name": "gamma",
                    "base_url": "https://g.example/v1",
                    "model": "g-1",
                    "api_key": SECRET,
                }
            }
        )

        assert "gamma" in {entry["name"] for entry in outcome["models"]}
        assert "Key" in outcome["problems"]["add_model"]

    def test_a_local_endpoint_can_be_added_without_any_key(
        self, prefs: Preferences, env: dict[str, str]
    ) -> None:
        """Ollama / LM Studio on localhost have no credential to type.

        The panel already labelled the field 「可选」 while the client raised
        ``LlmAuthError`` before opening a socket, and the connectivity gate then rolled
        the row back with 「连不上，没保存」 -- so a local model could not be added at all.
        """
        service, fake_llm, _ = _service(prefs, env)

        outcome = service.apply(
            {
                "add_model": {
                    "name": "ollama",
                    "base_url": "http://localhost:11434/v1",
                    "model": "qwen2.5",
                    "key_optional": True,
                }
            }
        )

        assert outcome["problems"] == {}
        row = next(entry for entry in outcome["models"] if entry["name"] == "ollama")
        assert row["key_optional"] is True
        assert row["key_set"] is False, "没配钥匙就该照实说没配，而不是假装配好了"
        assert fake_llm.override is not None
        assert fake_llm.override.providers["ollama"].key_optional is True

    def test_a_row_without_the_flag_still_demands_a_key(
        self, prefs: Preferences, env: dict[str, str]
    ) -> None:
        """Unlocking local servers must not unlock the cloud row next to it."""
        service, _, _ = _service(prefs, env)

        outcome = service.apply(
            {"add_model": {"name": "cloudai", "base_url": "https://c.example/v1", "model": "m"}}
        )

        row = next(entry for entry in outcome["models"] if entry["name"] == "cloudai")
        assert row["key_optional"] is False

    def test_the_keyless_choice_survives_a_restart(
        self, prefs: Preferences, env: dict[str, str]
    ) -> None:
        """It is stored in the row, not remembered only in this process's memory."""
        service, _, _ = _service(prefs, env)
        service.apply(
            {
                "add_model": {
                    "name": "ollama",
                    "base_url": "http://localhost:11434/v1",
                    "model": "qwen2.5",
                    "key_optional": True,
                }
            }
        )

        restarted, _, _ = _service(Preferences(prefs.path), env)

        row = next(entry for entry in restarted.snapshot()["models"] if entry["name"] == "ollama")
        assert row["key_optional"] is True

    def test_removing_one_of_several_rows_persists(
        self, prefs: Preferences, env: dict[str, str]
    ) -> None:
        """The sibling of the add bug: deleting one row rewrites the whole list."""
        service, _, _ = _service(prefs, env)
        service.apply(
            {
                "add_model": [
                    {"name": "one", "base_url": "https://1.example/v1", "model": "m-1"},
                    {"name": "two", "base_url": "https://2.example/v1", "model": "m-2"},
                ]
            }
        )

        outcome = service.apply({"remove_model": "one"})

        assert outcome["problems"] == {}
        names = {entry["name"] for entry in outcome["models"]}
        assert names == {"alpha", "beta", "two"}
        restarted, _, _ = _service(Preferences(prefs.path), env)
        assert {entry["name"] for entry in restarted.snapshot()["models"]} == {
            "alpha",
            "beta",
            "two",
        }

    def test_the_old_global_overrides_fold_into_their_provider_on_start(
        self, prefs: Preferences, env: dict[str, str]
    ) -> None:
        from jarvis.app.preferences import LLM_BASE_URL, LLM_PROVIDER

        prefs.set(LLM_PROVIDER, "beta")
        prefs.set(LLM_BASE_URL, "https://legacy.example/v1")
        prefs.set(LLM_MODEL, "legacy-9")
        service, _, _ = _service(prefs, env)

        service.start()

        row = next(entry for entry in service.snapshot()["models"] if entry["name"] == "beta")
        assert row["base_url"] == "https://legacy.example/v1"
        assert row["model"] == "legacy-9"
        assert prefs.text(LLM_BASE_URL) == ""
        # ``llm.model`` is the model the chat is *currently using* as much as it is a
        # legacy row, so the migration must not eat it: forgetting it here made every
        # start reset the operator's selection.
        assert prefs.text(LLM_MODEL) == "legacy-9"

    def test_the_migration_only_runs_on_the_key_nothing_else_writes(
        self, prefs: Preferences, env: dict[str, str]
    ) -> None:
        """没有 llm.base_url 就别再动覆盖表 —— 它以前每次启动都动一遍。"""
        from jarvis.app.preferences import LLM_OVERRIDES

        prefs.set(LLM_MODEL, "alpha-2")
        prefs.set(LLM_OVERRIDES, {"alpha": {"models": [{"id": "alpha-1"}, {"id": "alpha-2"}]}})
        service, _, _ = _service(prefs, env)

        service.start()

        stored = prefs.get(LLM_OVERRIDES)
        assert stored == {"alpha": {"models": [{"id": "alpha-1"}, {"id": "alpha-2"}]}}
        assert "model" not in stored["alpha"], "当前选中的模型不该被写成一行覆盖"

    def test_saving_rebuilds_the_client_without_a_restart(
        self, prefs: Preferences, env: dict[str, str]
    ) -> None:
        service, fake_llm, _ = _service(prefs, env)
        before = fake_llm.calls

        service.apply({"target": "alpha", "model": "alpha-2"})

        assert fake_llm.calls > before
        assert fake_llm.override.providers["alpha"].model == "alpha-2"


class TestAddedModelsSurviveARestart:
    """用户报的那条：加进去的模型，重开软件就没了。

    盘上其实一直留着四条 —— 消失的是**读**的那一头。旧写法把校验过的行（里面的
    ``models`` 是 ``ModelSpec`` 对象）整个塞回 preferences，那一写必然抛，而抛异常被
    咽掉、内存里却留下了这坨写不出去的东西；之后每次读都拿到一个 list 校验过不去的
    tuple，于是"这一家有哪些模型"退化成"当前选中的那一个"。所以这里钉三件事：
    覆盖表必须能序列化、重启后模型清单原样还在、启动不许吃掉当前选择。
    """

    @staticmethod
    def _restart(tmp_path: Path, env: dict[str, str]) -> SettingsService:
        """A fresh process: new Preferences and a new service over the same file."""
        service, _, _ = _service(Preferences(tmp_path / "preferences.json"), env)
        service.start()
        return service

    def test_every_model_added_from_the_window_is_still_there_after_a_restart(
        self, prefs: Preferences, env: dict[str, str], tmp_path: Path
    ) -> None:
        service, _, _ = _service(prefs, env)
        service.start()
        for model_id in ("alpha-2", "alpha-3", "glm-5.3"):
            answer = service.add_model("alpha", model_id, "")
            assert answer["ok"], answer

        row = next(
            entry
            for entry in self._restart(tmp_path, env).snapshot()["models"]
            if entry["name"] == "alpha"
        )

        assert [entry["id"] for entry in row["models"]] == [
            "alpha-1",
            "alpha-2",
            "alpha-3",
            "glm-5.3",
        ]

    def test_editing_the_address_does_not_eat_the_model_list(
        self, prefs: Preferences, env: dict[str, str], tmp_path: Path
    ) -> None:
        """Every writer here is "读出整张表、改一格、写回整张表" —— 表里只要有一样东西
        序列化不了，那一写就连同它路过的每一行一起失败。"""
        service, _, _ = _service(prefs, env)
        service.start()
        service.add_model("alpha", "alpha-2", "第二个")

        service.apply({"target": "alpha", "base_url": "https://alpha2.example/v1"})

        row = next(
            entry
            for entry in self._restart(tmp_path, env).snapshot()["models"]
            if entry["name"] == "alpha"
        )
        assert row["base_url"] == "https://alpha2.example/v1"
        assert [entry["id"] for entry in row["models"]] == ["alpha-1", "alpha-2"]

    def test_a_failed_write_does_not_leave_the_reader_a_different_answer(
        self, prefs: Preferences, env: dict[str, str], tmp_path: Path
    ) -> None:
        """写不进文件的东西不该留在内存里冒充已经保存了。"""
        from jarvis.config.schema import ModelSpec

        assert prefs.set(LLM_OVERRIDES, {"alpha": {"models": (ModelSpec(id="x"),)}}) is False
        assert prefs.get(LLM_OVERRIDES) is None
        assert LLM_OVERRIDES not in prefs.as_dict()

    def test_the_current_model_survives_a_start_with_no_legacy_row(
        self, prefs: Preferences, env: dict[str, str], tmp_path: Path
    ) -> None:
        prefs.set(LLM_PROVIDER, "alpha")
        service, _, _ = _service(prefs, env)
        service.choose_model("alpha", "alpha-1")

        restarted = self._restart(tmp_path, env)

        assert restarted.selected_pair() == ("alpha", "alpha-1")


class TestOneNewProviderCanCarrySeveralModels:
    """用户报的第二条：添加那里只能填一个模型，可「看看它有哪些模型」拉回来的是好几个。

    旧表单逼人一屏一次存一家，然后回到那一行里逐个再加 —— 三个模型就是三轮。
    这里钉的是：清单能一次存进去、第一个当起始、重启还在、重名折叠、坏名字说得出是哪一个。
    """

    @staticmethod
    def _restart(tmp_path: Path, env: dict[str, str]) -> SettingsService:
        service, _, _ = _service(Preferences(tmp_path / "preferences.json"), env)
        service.start()
        return service

    def test_a_draft_carrying_a_list_stores_every_model_in_it(
        self, prefs: Preferences, env: dict[str, str]
    ) -> None:
        service, fake_llm, _ = _service(prefs, env)

        outcome = service.apply(
            {
                "add_model": {
                    "name": "family",
                    "base_url": "https://f.example/v1",
                    "models": ["f-max", {"id": "f-mini", "label": "F Mini"}],
                }
            }
        )

        assert outcome["problems"] == {}
        row = next(item for item in outcome["models"] if item["name"] == "family")
        assert [entry["id"] for entry in row["models"]] == ["f-max", "f-mini"]
        assert row["default_model"] == "f-max"
        assert fake_llm.override is not None
        assert [spec.id for spec in fake_llm.override.providers["family"].models] == [
            "f-max",
            "f-mini",
        ]

    def test_those_models_are_still_there_after_a_restart(
        self, prefs: Preferences, env: dict[str, str], tmp_path: Path
    ) -> None:
        """写进去的是清单，读回来的也只能是清单 —— 存储形状一错，重启就退化成"当前那一个"。"""
        service, _, _ = _service(prefs, env)
        service.apply(
            {
                "add_model": {
                    "name": "family",
                    "base_url": "https://f.example/v1",
                    "models": ["f-max", "f-mini"],
                }
            }
        )

        row = next(
            item
            for item in self._restart(tmp_path, env).snapshot()["models"]
            if item["name"] == "family"
        )

        assert [entry["id"] for entry in row["models"]] == ["f-max", "f-mini"]
        assert row["default_model"] == "f-max"

    def test_the_same_name_clicked_twice_stores_it_once(
        self, prefs: Preferences, env: dict[str, str]
    ) -> None:
        """清单是点出来的，点两下是手滑，不是"我要存两份"。"""
        service, _, _ = _service(prefs, env)

        outcome = service.apply(
            {
                "add_model": {
                    "name": "family",
                    "base_url": "https://f.example/v1",
                    "models": ["f-max", "f-max"],
                }
            }
        )

        row = next(item for item in outcome["models"] if item["name"] == "family")
        assert [entry["id"] for entry in row["models"]] == ["f-max"]

    def test_a_bad_name_in_the_list_says_which_one(
        self, prefs: Preferences, env: dict[str, str]
    ) -> None:
        service, _, _ = _service(prefs, env)

        outcome = service.apply(
            {
                "add_model": {
                    "name": "family",
                    "base_url": "https://f.example/v1",
                    "models": ["f-max", "bad name"],
                }
            }
        )

        why = outcome["problems"]["add_model"]
        assert "bad name" in why
        assert "family" not in {entry["name"] for entry in service.snapshot()["models"]}

    def test_an_empty_list_is_refused_instead_of_storing_a_silent_row(
        self, prefs: Preferences, env: dict[str, str]
    ) -> None:
        service, _, _ = _service(prefs, env)

        outcome = service.apply(
            {"add_model": {"name": "family", "base_url": "https://f.example/v1", "models": []}}
        )

        assert "至少写一个模型名" in outcome["problems"]["add_model"]

    def test_the_older_single_model_draft_still_adds(
        self, prefs: Preferences, env: dict[str, str]
    ) -> None:
        """手机、CLI 和上一版页面发的都是这一种；新字段不能把它挤掉。"""
        service, _, _ = _service(prefs, env)

        outcome = service.apply(
            {"add_model": {"name": "solo", "base_url": "https://s.example/v1", "model": "s-1"}}
        )

        row = next(item for item in outcome["models"] if item["name"] == "solo")
        assert [entry["id"] for entry in row["models"]] == ["s-1"]
        assert row["default_model"] == "s-1"


class TestProviderLevelChoices:
    """The chat header's two dropdowns: a provider, then one of its models."""

    def test_every_provider_carries_its_own_model_list_and_default(
        self, prefs: Preferences, env: dict[str, str]
    ) -> None:
        """The shape is the point: a key belongs to a provider, a provider has models.

        A flat list would repeat the vendor on every row and still leave the page to
        group them, which is what the old single dropdown did.
        """
        service, _, _ = _service(prefs, env)

        listing = service.model_choices()

        alpha = next(entry for entry in listing["providers"] if entry["name"] == "alpha")
        assert [spec["id"] for spec in alpha["models"]] == ["alpha-1"]
        assert alpha["default_model"] == "alpha-1"
        assert "choices" not in listing, "the old flat list must not come back"

    def test_a_label_falls_back_to_the_id_so_the_page_never_shows_a_blank(
        self, prefs: Preferences, env: dict[str, str]
    ) -> None:
        section = _section(
            providers={
                "alpha": ProviderSection(
                    name="alpha",
                    base_url="https://alpha.example/v1",
                    models=(ModelSpec(id="alpha-1"),),
                    default_model="alpha-1",
                    api_key_env="ALPHA_API_KEY",
                    cost_input_per_1m=0.0,
                    cost_output_per_1m=0.0,
                )
            }
        )
        service, _, _ = _service(prefs, env, section=section)

        spec = service.model_choices()["providers"][0]["models"][0]

        assert spec == {"id": "alpha-1", "label": "alpha-1"}

    def test_choosing_a_model_inside_a_provider_lands_on_that_model(
        self, prefs: Preferences, env: dict[str, str]
    ) -> None:
        section = _section(
            providers={
                "alpha": ProviderSection(
                    name="alpha",
                    base_url="https://alpha.example/v1",
                    models=(ModelSpec(id="alpha-1"), ModelSpec(id="alpha-small")),
                    default_model="alpha-1",
                    api_key_env="ALPHA_API_KEY",
                    cost_input_per_1m=0.0,
                    cost_output_per_1m=0.0,
                ),
                "beta": ProviderSection(
                    name="beta",
                    base_url="https://beta.example/v1",
                    models=(ModelSpec(id="beta-1"),),
                    default_model="beta-1",
                    api_key_env="BETA_API_KEY",
                    cost_input_per_1m=0.0,
                    cost_output_per_1m=0.0,
                ),
            }
        )
        service, _, _ = _service(prefs, env, section=section)

        listing = service.choose_model("alpha", "alpha-small")

        assert listing["error"] == ""
        assert listing["provider"] == "alpha"
        assert listing["model"] == "alpha-small"
        assert prefs.text(LLM_MODEL) == "alpha-small"

    def test_switching_provider_without_a_model_lands_on_that_providers_default(
        self, prefs: Preferences, env: dict[str, str]
    ) -> None:
        """A half-complete selection has to land somewhere, and somewhere is the
        provider's own default rather than a model that does not exist there."""
        section = _section(
            providers={
                "alpha": ProviderSection(
                    name="alpha",
                    base_url="https://alpha.example/v1",
                    models=(ModelSpec(id="alpha-1"),),
                    default_model="alpha-1",
                    api_key_env="ALPHA_API_KEY",
                    cost_input_per_1m=0.0,
                    cost_output_per_1m=0.0,
                ),
                "beta": ProviderSection(
                    name="beta",
                    base_url="https://beta.example/v1",
                    models=(ModelSpec(id="beta-9"), ModelSpec(id="beta-small")),
                    default_model="beta-9",
                    api_key_env="BETA_API_KEY",
                    cost_input_per_1m=0.0,
                    cost_output_per_1m=0.0,
                ),
            }
        )
        service, _, _ = _service(prefs, env, section=section)

        listing = service.choose_model("beta")

        assert listing["model"] == "beta-9"

    def test_an_unknown_model_falls_back_rather_than_raising(
        self, prefs: Preferences, env: dict[str, str]
    ) -> None:
        """A stale model name is reachable from a hand-edited file or an old session;
        the picker must land on something usable instead of blanking the header."""
        service, _, _ = _service(prefs, env)

        listing = service.choose_model("alpha", "gone-in-a-refactor")

        assert listing["error"] == ""
        assert listing["model"] == "alpha-1"


class TestModelTuning:
    """The two per-model knobs: thinking level and context size.

    Keyed by the provider/model pair, because "how hard should this model think" is
    only meaningful next to the model it is about. Remembering one global pair is the
    defect this class exists for.
    """

    def test_a_level_maps_to_a_real_token_budget(
        self, prefs: Preferences, env: dict[str, str]
    ) -> None:
        """A level is a readable shortcut to a number, not a second incompatible scale."""
        service, _, _ = _service(prefs, env)

        outcome = service.set_tuning(thinking="high")

        assert outcome["ok"] is True
        assert service.thinking_enabled() is True
        assert service.thinking_budget() == 16_000

    def test_off_sends_no_thinking_parameter_at_all(
        self, prefs: Preferences, env: dict[str, str]
    ) -> None:
        service, _, _ = _service(prefs, env)

        service.set_tuning(thinking="off")

        assert service.thinking_enabled() is False

    def test_the_pair_is_the_identity_so_each_model_keeps_its_own_level(
        self, prefs: Preferences, env: dict[str, str]
    ) -> None:
        section = _section(
            providers={
                "alpha": ProviderSection(
                    name="alpha",
                    base_url="https://alpha.example/v1",
                    models=(ModelSpec(id="alpha-1"), ModelSpec(id="alpha-small")),
                    default_model="alpha-1",
                    api_key_env="ALPHA_API_KEY",
                    cost_input_per_1m=0.0,
                    cost_output_per_1m=0.0,
                ),
                "beta": ProviderSection(
                    name="beta",
                    base_url="https://beta.example/v1",
                    models=(ModelSpec(id="beta-1"),),
                    default_model="beta-1",
                    api_key_env="BETA_API_KEY",
                    cost_input_per_1m=0.0,
                    cost_output_per_1m=0.0,
                ),
            }
        )
        service, _, _ = _service(prefs, env, section=section)

        service.choose_model("alpha", "alpha-1")
        service.set_tuning(thinking="high", turns=20)
        service.choose_model("beta")
        service.set_tuning(thinking="low", turns=4)
        service.choose_model("alpha", "alpha-1")

        assert _knobs(service, "alpha", "alpha-1") == {
            "thinking": "high",
            "turns": 20,
            "tools": True,
        }
        assert _knobs(service, "beta", "beta-1") == {
            "thinking": "low",
            "turns": 4,
            "tools": True,
        }

    def test_switching_a_model_carries_its_own_pair_into_the_listing(
        self, prefs: Preferences, env: dict[str, str]
    ) -> None:
        section = _section(
            providers={
                "alpha": ProviderSection(
                    name="alpha",
                    base_url="https://alpha.example/v1",
                    models=(ModelSpec(id="alpha-1"), ModelSpec(id="alpha-small")),
                    default_model="alpha-1",
                    api_key_env="ALPHA_API_KEY",
                    cost_input_per_1m=0.0,
                    cost_output_per_1m=0.0,
                ),
                "beta": ProviderSection(
                    name="beta",
                    base_url="https://beta.example/v1",
                    models=(ModelSpec(id="beta-1"),),
                    default_model="beta-1",
                    api_key_env="BETA_API_KEY",
                    cost_input_per_1m=0.0,
                    cost_output_per_1m=0.0,
                ),
            }
        )
        service, _, _ = _service(prefs, env, section=section)
        service.set_tuning(thinking="high", turns=20)
        # Give the second model a different pair, then switch back and read the listing.
        service.choose_model("alpha", "alpha-small")
        service.set_tuning(thinking="off", turns=0)

        listing = service.choose_model("alpha", "alpha-1")

        assert listing["thinking"] == "high"
        assert listing["turns"] == 20

    def test_one_knob_does_not_reset_the_other(
        self, prefs: Preferences, env: dict[str, str]
    ) -> None:
        """The two knobs sit next to each other; a caller changing one must not
        silently send the other back to its default."""
        service, _, _ = _service(prefs, env)
        service.set_tuning(thinking="high", turns=30)

        outcome = service.set_tuning(turns=2)

        assert outcome["turns"] == 2
        assert outcome["thinking"] == "high", "the level nobody touched must survive"

    def test_an_unknown_level_is_refused_loudly(
        self, prefs: Preferences, env: dict[str, str]
    ) -> None:
        service, _, _ = _service(prefs, env)

        outcome = service.set_tuning(thinking="galaxy-brain")

        assert outcome["ok"] is False
        assert outcome["error"]
        # 出厂那份是「不思考 / 2048」：没单独设过的模型跟的是最后一次改动那两个全局镜像，
        # 而全新安装的镜像就是关（r85 之前这里固定回 "medium"，面板却显示未勾选）。
        assert outcome["thinking"] == "off", "the refusal must not move the knob"

    def test_a_turn_count_outside_the_advertised_bounds_is_refused(
        self, prefs: Preferences, env: dict[str, str]
    ) -> None:
        service, _, _ = _service(prefs, env)

        outcome = service.set_tuning(turns=9_999)

        assert outcome["ok"] is False
        assert "0" in outcome["error"] and "50" in outcome["error"]

    def test_the_listing_advertises_the_same_bounds_the_save_enforces(
        self, prefs: Preferences, env: dict[str, str]
    ) -> None:
        service, _, _ = _service(prefs, env)

        listing = service.model_choices()

        assert listing["turns_bounds"] == [0, 50]
        assert listing["thinking_levels"] == ["off", "low", "medium", "high"]

    def test_a_hand_edited_level_degrades_to_the_default_rather_than_raising(
        self, prefs: Preferences, env: dict[str, str]
    ) -> None:
        """preferences.json is a file the operator can open in Notepad."""
        from jarvis.app.preferences import LLM_TUNING

        prefs.set(LLM_TUNING, {"alpha\x00alpha-1": {"thinking": "banana", "turns": "many"}})
        service, _, _ = _service(prefs, env)

        assert _knobs(service, "alpha", "alpha-1") == {
            "thinking": "medium",
            "turns": 10,
            "tools": True,
        }


class TestEditingTheModelList:
    """Adding and removing models inside one provider, from the window."""

    def test_a_model_can_be_added_and_then_answered_by_name(
        self, prefs: Preferences, env: dict[str, str]
    ) -> None:
        service, _, _ = _service(prefs, env)

        outcome = service.add_model("alpha", "alpha-turbo", "Alpha Turbo")

        assert outcome["ok"] is True
        assert outcome["default_model"] == "alpha-1", "adding must not steal the default"
        assert [spec["id"] for spec in outcome["models"]] == ["alpha-1", "alpha-turbo"]
        assert outcome["models"][1]["label"] == "Alpha Turbo"
        assert service.choose_model("alpha", "alpha-turbo")["model"] == "alpha-turbo"

    def test_a_duplicate_is_refused_rather_than_listed_twice(
        self, prefs: Preferences, env: dict[str, str]
    ) -> None:
        service, _, _ = _service(prefs, env)

        outcome = service.add_model("alpha", "alpha-1")

        assert outcome["ok"] is False
        assert outcome["error"]

    def test_an_empty_or_spaced_name_is_refused(
        self, prefs: Preferences, env: dict[str, str]
    ) -> None:
        service, _, _ = _service(prefs, env)

        assert service.add_model("alpha", "  ")["ok"] is False
        assert service.add_model("alpha", "has a space")["ok"] is False

    def test_adding_to_an_unknown_provider_is_refused_loudly(
        self, prefs: Preferences, env: dict[str, str]
    ) -> None:
        service, _, _ = _service(prefs, env)

        outcome = service.add_model("nope", "m-1")

        assert outcome["ok"] is False
        assert "nope" in outcome["error"]

    def test_the_window_added_model_survives_a_restart(
        self, prefs: Preferences, env: dict[str, str]
    ) -> None:
        """Stored as an override, so the next boot reads the same list back."""
        service, _, _ = _service(prefs, env)
        service.add_model("alpha", "alpha-turbo")

        reopened, _, _ = _service(prefs, env)
        row = next(
            entry for entry in reopened.model_choices()["providers"] if entry["name"] == "alpha"
        )

        assert [spec["id"] for spec in row["models"]] == ["alpha-1", "alpha-turbo"]

    def test_a_model_can_be_removed(self, prefs: Preferences, env: dict[str, str]) -> None:
        service, _, _ = _service(prefs, env)
        service.add_model("alpha", "alpha-turbo")

        outcome = service.remove_model("alpha", "alpha-turbo")

        assert outcome["ok"] is True
        assert [spec["id"] for spec in outcome["models"]] == ["alpha-1"]

    def test_the_last_model_cannot_be_removed(
        self, prefs: Preferences, env: dict[str, str]
    ) -> None:
        """A provider with no models is one neither the picker nor the client factory
        can use, so the last row is a floor rather than a suggestion."""
        service, _, _ = _service(prefs, env)

        outcome = service.remove_model("alpha", "alpha-1")

        assert outcome["ok"] is False
        assert "至少" in outcome["error"]
        assert [spec["id"] for spec in outcome["models"]] == ["alpha-1"]

    def test_removing_the_model_in_use_moves_the_default_rather_than_pointing_at_it(
        self, prefs: Preferences, env: dict[str, str]
    ) -> None:
        service, _, _ = _service(prefs, env)
        service.add_model("alpha", "alpha-turbo")
        service.choose_model("alpha", "alpha-turbo")

        service.remove_model("alpha", "alpha-turbo")

        assert prefs.text(LLM_MODEL) == "alpha-1"
        assert service.model_choices()["model"] == "alpha-1"

    def test_removing_a_model_that_is_not_there_says_so(
        self, prefs: Preferences, env: dict[str, str]
    ) -> None:
        service, _, _ = _service(prefs, env)

        outcome = service.remove_model("alpha", "ghost")

        assert outcome["ok"] is False
        assert outcome["error"]

    def test_the_single_model_field_is_gone_after_a_write(
        self, prefs: Preferences, env: dict[str, str]
    ) -> None:
        """Two spellings of the same list is how the panel and the picker end up
        showing different sets."""
        from jarvis.app.preferences import LLM_OVERRIDES

        service, _, _ = _service(prefs, env)
        prefs.set(LLM_OVERRIDES, {"alpha": {"model": "legacy", "base_url": "https://a.example/v1"}})

        service.add_model("alpha", "alpha-turbo")

        assert "model" not in prefs.get(LLM_OVERRIDES)["alpha"]


def test_the_panel_can_store_the_cloud_voice_key(prefs: Preferences, env: dict[str, str]) -> None:
    """``key_for="voice_cloud"`` writes the DashScope variable.

    Without a name for it the settings field would exist on screen and save
    nowhere, which reads as "the app is broken" rather than "this key is not
    wired up".
    """
    from jarvis.app.settings_service import CLOUD_VOICE_KEY_ENV

    service, _llm, recorder = _service(prefs, env)
    outcome = service.apply({"api_key": "sk-cloud", "key_for": "voice_cloud"}, by="test")
    assert outcome["problems"] == {}
    assert env[CLOUD_VOICE_KEY_ENV] == "sk-cloud"
    assert (CLOUD_VOICE_KEY_ENV, "sk-cloud") in recorder.writes


def test_a_cloud_voice_key_is_reported_as_set_without_echoing_it(
    prefs: Preferences, env: dict[str, str]
) -> None:
    """The snapshot says *whether* the key is set and never what it is: echoing
    a secret into a webview puts it in the DOM and in every screenshot."""
    from jarvis.app.settings_service import CLOUD_VOICE_KEY_ENV

    env[CLOUD_VOICE_KEY_ENV] = "sk-cloud"
    service, _llm, _recorder = _service(prefs, env)
    snapshot = service.snapshot()
    assert snapshot["cloud_voice_key_set"] is True
    assert snapshot["cloud_voice_key_variable"] == CLOUD_VOICE_KEY_ENV
    assert "sk-cloud" not in str(snapshot)


def test_the_cloud_voice_key_variable_matches_what_the_client_reads() -> None:
    """One name, two readers, or the key never takes effect.

    The settings panel writes a *variable name* while the cloud client reads
    one, and if they ever drift the symptom is "I entered the key and nothing
    changed" -- with no error anywhere. Asserted rather than commented because a
    comment cannot fail.
    """
    from jarvis.app.settings_service import CLOUD_VOICE_KEY_ENV
    from jarvis.tts.cloud import DEFAULT_KEY_ENV

    assert CLOUD_VOICE_KEY_ENV == DEFAULT_KEY_ENV


class TestTheAlertSettingsRoundTrip:
    """The panel's 告警 box and the engine's knobs must be reading one store.

    These go through the real :class:`SettingsService` and the real
    :class:`AlertService` rather than a stub, because the failure this guards against is
    the two of them disagreeing -- a panel that says 保存成功 while the engine kept the
    old line is exactly the bug the model-list persistence had.
    """

    def test_the_snapshot_carries_the_rules_the_engine_actually_has(
        self, prefs: Preferences, env: dict[str, str]
    ) -> None:
        service, _, _ = _service(prefs, env, alerts=AlertService(prefs))

        section = service.snapshot()["alerts"]
        assert [row["code"] for row in section["rules"]] == ["cpu", "memory", "swap", "disk"]
        assert section["cooldown_bounds"] == [1.0, 180.0]
        assert section["sustain_seconds"] == 60.0

    def test_a_threshold_saved_from_the_panel_moves_the_line(
        self, prefs: Preferences, env: dict[str, str]
    ) -> None:
        alerts = AlertService(prefs)
        service, _, _ = _service(prefs, env, alerts=alerts)

        result = service.apply({"alerts_rules": {"cpu": {"threshold": 70.0}}})
        assert result["problems"] == {}
        by_code = {row["code"]: row for row in result["alerts"]["rules"]}
        assert by_code["cpu"]["threshold"] == 70.0, "回给界面的就是引擎在用的那一条"

    def test_a_threshold_outside_the_rule_is_refused_and_not_stored(
        self, prefs: Preferences, env: dict[str, str]
    ) -> None:
        service, _, _ = _service(prefs, env, alerts=AlertService(prefs))

        result = service.apply({"alerts_rules": {"cpu": {"threshold": 7.0}}})
        assert "alerts_rules" in result["problems"]
        by_code = {row["code"]: row for row in result["alerts"]["rules"]}
        assert by_code["cpu"]["threshold"] == 90.0

    def test_an_unknown_rule_name_is_refused(self, prefs: Preferences, env: dict[str, str]) -> None:
        service, _, _ = _service(prefs, env, alerts=AlertService(prefs))

        result = service.apply({"alerts_rules": {"gpu": {"enabled": False}}})
        assert "没有这条告警" in result["problems"]["alerts_rules"]

    def test_one_bad_row_refuses_the_whole_rules_patch(
        self, prefs: Preferences, env: dict[str, str]
    ) -> None:
        service, _, _ = _service(prefs, env, alerts=AlertService(prefs))

        result = service.apply(
            {
                "alerts_rules": {"cpu": {"threshold": 70.0}, "memory": {"threshold": 500.0}},
            }
        )
        assert "alerts_rules" in result["problems"]
        by_code = {row["code"]: row for row in result["alerts"]["rules"]}
        assert by_code["cpu"]["threshold"] == 90.0, "一半生效比全部不生效更难查"
        assert by_code["memory"]["threshold"] == 90.0

    def test_a_refused_rule_does_not_swallow_the_rest_of_the_form(
        self, prefs: Preferences, env: dict[str, str]
    ) -> None:
        """The all-or-nothing is about the four rules, not about the whole dialog.

        The panel re-sends every alert key on each 保存; rejecting a bad threshold must
        not quietly discard the cooldown the operator also moved, and must not report it
        as a problem either -- the snapshot it gets back says what is actually true.
        """
        service, _, _ = _service(prefs, env, alerts=AlertService(prefs))

        result = service.apply(
            {"alerts_rules": {"cpu": {"threshold": 7.0}}, "alerts_cooldown_minutes": 3.0}
        )
        assert "alerts_rules" in result["problems"]
        assert "alerts_cooldown_minutes" not in result["problems"]
        assert result["alerts"]["cooldown_minutes"] == 3.0
        by_code = {row["code"]: row for row in result["alerts"]["rules"]}
        assert by_code["cpu"]["threshold"] == 90.0

    def test_the_cooldown_and_the_voice_switch_survive_a_restart(
        self, prefs: Preferences, env: dict[str, str]
    ) -> None:
        service, _, _ = _service(prefs, env, alerts=AlertService(prefs))
        result = service.apply({"alerts_cooldown_minutes": 3.0, "alerts_speak_critical": False})
        assert result["problems"] == {}

        reopened = AlertService(Preferences(prefs.path))
        assert reopened.settings()["cooldown_minutes"] == 3.0
        assert reopened.settings()["speak_critical"] is False

    def test_a_cooldown_outside_the_bounds_is_refused(
        self, prefs: Preferences, env: dict[str, str]
    ) -> None:
        service, _, _ = _service(prefs, env, alerts=AlertService(prefs))

        for rejected in (0, 900):
            problems = service.apply({"alerts_cooldown_minutes": rejected})["problems"]
            assert "alerts_cooldown_minutes" in problems, f"{rejected} 分钟不该被收下"

    def test_without_an_alert_centre_the_panel_gets_a_refusal_not_a_silent_yes(
        self, prefs: Preferences, env: dict[str, str]
    ) -> None:
        service, _, _ = _service(prefs, env)

        result = service.apply({"alerts_cooldown_minutes": 3.0})
        assert result["problems"]["alerts_cooldown_minutes"] == "告警中心未启用"
        assert result["alerts"] == {}, "没有引擎就没有这一节，界面不该画出四条空的线"


class TestTheWakeWordRoundTrip:
    """The 唤醒词 box, the settings store, and the words the engine will actually watch."""

    SHIPPED = ("你好小夜", "你好小智", "你好晓夜", "你好小业")

    def test_the_snapshot_shows_both_the_shipped_words_and_the_bounds(
        self, prefs: Preferences, env: dict[str, str]
    ) -> None:
        words = WakeWords(prefs, lambda: self.SHIPPED)
        service, _, _ = _service(prefs, env, wake_words=words)

        snapshot = service.snapshot()

        assert snapshot["wake_keywords"] == list(self.SHIPPED)
        assert snapshot["wake_keywords_stored"] == []
        assert snapshot["wake_keyword_min_chars"] == MIN_KEYWORD_CHARS

    def test_a_rename_is_live_immediately_and_survives_a_restart(
        self, prefs: Preferences, env: dict[str, str], tmp_path: Any
    ) -> None:
        """One object, two threads, then a second process.

        The engine reads through the same store the panel writes, so "saved" and "watching"
        are one fact rather than two that can drift -- which is precisely the shape the model
        list had when it looked unsaved.
        """
        words = WakeWords(prefs, lambda: self.SHIPPED)
        service, _, _ = _service(prefs, env, wake_words=words)

        result = service.apply({"wake_keywords": "辛苦你了、Hey Jarvis"})

        assert result["problems"] == {}
        assert result["wake_keywords"] == ["辛苦你了", "Hey Jarvis"]
        assert words.effective() == ("辛苦你了", "Hey Jarvis"), "改名还得重启就是没改"

        reopened = WakeWords(Preferences(tmp_path / "preferences.json"), lambda: self.SHIPPED)
        assert reopened.effective() == ("辛苦你了", "Hey Jarvis")

    def test_the_old_words_stop_waking_the_assistant_once_the_save_lands(
        self, prefs: Preferences, env: dict[str, str]
    ) -> None:
        words = WakeWords(prefs, lambda: self.SHIPPED)
        service, _, _ = _service(prefs, env, wake_words=words)

        assert words.effective() == self.SHIPPED
        service.apply({"wake_keywords": "辛苦你了"})

        assert "你好小夜" not in words.effective()

    def test_clearing_the_box_hands_the_shipped_spellings_back(
        self, prefs: Preferences, env: dict[str, str]
    ) -> None:
        words = WakeWords(prefs, lambda: self.SHIPPED)
        service, _, _ = _service(prefs, env, wake_words=words)
        assert service.apply({"wake_keywords": "辛苦你了"})["problems"] == {}

        result = service.apply({"wake_keywords": "   "})

        assert result["problems"] == {}
        assert result["wake_keywords"] == list(self.SHIPPED)

    def test_a_word_that_is_too_short_is_refused_and_changes_nothing(
        self, prefs: Preferences, env: dict[str, str]
    ) -> None:
        words = WakeWords(prefs, lambda: self.SHIPPED)
        service, _, _ = _service(prefs, env, wake_words=words)
        assert service.apply({"wake_keywords": "辛苦你了"})["problems"] == {}

        result = service.apply({"wake_keywords": "夜"})

        assert "wake_keywords" in result["problems"]
        assert "太短" in result["problems"]["wake_keywords"]
        assert words.effective() == ("辛苦你了",)

    def test_without_a_wake_word_layer_no_box_is_drawn(
        self, prefs: Preferences, env: dict[str, str]
    ) -> None:
        """A shell that never wired this must not offer a field that saves into nowhere."""
        service, _, _ = _service(prefs, env)

        snapshot = service.snapshot()
        assert "wake_keywords" not in snapshot
        refused = service.apply({"wake_keywords": "辛苦你了"})
        assert refused["problems"]["wake_keywords"] == "这台机器没有接唤醒词设置"


class TestHidingAProviderRow:
    """「藏起来」 is a view switch. The only destructive version of it would be a delete.

    The row still exists in ``config.yaml``, still resolves, and 找回 puts it back with
    nothing to repair -- which is the whole reason the panel is allowed to offer it.
    """

    def test_a_hidden_row_leaves_the_menu_but_not_the_file(
        self, prefs: Preferences, env: dict[str, str]
    ) -> None:
        service, _, _ = _service(prefs, env)
        assert [row["name"] for row in service.snapshot()["models"]] == ["alpha", "beta"]

        result = service.apply({"hide_provider": "beta"})

        assert result["problems"] == {}
        assert [row["name"] for row in result["models"]] == ["alpha"]
        assert result["providers"] == ["alpha"]
        assert [row["name"] for row in result["hidden_models"]] == ["beta"]
        assert result["hidden_models"][0]["source"] == "配置文件"

    def test_the_row_in_use_cannot_be_hidden(self, prefs: Preferences, env: dict[str, str]) -> None:
        """Otherwise the chat header names a provider no list contains."""
        service, _, _ = _service(prefs, env)

        result = service.apply({"hide_provider": "alpha"})

        assert "正在用" in result["problems"]["hide_provider"]
        assert [row["name"] for row in result["models"]] == ["alpha", "beta"]
        assert result["hidden_models"] == []

    def test_moving_first_then_hiding_works(self, prefs: Preferences, env: dict[str, str]) -> None:
        service, _, _ = _service(prefs, env)

        assert service.apply({"provider": "beta"})["provider"] == "beta"
        result = service.apply({"hide_provider": "alpha"})

        assert result["problems"] == {}
        assert [row["name"] for row in result["models"]] == ["beta"]
        assert [row["name"] for row in result["hidden_models"]] == ["alpha"]

    def test_finding_it_again_restores_the_whole_list(
        self, prefs: Preferences, env: dict[str, str]
    ) -> None:
        service, _, _ = _service(prefs, env)
        assert service.apply({"hide_provider": "beta"})["problems"] == {}

        result = service.apply({"show_provider": "beta"})

        assert result["problems"] == {}
        assert [row["name"] for row in result["models"]] == ["alpha", "beta"]
        assert result["hidden_models"] == []

    def test_unhiding_something_that_is_not_hidden_is_refused(
        self, prefs: Preferences, env: dict[str, str]
    ) -> None:
        service, _, _ = _service(prefs, env)

        result = service.apply({"show_provider": "beta"})

        assert "没有被藏起来" in result["problems"]["show_provider"]

    def test_an_unknown_row_is_refused_rather_than_stored(
        self, prefs: Preferences, env: dict[str, str]
    ) -> None:
        """Storing a typo would hide nothing and list a 找回 button for nothing."""
        service, _, _ = _service(prefs, env)

        result = service.apply({"hide_provider": "gamma"})

        assert "没有这一行服务商" in result["problems"]["hide_provider"]
        assert service.snapshot()["hidden_models"] == []

    def test_the_choice_survives_a_restart(
        self, prefs: Preferences, env: dict[str, str], tmp_path: Any
    ) -> None:
        """Read back through a second instance over the same file -- the model-list lesson."""
        service, _, _ = _service(prefs, env)
        assert service.apply({"hide_provider": "beta"})["problems"] == {}

        reopened = SettingsService(
            Preferences(tmp_path / "preferences.json"),
            _FakeLlm(),  # type: ignore[arg-type]
            lambda: _section(),
            environ=env,
            persist_env=_Recorder(),
        )
        snapshot = reopened.snapshot()

        assert [row["name"] for row in snapshot["models"]] == ["alpha"]
        assert [row["name"] for row in snapshot["hidden_models"]] == ["beta"]

    def test_the_file_itself_is_never_touched(
        self, prefs: Preferences, env: dict[str, str]
    ) -> None:
        """Hiding must not look like editing, and must not write anything to config.yaml."""
        section = _section()
        service, _, _ = _service(prefs, env, section=section)

        service.apply({"hide_provider": "beta"})

        assert set(section.providers) == {"alpha", "beta"}

    def test_a_stale_name_is_skipped_and_said_out_loud(
        self, prefs: Preferences, env: dict[str, str], caplog: pytest.LogCaptureFixture
    ) -> None:
        """A hidden row whose provider later disappeared must not render a dead button."""
        assert prefs.set(LLM_HIDDEN, ["ghost"])
        service, _, _ = _service(prefs, env)

        with caplog.at_level("WARNING", logger="jarvis.app.settings_service"):
            snapshot = service.snapshot()

        assert snapshot["hidden_models"] == []
        assert "ghost" in caplog.text


class TestTurningARowKeyless:
    """The row editor's 免 Key checkbox, for rows that already exist.

    Someone who adds an Ollama row without ticking the box is refused and rolled back --
    but a row that arrived from ``config.yaml`` (or was added while a key *was* set) would
    otherwise have no way to become keyless, and hand-editing a yaml file is not something
    a person who bought a desktop app should have to do.
    """

    @staticmethod
    def _keyless_alpha() -> LlmSection:
        return replace(
            _section(),
            providers={
                **_section().providers,
                "alpha": replace(_section().providers["alpha"], key_optional=True),
            },
        )

    def test_turning_it_on_reaches_the_client_and_the_panel(
        self, prefs: Preferences, env: dict[str, str]
    ) -> None:
        service, fake_llm, _ = _service(prefs, env)

        outcome = service.apply({"target": "alpha", "key_optional": True})

        assert outcome["problems"] == {}
        row = next(entry for entry in outcome["models"] if entry["name"] == "alpha")
        assert row["key_optional"] is True
        assert fake_llm.override is not None
        assert fake_llm.override.providers["alpha"].key_optional is True

    def test_the_flag_survives_a_restart(self, prefs: Preferences, env: dict[str, str]) -> None:
        service, _, _ = _service(prefs, env)
        service.apply({"target": "alpha", "key_optional": True})

        restarted, _, _ = _service(Preferences(prefs.path), env)

        row = next(entry for entry in restarted.snapshot()["models"] if entry["name"] == "alpha")
        assert row["key_optional"] is True

    def test_a_save_that_did_not_touch_the_box_leaves_the_file_alone(
        self, prefs: Preferences, env: dict[str, str]
    ) -> None:
        """The panel re-sends the whole form on every 保存.

        Writing ``False`` for rows nobody re-checked would take the flag away from a
        ``config.yaml`` provider declared keyless -- the same class of bug as the one that
        made a blank 调用地址 mean 不覆盖.
        """
        service, _, _ = _service(prefs, env, section=self._keyless_alpha())

        service.apply({"target": "alpha", "model": "alpha-1"})

        row = next(entry for entry in service.snapshot()["models"] if entry["name"] == "alpha")
        assert row["key_optional"] is True
        stored = prefs.get(LLM_OVERRIDES) or {}
        assert "key_optional" not in stored.get("alpha", {})

    def test_turning_it_off_demands_a_key_again(
        self, prefs: Preferences, env: dict[str, str]
    ) -> None:
        service, fake_llm, _ = _service(prefs, env, section=self._keyless_alpha())

        outcome = service.apply({"target": "alpha", "key_optional": False})

        row = next(entry for entry in outcome["models"] if entry["name"] == "alpha")
        assert row["key_optional"] is False
        assert fake_llm.override is not None
        assert fake_llm.override.providers["alpha"].key_optional is False

    def test_a_row_that_is_not_there_is_named_and_changes_nothing(
        self, prefs: Preferences, env: dict[str, str]
    ) -> None:
        service, _, _ = _service(prefs, env)

        outcome = service.apply({"target": "nope", "key_optional": True})

        assert "key_optional" in outcome["problems"]
        assert not prefs.get(LLM_OVERRIDES)


class TestPerProviderTimeout:
    """The row's own timeout, from the add block and from the row editor."""

    def test_an_added_row_keeps_its_timeout_and_reaches_the_client(
        self, prefs: Preferences, env: dict[str, str]
    ) -> None:
        service, fake_llm, _ = _service(prefs, env)

        outcome = service.apply(
            {
                "add_model": {
                    "name": "ollama",
                    "base_url": "http://localhost:11434/v1",
                    "model": "qwen2.5",
                    "key_optional": True,
                    "timeout_seconds": "180",
                }
            }
        )

        assert outcome["problems"] == {}
        row = next(entry for entry in outcome["models"] if entry["name"] == "ollama")
        assert row["timeout_seconds"] == 180.0
        assert fake_llm.override is not None
        assert fake_llm.override.providers["ollama"].timeout_seconds == 180.0

    def test_the_timeout_survives_a_restart(self, prefs: Preferences, env: dict[str, str]) -> None:
        service, _, _ = _service(prefs, env)
        service.apply(
            {
                "add_model": {
                    "name": "ollama",
                    "base_url": "http://localhost:11434/v1",
                    "model": "qwen2.5",
                    "timeout_seconds": 180,
                }
            }
        )

        restarted, _, _ = _service(Preferences(prefs.path), env)

        row = next(entry for entry in restarted.snapshot()["models"] if entry["name"] == "ollama")
        assert row["timeout_seconds"] == 180.0

    def test_a_timeout_outside_the_bounds_is_refused_where_it_is_typed(
        self, prefs: Preferences, env: dict[str, str]
    ) -> None:
        service, _, _ = _service(prefs, env)

        outcome = service.apply(
            {
                "add_model": {
                    "name": "ollama",
                    "base_url": "http://localhost:11434/v1",
                    "model": "qwen2.5",
                    "timeout_seconds": "9000",
                }
            }
        )

        assert "add_model" in outcome["problems"]
        assert "5" in outcome["problems"]["add_model"]

    def test_the_row_editor_can_set_and_clear_it(
        self, prefs: Preferences, env: dict[str, str]
    ) -> None:
        service, fake_llm, _ = _service(prefs, env)

        set_it = service.apply({"target": "alpha", "timeout_seconds": "240"})
        row = next(entry for entry in set_it["models"] if entry["name"] == "alpha")
        assert row["timeout_seconds"] == 240.0
        assert fake_llm.override is not None
        assert fake_llm.override.providers["alpha"].timeout_seconds == 240.0

        cleared = service.apply({"target": "alpha", "timeout_seconds": ""})
        row = next(entry for entry in cleared["models"] if entry["name"] == "alpha")
        assert row["timeout_seconds"] is None, "清空要真的回到全局，不是留着旧值"

    def test_the_row_editor_refuses_an_out_of_bounds_number_too(
        self, prefs: Preferences, env: dict[str, str]
    ) -> None:
        """The add block and the row editor are two doors onto one rule.

        Testing only the add block left the editor's bounds check unwatched -- a
        mutation that deleted it stayed green.
        """
        service, _, _ = _service(prefs, env)

        outcome = service.apply({"target": "alpha", "timeout_seconds": "9000"})

        assert "timeout_seconds" in outcome["problems"]
        row = next(entry for entry in outcome["models"] if entry["name"] == "alpha")
        assert row["timeout_seconds"] is None

    def test_the_snapshot_ships_the_global_and_the_loader_labels(
        self, prefs: Preferences, env: dict[str, str]
    ) -> None:
        service, _, _ = _service(prefs, env)

        snapshot = service.snapshot()
        assert snapshot["llm_timeout_seconds"] == 30.0
        options = snapshot["thinking_loader_options"]
        assert {option["id"] for option in options} == set(snapshot["thinking_loader_choices"])
        assert all(option["label"] for option in options)


class TestTakingTheToolsAwayFromOneNamedPair:
    """本地模型那一轮之后要记住的是**它**，不是"界面当前那一对"。

    会话可以钉在另一个模型上；按"当前"写就会把精简记到别人头上，然后操作员看见
    一格自己没勾过的勾，而真正装不下的那一行下一轮还是要先失败一次。
    """

    def test_it_writes_the_named_pair_and_keeps_the_other_two_knobs(
        self, prefs: Preferences, env: dict[str, str]
    ) -> None:
        service, _, _ = _service(prefs, env)
        service.set_tuning(thinking="high", turns=30)

        answer = service.remember_tools("alpha", "alpha-1", False)

        assert answer["ok"] is True
        assert _knobs(service, "alpha", "alpha-1") == {
            "thinking": "high",
            "turns": 30,
            "tools": False,
        }

    def test_it_survives_a_restart_because_it_was_written_in_its_own_shape(
        self, prefs: Preferences, env: dict[str, str]
    ) -> None:
        """第二次读要读回同一个形状 —— 这是 r60 那一课，别再犯。"""
        service, _, _ = _service(prefs, env)
        service.remember_tools("alpha", "alpha-1", False)

        again, _, _ = _service(prefs, env)

        assert again.tuning_for("alpha", "alpha-1")["tools"] is False

    def test_a_pair_nobody_touched_still_carries_tools(
        self, prefs: Preferences, env: dict[str, str]
    ) -> None:
        """没说过就是带：这格存在之前每一轮都带着工具表，老记录不能反过来拿走她的手。"""
        service, _, _ = _service(prefs, env)

        assert service.tuning_for("alpha", "alpha-1")["tools"] is True

    def test_turning_one_pair_lean_leaves_the_others_alone(
        self, prefs: Preferences, env: dict[str, str]
    ) -> None:
        service, _, _ = _service(prefs, env)
        service.remember_tools("alpha", "alpha-1", False)

        assert service.tuning_for("alpha", "alpha-2")["tools"] is True

    def test_the_pair_named_is_the_pair_written_even_when_another_is_current(
        self, prefs: Preferences, env: dict[str, str]
    ) -> None:
        """当前是 alpha，刚 overflow 的是 beta：记住的必须是 beta。

        按「当前」写是这一格最容易写错的地方 —— 界面看着没动，下一轮 beta 还是先撞一次
        400，而 alpha 莫名其妙少了那双手。
        """
        service, _, _ = _service(prefs, env)
        assert service.selected_pair()[0] == "alpha"

        service.remember_tools("beta", "beta-1", False)

        assert service.tuning_for("beta", "beta-1")["tools"] is False
        assert service.tuning_for("alpha", "alpha-1")["tools"] is True


def _knobs(service: SettingsService, provider: str, model: str) -> dict[str, object]:
    """只取调用方真正据以行动的三格。

    ``tuning_for`` 还带回档位、精确预算、有没有单独设过（r85 加的），整字典相等会把
    这几格也钉死 —— 断言想说清的是"这一对的核心三格是什么"，而不是字典长什么样。
    """
    row = service.tuning_for(provider, model)
    return {"thinking": row["thinking"], "turns": row["turns"], "tools": row["tools"]}


def _with_local(section: LlmSection) -> LlmSection:
    from dataclasses import replace

    from jarvis.config.schema import ModelSpec, ProviderSection

    local = ProviderSection(
        name="local",
        base_url="http://127.0.0.1:28181/v1",
        models=(ModelSpec("qwen2.5-7b", "自带·Qwen2.5-7B（离线）"),),
        default_model="qwen2.5-7b",
        api_key_env="",
        key_optional=True,
        timeout_seconds=300.0,
        cost_input_per_1m=0.0,
        cost_output_per_1m=0.0,
    )
    providers = dict(section.providers)
    providers["local"] = local
    return replace(section, providers=providers)


def test_the_bundled_model_is_the_default_only_when_it_can_run(
    prefs: Preferences, env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A buyer without the 4.36 GB weights must still get answers.

    The YAML keeps the cloud row as the configured default; ``local`` becomes the default
    only while :func:`local_server.plan` says this machine can actually run it.
    """
    from jarvis.llm import local_server

    service, _, _ = _service(prefs, env, section=_with_local(_section()))
    monkeypatch.setattr(local_server, "plan", lambda: local_server.Plan(False, "没找到权重"))

    assert service.selected_pair()[0] != local_server.LOCAL_PROVIDER

    monkeypatch.setattr(local_server, "plan", lambda: local_server.Plan(True, "", None, None, 9000))

    assert service.selected_pair()[0] == local_server.LOCAL_PROVIDER


def test_the_bundled_provider_points_at_the_port_that_is_answering(
    prefs: Preferences, env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The yaml port is a guess; the live one is what the child bound."""
    from jarvis.llm import local_server

    service, _, _ = _service(prefs, env, section=_with_local(_section()))
    monkeypatch.setattr(local_server, "endpoint", lambda: "http://127.0.0.1:29555/v1")

    rows = service.model_choices()["providers"]
    local = next(row for row in rows if row["name"] == local_server.LOCAL_PROVIDER)

    assert local["base_url"] == "http://127.0.0.1:29555/v1"


class TestBundledModelDials:
    """What the shipped offline model costs is the buyer's machine's business, so they dial it.

    Every tier note these tests assert on is a measured number (see
    ``jarvis.llm.local_server.MEASURED_RESIDENT_MB``); if one of them drifts, the screen
    is quoting a figure nobody loaded.
    """

    @pytest.fixture(autouse=True)
    def _reset_dial(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from jarvis.llm import local_server
        from jarvis.llm.local_server import Tuning

        monkeypatch.setattr(local_server, "_TUNING", Tuning())

    def test_a_fresh_install_ships_this_machines_measured_shape(
        self, prefs: Preferences, env: dict[str, str]
    ) -> None:
        from jarvis.llm import local_server

        service, _, _ = _service(prefs, env)
        snap = service.snapshot()

        assert snap["local_context"] == local_server.DEFAULT_CONTEXT == 4096
        assert snap["local_idle_minutes"] == local_server.DEFAULT_IDLE_MINUTES == 10
        assert snap["local_eager"] is False
        assert snap["local_tool_table_tokens"] == local_server.TOOL_TABLE_TOKENS == 4702
        assert snap["local_needs_free_mb"] == 5200
        assert [row["id"] for row in snap["local_context_options"]] == [2048, 4096, 8192, 16384]
        assert [row["id"] for row in snap["local_idle_options"]] == [0, 2, 5, 10, 30]
        assert [row["needs_free_mb"] for row in snap["local_context_options"]] == [
            5072,
            5200,
            5456,
            5968,
        ], "每一档的门槛要各带各的：下拉框不许拿上次保存的那个数说话"

    def test_moving_the_window_moves_what_the_llm_layer_will_spawn(
        self, prefs: Preferences, env: dict[str, str]
    ) -> None:
        from jarvis.llm import local_server

        service, _, _ = _service(prefs, env)

        problems = service.apply({"local_context": 16384})["problems"]

        assert problems == {}, problems
        assert local_server.tuning().context == 16384
        assert service.snapshot()["local_needs_free_mb"] > 5200

    def test_a_window_off_the_ladder_is_refused_and_changes_nothing(
        self, prefs: Preferences, env: dict[str, str]
    ) -> None:
        from jarvis.llm import local_server

        service, _, _ = _service(prefs, env)

        answer = service.apply({"local_context": 5000})

        assert "5000" in answer["problems"]["local_context"], answer
        assert local_server.tuning().context == 4096, "被拒的值不许把口径带跑"

    def test_a_stored_value_that_is_not_a_tier_falls_back_instead_of_crashing(
        self, prefs: Preferences, env: dict[str, str]
    ) -> None:
        from jarvis.app.preferences import LOCAL_CONTEXT

        service, _, _ = _service(prefs, env)
        prefs.set(LOCAL_CONTEXT, 999)

        assert service.snapshot()["local_context"] == 4096

    def test_zero_idle_minutes_means_keep_it_loaded(
        self, prefs: Preferences, env: dict[str, str]
    ) -> None:
        from jarvis.llm import local_server

        service, _, _ = _service(prefs, env)

        assert service.apply({"local_idle_minutes": 0})["problems"] == {}
        assert local_server.tuning().idle_seconds == 0.0
        assert service.snapshot()["local_idle_minutes"] == 0

    def test_the_tier_note_quotes_measured_numbers_for_both_kinds_of_machine(
        self, prefs: Preferences, env: dict[str, str]
    ) -> None:
        service, _, _ = _service(prefs, env)

        notes = {row["id"]: row["note"] for row in service.snapshot()["local_context_options"]}

        # r88: 4096 no longer means "只回答、不动手". The full 4702-token table still does
        # not fit, but the routed pack does, so the honest sentence is the split one.
        assert "4.9 GB" in notes[4096]
        assert "装不下 4702 token 的完整工具清单" in notes[4096]
        assert "1606 token" in notes[4096] and "都能动手" in notes[4096]
        assert "只回答、不动手" not in notes[4096]
        assert "没显卡的机器约 4.5 GB" in notes[2048], "纯 CPU 那个数也要在，不然没显卡的买家被误导"
        assert "动手" in notes[8192] and "动手" in notes[16384]
        assert "没显卡的机器约 5.3 GB" in notes[16384]

    def test_the_bundled_model_says_its_thinking_budget_does_nothing(
        self, prefs: Preferences, env: dict[str, str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """r90 measured it: 32/128/512/2048 all thought ~850 characters on llama.cpp.

        The row used to claim the number could be verified against the reply's
        thinking-token count. On the shipped runtime it can't be verified because it
        is not applied, so the snapshot has to carry which of the two stories to tell.
        """
        from jarvis.llm import local_server

        service, _, _ = _service(prefs, env)

        assert service.snapshot()["thinking_budget_honored"] is True

        # The provider in view comes from the config section's default, not a pref --
        # so switching to 自带 means building the service against a local-default section.
        bundled, _, _ = _service(prefs, env, section=_section(default_provider="local"))
        monkeypatch.setattr(
            local_server,
            "selected_card",
            lambda: SimpleNamespace(thinking=True, label="Qwen3-4B"),
        )
        assert bundled.snapshot()["thinking_budget_honored"] is False

        # A bundled weight with no thinking mode at all (Qwen2.5) is a different case:
        # there the field is not "ignored", it simply never applies to either provider.
        monkeypatch.setattr(
            local_server,
            "selected_card",
            lambda: SimpleNamespace(thinking=False, label="Qwen2.5-7B"),
        )
        assert bundled.snapshot()["thinking_budget_honored"] is True


class TestTheDialIsAppliedAtBoot:
    """A stored dial has to be live before the first status read, not after the panel opens."""

    @pytest.fixture(autouse=True)
    def _reset_dial(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from jarvis.llm import local_server
        from jarvis.llm.local_server import Tuning

        monkeypatch.setattr(local_server, "_TUNING", Tuning())

    def test_starting_the_app_pushes_the_stored_window_into_the_llm_layer(
        self, prefs: Preferences, env: dict[str, str]
    ) -> None:
        from jarvis.app.preferences import LOCAL_CONTEXT
        from jarvis.llm import local_server

        prefs.set(LOCAL_CONTEXT, 16384)
        service, _, _ = _service(prefs, env)

        service.start()

        assert local_server.tuning().context == 16384
        assert local_server.shared_server().status()["context"] == 16384


class TestDisplayName:
    """The name on the screen is the operator's, not the build's."""

    def test_the_shipped_name_shows_until_somebody_changes_it(
        self, prefs: Preferences, env: dict[str, str]
    ) -> None:
        service, _, _ = _service(prefs, env)

        assert service.display_name() == "小夜"
        assert service.snapshot()["display_name_default"] == "小夜"

    def test_a_new_name_reaches_every_surface_that_asks(
        self, prefs: Preferences, env: dict[str, str]
    ) -> None:
        service, _, _ = _service(prefs, env)

        assert service.apply({"display_name": "阿明"})["problems"] == {}

        assert service.display_name() == "阿明"
        assert service.snapshot()["display_name"] == "阿明"

    def test_empty_hands_the_shipped_name_back(
        self, prefs: Preferences, env: dict[str, str]
    ) -> None:
        service, _, _ = _service(prefs, env)
        service.apply({"display_name": "阿明"})

        assert service.apply({"display_name": "   "})["problems"] == {}
        assert service.display_name() == "小夜"

    def test_a_name_long_enough_to_be_a_sentence_is_cut(
        self, prefs: Preferences, env: dict[str, str]
    ) -> None:
        service, _, _ = _service(prefs, env)

        service.apply({"display_name": "十二个字的名称刚刚好再多就溢出"})

        assert len(service.display_name()) == service.snapshot()["display_name_max"] == 12


def _shown(service: SettingsService) -> Any:
    """把桌面版真正接线的那份 reader 收窄成"必须有值"。

    ``_tuning_reader`` 的返回类型是 ``ConversationTuning | None``：拿不到就是 None，
    而 None 传进断言里 mypy 会一路报 union-attr，真机上则是"屏上那格空白"。
    """
    reader = _tuning_reader(service)

    def one(session: str, model: str) -> ConversationTuning:
        row = reader(session, model)
        assert row is not None, f"{session}/{model} 那一行读不到，接线断了"
        return row

    return one


class TestThePanelAndTheHeaderAreOneKnob:
    """r85：设置 → 思考与上下文 那三格，必须就是对话区那几格用的同一个值。

    用户拍图问"这两个地方都有上下文配置不会冲突吧"。查下去发现比冲突更糟：面板那三格
    写的是两个全局副本，而每次请求读的是**每一对模型自己那一行** ——
    实测面板显示「未勾选 / 2048 / 10 轮」，请求却带着 4096 token 的思考预算；
    在面板把轮数改成 2 并保存，发出去还是 10 轮。三格全是摆设。

    这里的判据一律走桌面版真正接线的那份 `_tuning_reader`，不另写一份翻译。
    """

    def test_a_fresh_install_shows_what_a_request_will_carry(
        self, prefs: Preferences, env: dict[str, str]
    ) -> None:
        service, _, _ = _service(prefs, env)

        snapshot = service.snapshot()
        sent = _shown(service)("alpha", "alpha-1")

        assert snapshot["thinking_enabled"] is sent.thinking.enabled
        assert snapshot["thinking_budget"] == sent.thinking.budget
        assert snapshot["history_turns"] == sent.history_turns
        assert snapshot["tuning_pinned"] is False
        assert snapshot["tuning_pair"] == "alpha · alpha-1"

    def test_a_save_in_the_panel_is_what_the_next_request_uses(
        self, prefs: Preferences, env: dict[str, str]
    ) -> None:
        service, _, _ = _service(prefs, env)
        reader = _shown(service)

        assert service.apply({"history_turns": 2, "thinking_enabled": False})["problems"] == {}

        sent = reader("alpha", "alpha-1")
        assert sent.history_turns == 2
        assert sent.thinking.enabled is False
        snapshot = service.snapshot()
        assert (snapshot["history_turns"], snapshot["thinking_enabled"]) == (2, False)
        assert snapshot["tuning_pinned"] is True

    def test_a_change_in_the_chat_header_shows_up_in_the_panel(
        self, prefs: Preferences, env: dict[str, str]
    ) -> None:
        service, _, _ = _service(prefs, env)

        service.set_tuning(thinking="high", turns=4)

        snapshot = service.snapshot()
        assert snapshot["thinking_enabled"] is True
        assert snapshot["thinking_budget"] == 16000
        assert snapshot["history_turns"] == 4

    def test_the_panel_turns_knob_overrides_a_row_the_header_pinned(
        self, prefs: Preferences, env: dict[str, str]
    ) -> None:
        """顶栏把这一对钉过之后，面板改轮数必须改得动。

        面板那格只写全局的话，已经存在的行会把它整个盖掉：屏上显示 2，发出去还是 4。
        出厂没行的机器看不出来 —— 这一段是 r85 变异复查时补的。
        """
        service, _, _ = _service(prefs, env)
        reader = _shown(service)
        service.set_tuning(turns=4)

        assert service.apply({"history_turns": 2})["problems"] == {}

        assert reader("alpha", "alpha-1").history_turns == 2
        assert service.snapshot()["history_turns"] == 2

    def test_the_panel_thinking_knob_overrides_a_row_the_header_pinned(
        self, prefs: Preferences, env: dict[str, str]
    ) -> None:
        """同一格思考勾：顶栏钉过的高强度，面板关掉就得真的关掉。"""
        service, _, _ = _service(prefs, env)
        reader = _shown(service)
        service.set_tuning(thinking="high")

        assert service.apply({"thinking_enabled": False})["problems"] == {}

        sent = reader("alpha", "alpha-1")
        assert sent.thinking.enabled is False, "行还在，全局写什么都没用"
        assert service.snapshot()["thinking_enabled"] is False

    def test_a_pair_that_was_never_set_follows_the_last_change(
        self, prefs: Preferences, env: dict[str, str]
    ) -> None:
        """没单独设过的那一对跟的是**最后一次改动**，不是出厂的 medium。

        这里以前固定回 "medium"：面板显示「未勾选」而请求带着 4096 —— 两个入口给两个答案。
        """
        service, _, _ = _service(prefs, env)
        assert service.apply({"thinking_enabled": True, "thinking_budget": 8000})["problems"] == {}

        row = service.tuning_for("beta", "beta-1")

        assert row["stored"] is False, "beta 这一对确实没单独设过"
        assert row["thinking_tokens"] == 8000, row
        assert row["thinking_enabled"] is True

    def test_the_panel_shows_the_pair_on_screen_not_the_last_global_write(
        self, prefs: Preferences, env: dict[str, str]
    ) -> None:
        """面板显示的是**屏幕上这一对**在用的值，不是全局副本。

        两对模型把全局镜像拧过之后，副本和行就分家了 —— 这正是 r85 那条 bug 的形状。
        """
        service, _, _ = _service(prefs, env)
        reader = _shown(service)
        assert (
            service.apply({"history_turns": 2, "thinking_enabled": True, "thinking_budget": 8000})[
                "problems"
            ]
            == {}
        )

        # 另一对模型上走**面板**那条路（它会连带把全局镜像写一遍），于是副本和这一对那行分家
        service.choose_model("beta", "beta-1")
        assert service.apply({"history_turns": 7, "thinking_enabled": False})["problems"] == {}

        service.choose_model("alpha", "alpha-1")

        snapshot = service.snapshot()
        assert snapshot["tuning_pair"] == "alpha · alpha-1"
        assert snapshot["history_turns"] == 2, "屏上不能写全局那个 7"
        assert snapshot["thinking_budget"] == 8000, "预算那格也一样：换过一次模型不该把它冲掉"
        assert snapshot["thinking_enabled"] is True, "这一对是开着的"
        assert reader("alpha", "alpha-1").history_turns == 2
        assert reader("alpha", "alpha-1").thinking.enabled is True
        assert reader("alpha", "alpha-1").thinking.budget == 8000

    def test_an_exact_budget_typed_in_the_panel_is_what_goes_out(
        self, prefs: Preferences, env: dict[str, str]
    ) -> None:
        """「思考预算（token）」那一格是数字，不是四档里的一个：填 8000 就发 8000。"""
        service, _, _ = _service(prefs, env)
        reader = _shown(service)

        assert service.apply({"thinking_enabled": True, "thinking_budget": 8000})["problems"] == {}

        sent = reader("alpha", "alpha-1")
        assert (sent.thinking.enabled, sent.thinking.budget) == (True, 8000)

        listing = service.model_choices()
        assert listing["thinking"] == "custom"
        assert listing["thinking_custom_budget"] == 8000
        assert "custom" in listing["thinking_levels"], "下拉挑不到它，屏上就像没设过"

        # 在对话区改选一个预设 = 换成那一档的数，精确预算让位
        service.set_tuning(thinking="low")
        assert reader("alpha", "alpha-1").thinking.budget == 1024

    def test_switching_thinking_off_keeps_the_number_for_next_time(
        self, prefs: Preferences, env: dict[str, str]
    ) -> None:
        service, _, _ = _service(prefs, env)
        reader = _shown(service)
        service.apply({"thinking_enabled": True, "thinking_budget": 8000})

        service.apply({"thinking_enabled": False})

        assert reader("alpha", "alpha-1").thinking.enabled is False
        assert service.snapshot()["thinking_budget"] == 8000, "那一格留着重开时要用的数"

        service.apply({"thinking_enabled": True})

        sent = reader("alpha", "alpha-1")
        assert (sent.thinking.enabled, sent.thinking.budget) == (True, 8000)

    def test_a_model_nobody_touched_follows_the_last_change(
        self, prefs: Preferences, env: dict[str, str]
    ) -> None:
        """没单独设过的模型跟最后一次改动 —— 面板里那句提示说的就是这个。"""
        service, _, _ = _service(prefs, env)
        reader = _shown(service)

        service.apply({"history_turns": 2})

        assert reader("beta", "beta-1").history_turns == 2
        assert reader("beta", "beta-1").tools is True, "轮数改了不该顺手把工具也拿走"
