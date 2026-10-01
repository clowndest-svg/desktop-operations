"""Tests for :class:`jarvis.app.settings_service.SettingsService`.

The interesting assertions here are not the happy path. They are the ones about
what must *not* happen: a key that reaches a file, a snapshot, or a log line; an
invalid endpoint that saves anyway and turns into "the assistant is broken" three
clicks later; an override that is remembered but never applied.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import pytest

from jarvis.app.preferences import (
    LLM_BASE_URL,
    LLM_MODEL,
    LLM_OVERRIDES,
    LLM_PROVIDER,
    TELEMETRY_INTERVAL_MS,
    VOICE_AUTO_ARM,
    VOICE_AUTO_SPEAK_TYPED,
    Preferences,
)
from jarvis.app.settings_service import SettingsService
from jarvis.config.schema import LlmSection, ProviderSection
from jarvis.core.exceptions import ConfigurationError
from jarvis.llm.service import LlmService

SECRET = "sk-DO-NOT-LEAK-9f2c"


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
                model="alpha-1",
                api_key_env="ALPHA_API_KEY",
                cost_input_per_1m=0.0,
                cost_output_per_1m=0.0,
            ),
            "beta": ProviderSection(
                name="beta",
                base_url="https://beta.example/v1",
                model="beta-1",
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
) -> tuple[SettingsService, Any, Any]:
    fake_llm = llm if llm is not None else _FakeLlm()
    recorder = persist if persist is not None else _Recorder()
    service = SettingsService(
        prefs,
        fake_llm,  # type: ignore[arg-type]
        lambda: section or _section(),
        environ=env,
        persist_env=recorder,
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


class TestModelChoices:
    """The chat's model picker, which is the settings provider field by another name."""

    def test_every_configured_model_is_listed_with_the_active_one_marked(
        self, prefs: Preferences, env: dict[str, str]
    ) -> None:
        service, _, _ = _service(prefs, env)

        listing = service.model_choices()

        assert sorted(entry["provider"] for entry in listing["choices"]) == sorted(
            service.snapshot()["providers"]
        )
        assert next(e for e in listing["choices"] if e["current"])["provider"] == listing["current"]

    def test_a_model_without_a_key_is_still_offered_but_flagged(
        self, prefs: Preferences, env: dict[str, str]
    ) -> None:
        """Hiding it turns "where did my other model go" into the bug report; the
        real answer is one environment variable the operator can set themselves."""
        service, _, _ = _service(prefs, env)

        entry = service.model_choices()["choices"][0]

        assert entry["key_set"] is False
        assert entry["key_variable"], "the name is what the user needs, never the value"
        assert "api_key" not in str(entry)

    def test_choosing_a_model_writes_the_same_preference_the_panel_writes(
        self, prefs: Preferences, env: dict[str, str]
    ) -> None:
        service, _, _ = _service(prefs, env)
        other = next(
            entry["provider"]
            for entry in service.model_choices()["choices"]
            if not entry["current"]
        )

        listing = service.choose_model(other)

        assert listing["error"] == ""
        assert listing["current"] == other
        assert prefs.text(LLM_PROVIDER) == other
        assert next(e for e in listing["choices"] if e["current"])["provider"] == other

    def test_an_unknown_model_is_refused_loudly_and_changes_nothing(
        self, prefs: Preferences, env: dict[str, str]
    ) -> None:
        service, _, _ = _service(prefs, env)
        before = service.model_choices()["current"]

        listing = service.choose_model("gpt-nope")

        assert listing["error"], "a picker that silently keeps the old model is a lie"
        assert service.model_choices()["current"] == before

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
        alpha = next(entry for entry in section["choices"] if entry["provider"] == "alpha")
        assert alpha["model"] == "alpha-1"
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
        names = [entry["provider"] for entry in service.model_choices()["choices"]]
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
        assert listing["current"] == "gamma"

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
        assert prefs.text(LLM_MODEL) == ""

    def test_saving_rebuilds_the_client_without_a_restart(
        self, prefs: Preferences, env: dict[str, str]
    ) -> None:
        service, fake_llm, _ = _service(prefs, env)
        before = fake_llm.calls

        service.apply({"target": "alpha", "model": "alpha-2"})

        assert fake_llm.calls > before
        assert fake_llm.override.providers["alpha"].model == "alpha-2"
