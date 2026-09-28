"""Layered configuration loading.

Precedence (low -> high):

1. **Built-in defaults** — ``defaults.yaml`` shipped inside the package
   (complete: every known key has a value here);
2. **User file** — ``<data>/config/config.yaml`` or the file named by the
   ``JARVIS_CONFIG`` environment variable (partial: only overrides);
3. **Environment variables** — ``JARVIS_<SECTION>__<KEY>=value`` where a
   double underscore descends one nesting level, e.g.
   ``JARVIS_LOGGING__LEVEL=DEBUG``. Values are parsed as YAML scalars so
   ``true``/``42`` become ``bool``/``int``.

The merged mapping is validated into :class:`~jarvis.config.schema.AppConfig`
(fail-fast, precise key paths). No other package is allowed to read YAML or
``JARVIS_*`` environment variables directly.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from importlib import resources
from pathlib import Path
from typing import cast

import yaml

from jarvis.config.paths import ENV_CONFIG_FILE, ENV_HOME, AppPaths
from jarvis.config.schema import AppConfig
from jarvis.core.constants import DEFAULT_ENCODING, ENV_PREFIX
from jarvis.core.exceptions import ConfigurationError

_DEFAULTS_RESOURCE = "defaults.yaml"

# JARVIS_* variables that steer *where* config lives, not *what* it contains.
_ROUTING_ENV_VARS = frozenset({ENV_HOME, ENV_CONFIG_FILE})


# ---------------------------------------------------------------------------
# Raw-layer helpers
# ---------------------------------------------------------------------------


def _ensure_mapping(value: object, source: str) -> dict[str, object]:
    """YAML documents used as config layers must be mappings at top level."""
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ConfigurationError(
            f"config source '{source}' must contain a YAML mapping at top level",
            details={"source": source, "got": type(value).__name__},
        )
    result: dict[str, object] = {}
    for key, item in value.items():
        if not isinstance(key, str):
            raise ConfigurationError(
                f"config source '{source}' contains a non-string key",
                details={"source": source, "key": repr(key)},
            )
        result[key] = item
    return result


def load_defaults() -> dict[str, object]:
    """Load the built-in defaults shipped with the package."""
    resource = resources.files("jarvis.config").joinpath(_DEFAULTS_RESOURCE)
    text = resource.read_text(encoding=DEFAULT_ENCODING)
    return _ensure_mapping(yaml.safe_load(text), f"builtin:{_DEFAULTS_RESOURCE}")


def load_yaml_file(path: Path) -> dict[str, object]:
    """Load one YAML config file, with actionable error messages."""
    try:
        text = path.read_text(encoding=DEFAULT_ENCODING)
    except OSError as exc:
        raise ConfigurationError(
            f"cannot read config file: {path}",
            details={"file": str(path), "os_error": str(exc)},
        ) from exc
    try:
        parsed = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigurationError(
            f"config file is not valid YAML: {path}",
            details={"file": str(path), "yaml_error": str(exc)},
        ) from exc
    return _ensure_mapping(parsed, str(path))


def deep_merge(base: Mapping[str, object], override: Mapping[str, object]) -> dict[str, object]:
    """Recursively merge ``override`` onto ``base`` (mappings merge, rest replace)."""
    merged: dict[str, object] = dict(base)
    for key, value in override.items():
        existing = merged.get(key)
        if isinstance(existing, Mapping) and isinstance(value, Mapping):
            merged[key] = deep_merge(
                cast(Mapping[str, object], existing),
                cast(Mapping[str, object], value),
            )
        else:
            merged[key] = value
    return merged


def env_overrides(environ: Mapping[str, str] | None = None) -> dict[str, object]:
    """Extract config overrides from ``JARVIS_SECTION__KEY`` variables.

    Key segments are lowercased (env vars are conventionally upper case,
    YAML keys are lower case). Values are parsed as YAML scalars; if parsing
    fails they are kept as raw strings.
    """
    env = os.environ if environ is None else environ
    overrides: dict[str, object] = {}
    for name, raw_value in env.items():
        if not name.startswith(ENV_PREFIX) or name in _ROUTING_ENV_VARS:
            continue
        key_path = name.removeprefix(ENV_PREFIX)
        segments = [segment.lower() for segment in key_path.split("__")]
        if not all(segments):
            raise ConfigurationError(
                f"malformed override variable name: {name}",
                details={"variable": name},
            )
        try:
            value: object = yaml.safe_load(raw_value)
        except yaml.YAMLError:
            value = raw_value
        node: dict[str, object] = overrides
        for segment in segments[:-1]:
            child = node.get(segment)
            if not isinstance(child, dict):
                child = {}
                node[segment] = child
            node = cast(dict[str, object], child)
        node[segments[-1]] = value
    return overrides


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def resolve_user_config_file(
    paths: AppPaths, environ: Mapping[str, str] | None = None
) -> Path | None:
    """Determine which user override file to load, if any.

    ``JARVIS_CONFIG`` (if set) wins and the file MUST exist — a typo in an
    explicit path should never be silently ignored. The default location is
    optional: a fresh install has no user file yet.
    """
    env = os.environ if environ is None else environ
    explicit = env.get(ENV_CONFIG_FILE, "").strip()
    if explicit:
        path = Path(explicit).expanduser()
        if not path.is_file():
            raise ConfigurationError(
                f"{ENV_CONFIG_FILE} points to a missing file: {path}",
                details={"variable": ENV_CONFIG_FILE, "file": str(path)},
            )
        return path
    default_file = paths.user_config_file
    return default_file if default_file.is_file() else None


def load_config(
    paths: AppPaths,
    environ: Mapping[str, str] | None = None,
) -> AppConfig:
    """Load, merge and validate the full configuration.

    Args:
        paths: Resolved directory layout (decides the user file location).
        environ: Environment mapping; defaults to ``os.environ``.

    Raises:
        ConfigurationError: unreadable/invalid YAML, malformed override
            variable, or any schema violation.
    """
    merged = load_defaults()

    user_file = resolve_user_config_file(paths, environ)
    if user_file is not None:
        merged = deep_merge(merged, load_yaml_file(user_file))

    merged = deep_merge(merged, env_overrides(environ))

    return AppConfig.from_mapping(merged)
