"""Tests for layered config loading (jarvis.config.loader) and the schema."""

from __future__ import annotations

from pathlib import Path

import pytest

from jarvis.config.loader import (
    deep_merge,
    env_overrides,
    load_config,
    load_defaults,
    load_yaml_file,
    resolve_user_config_file,
)
from jarvis.config.paths import AppPaths
from jarvis.config.schema import AppConfig
from jarvis.core.exceptions import ConfigurationError


@pytest.fixture
def paths(tmp_path: Path) -> AppPaths:
    layout = AppPaths.from_data_dir(tmp_path)
    layout.ensure()
    return layout


class TestDefaults:
    def test_builtin_defaults_are_complete_and_valid(self) -> None:
        config = AppConfig.from_mapping(load_defaults())
        assert config.app.environment == "production"
        assert config.logging.level == "INFO"
        assert config.logging.max_bytes > 0


class TestDeepMerge:
    def test_nested_mappings_merge_and_scalars_replace(self) -> None:
        base = {"a": {"x": 1, "y": 2}, "b": "keep"}
        override = {"a": {"y": 99}}
        merged = deep_merge(base, override)
        assert merged == {"a": {"x": 1, "y": 99}, "b": "keep"}

    def test_override_replaces_mapping_with_scalar(self) -> None:
        merged = deep_merge({"a": {"x": 1}}, {"a": "flat"})
        assert merged == {"a": "flat"}

    def test_inputs_are_not_mutated(self) -> None:
        base = {"a": {"x": 1}}
        deep_merge(base, {"a": {"x": 2}})
        assert base == {"a": {"x": 1}}


class TestEnvOverrides:
    def test_double_underscore_nests_and_yaml_scalars_are_parsed(self) -> None:
        env = {
            "JARVIS_LOGGING__LEVEL": "DEBUG",
            "JARVIS_LOGGING__CONSOLE": "false",
            "JARVIS_LOGGING__BACKUP_COUNT": "9",
            "UNRELATED": "ignored",
        }
        assert env_overrides(env) == {
            "logging": {"level": "DEBUG", "console": False, "backup_count": 9}
        }

    def test_routing_variables_are_not_config_keys(self, tmp_path: Path) -> None:
        env = {"JARVIS_HOME": str(tmp_path), "JARVIS_CONFIG": str(tmp_path / "c.yaml")}
        assert env_overrides(env) == {}

    def test_launcher_helper_variables_are_not_config_keys(self) -> None:
        """``启动小夜.bat`` exports JARVIS_ROOT as the base for its cache paths.

        It is a shell helper, not a setting, and reading it as one made the launcher
        fail with ``unknown key 'root'`` before a window ever opened.
        """
        env = {"JARVIS_ROOT": "E:\\BianChengGongJu\\JarvisData", "JARVIS_WEBSOCKET__PORT": "9"}
        assert env_overrides(env) == {"websocket": {"port": 9}}

    def test_malformed_variable_name_fails_fast(self) -> None:
        with pytest.raises(ConfigurationError):
            env_overrides({"JARVIS_LOGGING__": "x"})


class TestLoadYamlFile:
    def test_invalid_yaml_reports_file(self, tmp_path: Path) -> None:
        bad = tmp_path / "bad.yaml"
        bad.write_text("logging: [unclosed", encoding="utf-8")
        with pytest.raises(ConfigurationError) as excinfo:
            load_yaml_file(bad)
        assert str(bad) in str(excinfo.value)

    def test_non_mapping_top_level_is_rejected(self, tmp_path: Path) -> None:
        bad = tmp_path / "list.yaml"
        bad.write_text("- just\n- a list\n", encoding="utf-8")
        with pytest.raises(ConfigurationError):
            load_yaml_file(bad)

    def test_empty_file_is_an_empty_layer(self, tmp_path: Path) -> None:
        empty = tmp_path / "empty.yaml"
        empty.write_text("", encoding="utf-8")
        assert load_yaml_file(empty) == {}


class TestResolveUserConfigFile:
    def test_missing_default_file_is_fine(self, paths: AppPaths) -> None:
        assert resolve_user_config_file(paths, {}) is None

    def test_default_file_is_picked_up(self, paths: AppPaths) -> None:
        paths.user_config_file.write_text("app:\n  language: en-US\n", encoding="utf-8")
        assert resolve_user_config_file(paths, {}) == paths.user_config_file

    def test_explicit_jarvis_config_must_exist(self, paths: AppPaths, tmp_path: Path) -> None:
        env = {"JARVIS_CONFIG": str(tmp_path / "nope.yaml")}
        with pytest.raises(ConfigurationError):
            resolve_user_config_file(paths, env)


class TestLoadConfig:
    def test_defaults_only(self, paths: AppPaths) -> None:
        config = load_config(paths, {})
        assert config.app.environment == "production"
        assert config.logging.level == "INFO"

    def test_user_file_overrides_defaults(self, paths: AppPaths) -> None:
        paths.user_config_file.write_text("app:\n  environment: development\n", encoding="utf-8")
        config = load_config(paths, {})
        assert config.app.environment == "development"
        assert config.logging.level == "INFO"  # untouched keys keep defaults

    def test_env_beats_user_file(self, paths: AppPaths) -> None:
        paths.user_config_file.write_text("logging:\n  level: WARNING\n", encoding="utf-8")
        config = load_config(paths, {"JARVIS_LOGGING__LEVEL": "ERROR"})
        assert config.logging.level == "ERROR"

    def test_unknown_key_fails_with_key_path(self, paths: AppPaths) -> None:
        paths.user_config_file.write_text("logging:\n  levle: DEBUG\n", encoding="utf-8")
        with pytest.raises(ConfigurationError) as excinfo:
            load_config(paths, {})
        assert "logging.levle" in str(excinfo.value)

    def test_wrong_type_fails_with_key_path(self, paths: AppPaths) -> None:
        paths.user_config_file.write_text("logging:\n  max_bytes: lots\n", encoding="utf-8")
        with pytest.raises(ConfigurationError) as excinfo:
            load_config(paths, {})
        assert "logging.max_bytes" in str(excinfo.value)

    def test_invalid_choice_fails(self, paths: AppPaths) -> None:
        config_text = "app:\n  environment: staging\n"
        paths.user_config_file.write_text(config_text, encoding="utf-8")
        with pytest.raises(ConfigurationError) as excinfo:
            load_config(paths, {})
        assert "app.environment" in str(excinfo.value)

    def test_log_level_is_case_insensitive(self, paths: AppPaths) -> None:
        paths.user_config_file.write_text("logging:\n  level: debug\n", encoding="utf-8")
        assert load_config(paths, {}).logging.level == "DEBUG"
