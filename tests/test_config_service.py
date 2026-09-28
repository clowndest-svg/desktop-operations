"""Tests for ConfigService lifecycle behaviour (jarvis.config.service)."""

from __future__ import annotations

from pathlib import Path

import pytest

from jarvis.app.application import Application
from jarvis.config.service import ConfigService
from jarvis.core.exceptions import ConfigurationError


def _env_for(tmp_path: Path) -> dict[str, str]:
    return {"JARVIS_HOME": str(tmp_path / "jarvis-data")}


def test_starting_against_the_live_environment_redirects_model_caches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    r"""The default data root must actually receive the weights.

    ``AppPaths.models_dir`` existed for phases without ever being read, so a
    gigabyte of weights landed in ``~\.cache`` instead -- invisible to backup,
    uninstall and the disk cleaner's idea of the application's own directory.
    """
    import os

    from jarvis.config.paths import MODEL_CACHE_ENV_VARS

    for name in MODEL_CACHE_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "live-data"))

    service = ConfigService()  # no injected environ -> os.environ, the real path
    service.start()
    try:
        for name in MODEL_CACHE_ENV_VARS:
            assert os.environ[name].startswith(str(tmp_path / "live-data")), name
    finally:
        service.stop()


def test_an_existing_model_cache_redirect_is_not_overwritten(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An operator who moved the weights off the system drive keeps that choice."""
    import os

    keeper = tmp_path / "elsewhere"
    monkeypatch.setenv("MODELSCOPE_CACHE", str(keeper))
    monkeypatch.delenv("HF_HOME", raising=False)
    monkeypatch.delenv("TORCH_HOME", raising=False)
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "live-data"))

    service = ConfigService()
    service.start()
    try:
        assert os.environ["MODELSCOPE_CACHE"] == str(keeper)
    finally:
        service.stop()


class TestConfigServiceLifecycle:
    def test_start_creates_layout_and_loads_defaults(self, tmp_path: Path) -> None:
        service = ConfigService(environ=_env_for(tmp_path))
        service.start()
        assert service.config.app.environment == "production"
        assert service.paths.data_dir == tmp_path / "jarvis-data"
        assert service.paths.logs_dir.is_dir()
        service.stop()

    def test_access_before_start_raises(self) -> None:
        service = ConfigService(environ={})
        with pytest.raises(ConfigurationError):
            _ = service.config
        with pytest.raises(ConfigurationError):
            _ = service.paths

    def test_stop_is_idempotent_and_clears_state(self, tmp_path: Path) -> None:
        service = ConfigService(environ=_env_for(tmp_path))
        service.start()
        service.stop()
        service.stop()
        with pytest.raises(ConfigurationError):
            _ = service.config

    def test_explicit_config_file_wins(self, tmp_path: Path) -> None:
        explicit = tmp_path / "special.yaml"
        explicit.write_text("app:\n  environment: development\n", encoding="utf-8")
        service = ConfigService(config_file=explicit, environ=_env_for(tmp_path))
        service.start()
        assert service.config.app.environment == "development"
        service.stop()

    def test_explicit_config_file_missing_fails_fast(self, tmp_path: Path) -> None:
        service = ConfigService(config_file=tmp_path / "missing.yaml", environ=_env_for(tmp_path))
        with pytest.raises(ConfigurationError):
            service.start()

    def test_env_override_reaches_service(self, tmp_path: Path) -> None:
        env = _env_for(tmp_path) | {"JARVIS_LOGGING__LEVEL": "DEBUG"}
        service = ConfigService(environ=env)
        service.start()
        assert service.config.logging.level == "DEBUG"
        service.stop()


class TestConfigServiceInApplication:
    def test_registers_and_runs_inside_composition_root(self, tmp_path: Path) -> None:
        app = Application()
        service = ConfigService(environ=_env_for(tmp_path))
        app.register(service)
        app.start()
        assert service.config.logging.console is True
        app.stop()
        with pytest.raises(ConfigurationError):
            _ = service.config  # released on stop
