"""Windows data-directory layout for JARVIS.

All runtime artefacts (user config, databases, logs, downloaded models,
caches) live under a single *data root* so that backup, migration and
uninstall are trivial. The root is resolved in this order:

1. ``JARVIS_HOME`` environment variable (explicit override, any platform);
2. ``%LOCALAPPDATA%\\Jarvis`` (standard Windows per-user app data);
3. ``~/.jarvis`` (fallback for non-Windows dev machines / CI).

Nothing in this module performs I/O at import time; directories are only
created when :meth:`AppPaths.ensure` is called (by ``ConfigService.start``).
"""

from __future__ import annotations

import os
from collections.abc import Mapping, MutableMapping
from dataclasses import dataclass
from pathlib import Path

from jarvis.core.constants import APP_NAME, ENV_PREFIX

ENV_HOME: str = f"{ENV_PREFIX}HOME"
"""Environment variable overriding the data root directory."""

ENV_CONFIG_FILE: str = f"{ENV_PREFIX}CONFIG"
"""Environment variable overriding the user config file location."""

USER_CONFIG_FILENAME: str = "config.yaml"
"""Name of the user override file inside ``<data root>/config``."""


def default_data_dir(environ: Mapping[str, str] | None = None) -> Path:
    """Resolve the data root directory (see module docstring for the order).

    Args:
        environ: Environment mapping; defaults to ``os.environ``. Injectable
            for tests so they never depend on the real machine state.
    """
    env = os.environ if environ is None else environ
    override = env.get(ENV_HOME, "").strip()
    if override:
        return Path(override).expanduser()

    local_app_data = env.get("LOCALAPPDATA", "").strip()
    if local_app_data:
        return Path(local_app_data) / APP_NAME.capitalize()

    return Path.home() / ".jarvis"


@dataclass(frozen=True, slots=True)
class AppPaths:
    """Immutable snapshot of every directory/file location JARVIS uses."""

    data_dir: Path
    """Root of all runtime artefacts."""

    config_dir: Path
    """User configuration files (``config.yaml``)."""

    database_dir: Path
    """SQLite databases and FAISS indexes."""

    logs_dir: Path
    """Rotating log files (phase 4)."""

    models_dir: Path
    """Where downloaded local model weights live.

    Not a passive label: :func:`export_model_cache_env` points the third-party
    caches (ModelScope / Hugging Face / Torch) here when the environment has not
    already chosen a location. Before that, this field had no consumer at all and
    a gigabyte of weights silently landed in whatever directory the SDK picked.
    """

    cache_dir: Path
    """Disposable caches; safe to delete at any time."""

    audit_dir: Path
    """Deletion audit trail. Evidence, so it is deliberately *not* under
    ``cache_dir``: a cache is something a cleaning tool is allowed to sweep away,
    and "what did you delete on my machine last week" must survive that."""

    @classmethod
    def from_data_dir(cls, data_dir: Path) -> AppPaths:
        """Build the standard layout underneath ``data_dir``."""
        return cls(
            data_dir=data_dir,
            config_dir=data_dir / "config",
            database_dir=data_dir / "database",
            logs_dir=data_dir / "logs",
            models_dir=data_dir / "models",
            cache_dir=data_dir / "cache",
            audit_dir=data_dir / "audit",
        )

    @classmethod
    def resolve(cls, environ: Mapping[str, str] | None = None) -> AppPaths:
        """Resolve the layout from the environment (``JARVIS_HOME`` aware)."""
        return cls.from_data_dir(default_data_dir(environ))

    @property
    def user_config_file(self) -> Path:
        """Default location of the user override file."""
        return self.config_dir / USER_CONFIG_FILENAME

    def ensure(self) -> None:
        """Create every directory of the layout (idempotent)."""
        for directory in (
            self.data_dir,
            self.config_dir,
            self.database_dir,
            self.logs_dir,
            self.models_dir,
            self.cache_dir,
            self.audit_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)


MODEL_CACHE_ENV_VARS: tuple[str, ...] = ("MODELSCOPE_CACHE", "HF_HOME", "TORCH_HOME")
"""Environment variables that decide where model weights are read from and written to."""


def _cache_subdirectory(paths: AppPaths, name: str) -> Path:
    suffix = {"MODELSCOPE_CACHE": "modelscope", "HF_HOME": "huggingface", "TORCH_HOME": "torch"}
    return paths.models_dir / suffix[name]


def export_model_cache_env(
    paths: AppPaths,
    environ: MutableMapping[str, str] | None = None,
) -> dict[str, Path]:
    """Point the model caches at :attr:`AppPaths.models_dir` unless already set.

    Returns only the variables this call actually wrote. An existing value is left
    alone: on a machine where the operator redirected ModelScope to another disk to
    keep the system drive free, that redirect is the deliberate answer and silently
    overriding it would move 1.7 GB back onto C:.
    """
    env = os.environ if environ is None else environ
    written: dict[str, Path] = {}
    for name in MODEL_CACHE_ENV_VARS:
        if env.get(name, "").strip():
            continue
        target = _cache_subdirectory(paths, name)
        target.mkdir(parents=True, exist_ok=True)
        env[name] = str(target)
        written[name] = target
    return written
