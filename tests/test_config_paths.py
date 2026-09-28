"""Tests for the data-directory layout (jarvis.config.paths)."""

from __future__ import annotations

from pathlib import Path

from jarvis.config.paths import (
    AppPaths,
    default_data_dir,
    export_model_cache_env,
)


class TestDefaultDataDir:
    def test_jarvis_home_wins(self, tmp_path: Path) -> None:
        env = {"JARVIS_HOME": str(tmp_path / "custom"), "LOCALAPPDATA": str(tmp_path / "lad")}
        assert default_data_dir(env) == tmp_path / "custom"

    def test_localappdata_is_used_on_windows(self, tmp_path: Path) -> None:
        env = {"LOCALAPPDATA": str(tmp_path / "AppData" / "Local")}
        assert default_data_dir(env) == tmp_path / "AppData" / "Local" / "Jarvis"

    def test_falls_back_to_home_dotdir(self) -> None:
        assert default_data_dir({}) == Path.home() / ".jarvis"

    def test_blank_override_is_ignored(self, tmp_path: Path) -> None:
        env = {"JARVIS_HOME": "   ", "LOCALAPPDATA": str(tmp_path)}
        assert default_data_dir(env) == tmp_path / "Jarvis"


class TestAppPaths:
    def test_layout_is_under_data_dir(self, tmp_path: Path) -> None:
        paths = AppPaths.from_data_dir(tmp_path)
        assert paths.config_dir == tmp_path / "config"
        assert paths.database_dir == tmp_path / "database"
        assert paths.logs_dir == tmp_path / "logs"
        assert paths.models_dir == tmp_path / "models"
        assert paths.cache_dir == tmp_path / "cache"
        assert paths.user_config_file == tmp_path / "config" / "config.yaml"

    def test_audit_trail_is_not_inside_the_disposable_cache(self, tmp_path: Path) -> None:
        """The deletion log is evidence, so it must not live where a cleaner sweeps.

        ``cache_dir`` is documented as "safe to delete at any time" -- which is
        exactly what the cleaning feature does to it. An audit trail in there would
        answer "what did you delete last week" with "we can no longer say".
        """
        paths = AppPaths.from_data_dir(tmp_path)
        assert paths.audit_dir == tmp_path / "audit"
        assert paths.audit_dir != paths.cache_dir
        assert paths.cache_dir not in paths.audit_dir.parents

    def test_ensure_creates_all_directories_idempotently(self, tmp_path: Path) -> None:
        paths = AppPaths.from_data_dir(tmp_path / "root")
        paths.ensure()
        paths.ensure()  # second call must not raise
        for directory in (
            paths.data_dir,
            paths.config_dir,
            paths.database_dir,
            paths.logs_dir,
            paths.models_dir,
            paths.cache_dir,
        ):
            assert directory.is_dir()


class TestModelCacheRedirect:
    """Where the 1.7 GB of weights actually land, and who gets to decide."""

    def test_an_existing_redirect_is_left_alone(self, tmp_path: Path) -> None:
        """The operator's own MODELSCOPE_CACHE wins: overriding it would move
        gigabytes back onto the system drive, which is what they avoided."""
        paths = AppPaths.from_data_dir(tmp_path / "jarvis")
        elsewhere = tmp_path / "big-disk" / "modelscope"
        env = {"MODELSCOPE_CACHE": str(elsewhere)}

        written = export_model_cache_env(paths, env)

        assert "MODELSCOPE_CACHE" not in written
        assert env["MODELSCOPE_CACHE"] == str(elsewhere)
        assert "HF_HOME" in written and "TORCH_HOME" in written

    def test_a_blank_redirect_is_treated_as_no_redirect(self, tmp_path: Path) -> None:
        paths = AppPaths.from_data_dir(tmp_path / "jarvis")
        env = {"MODELSCOPE_CACHE": "   "}

        written = export_model_cache_env(paths, env)

        assert "MODELSCOPE_CACHE" in written
        assert env["MODELSCOPE_CACHE"].endswith("models" + str(__import__("os").sep) + "modelscope")

    def test_the_directories_exist_after_the_redirect(self, tmp_path: Path) -> None:
        """Writing the variable is worthless if the SDK then fails on a missing dir."""
        paths = AppPaths.from_data_dir(tmp_path / "jarvis")
        env: dict[str, str] = {}

        written = export_model_cache_env(paths, env)

        assert written
        for target in written.values():
            assert target.is_dir()

    def test_the_data_directory_owns_the_default(self, tmp_path: Path) -> None:
        """``models_dir`` finally has a consumer: uninstall is one directory."""
        paths = AppPaths.from_data_dir(tmp_path / "jarvis")
        env: dict[str, str] = {}

        written = export_model_cache_env(paths, env)

        for name, target in written.items():
            assert paths.models_dir in target.parents, name
