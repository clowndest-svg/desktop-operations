"""ConfigService: the first real lifecycle component of JARVIS.

On :meth:`ConfigService.start` it resolves the directory layout, creates it,
loads + validates the layered configuration and freezes the result. Every
other component receives its settings from this service (constructor
injection by the composition root) instead of reading files or environment
variables itself.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from pathlib import Path

from jarvis.config.loader import (
    deep_merge,
    env_overrides,
    load_config,
    load_defaults,
    load_yaml_file,
    resolve_user_config_file,
)
from jarvis.config.paths import AppPaths, export_model_cache_env
from jarvis.config.schema import AppConfig
from jarvis.core.exceptions import ConfigurationError

logger = logging.getLogger(__name__)


class ConfigService:
    """Loads configuration at startup and exposes it read-only.

    Args:
        config_file: Explicit user config file (e.g. from ``--config``).
            Overrides both ``JARVIS_CONFIG`` and the default location.
        environ: Environment mapping; defaults to ``os.environ``.
            Injectable for tests.
    """

    def __init__(
        self,
        *,
        config_file: Path | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self._explicit_file = config_file
        self._environ: Mapping[str, str] = os.environ if environ is None else environ
        self._config: AppConfig | None = None
        self._paths: AppPaths | None = None

    @property
    def name(self) -> str:
        """Component name used by the composition root."""
        return "config"

    @property
    def config(self) -> AppConfig:
        """The validated configuration. Only available after ``start()``."""
        if self._config is None:
            raise ConfigurationError("configuration accessed before ConfigService.start()")
        return self._config

    @property
    def paths(self) -> AppPaths:
        """The resolved directory layout. Only available after ``start()``."""
        if self._paths is None:
            raise ConfigurationError("paths accessed before ConfigService.start()")
        return self._paths

    def start(self) -> None:
        """Resolve paths, create directories, load and validate config."""
        paths = AppPaths.resolve(self._environ)
        paths.ensure()
        self._redirect_model_cache(paths)

        if self._explicit_file is not None:
            config = self._load_with_explicit_file(self._explicit_file)
            source = str(self._explicit_file)
        else:
            config = load_config(paths, self._environ)
            user_file = resolve_user_config_file(paths, self._environ)
            source = str(user_file) if user_file else "<builtin defaults>"

        self._paths = paths
        self._config = config
        logger.info(
            "configuration loaded (env=%s, user file: %s, data dir: %s)",
            config.app.environment,
            source,
            paths.data_dir,
        )

    def _redirect_model_cache(self, paths: AppPaths) -> None:
        r"""Send model weights to the data directory unless the machine already chose.

        ``AppPaths.models_dir`` used to be a declared path with no consumer, and the
        1.7 GB of SenseVoice weights landed in whatever default each SDK picks --
        ``%USERPROFILE%\.cache`` on Windows. An operator who redirected that to
        another disk keeps their redirect: this only fills the gap where nothing was
        configured.
        """
        # Only the live process environment is written: an injected mapping belongs
        # to a test, and the point of the redirect is that the SDKs imported later
        # in *this* process see it. ``__init__`` defaults to ``os.environ`` itself,
        # so identity -- not ``is None`` -- is what distinguishes the two.
        written: dict[str, Path] = (
            export_model_cache_env(paths, os.environ) if self._environ is os.environ else {}
        )
        if not written:
            return
        for name, target in written.items():
            logger.info("%s -> %s (model cache, chosen by JARVIS)", name, target)
        if paths.models_dir.drive.lower().startswith("c:"):
            logger.warning(
                "model weights will occupy %s on the system drive; set MODELSCOPE_CACHE "
                "(and JARVIS_MODELS_DIR) to redirect them before the first voice run",
                paths.models_dir,
            )

    def stop(self) -> None:
        """Release the frozen snapshot (idempotent)."""
        self._config = None
        self._paths = None

    def _load_with_explicit_file(self, config_file: Path) -> AppConfig:
        """Load defaults + the explicitly given file + env overrides."""
        if not config_file.is_file():
            raise ConfigurationError(
                f"--config points to a missing file: {config_file}",
                details={"file": str(config_file)},
            )
        merged = load_defaults()
        merged = deep_merge(merged, load_yaml_file(config_file))
        merged = deep_merge(merged, env_overrides(self._environ))
        return AppConfig.from_mapping(merged)
