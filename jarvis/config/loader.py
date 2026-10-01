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

from jarvis.config.paths import ENV_CONFIG_FILE, AppPaths
from jarvis.config.schema import AppConfig
from jarvis.core.constants import DEFAULT_ENCODING, ENV_PREFIX
from jarvis.core.exceptions import ConfigurationError

_DEFAULTS_RESOURCE = "defaults.yaml"

# The separator that makes a JARVIS_* name an override: JARVIS_SECTION__KEY.
_OVERRIDE_SEPARATOR = "__"


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

    A ``JARVIS_*`` name with no ``SECTION__KEY`` body is not an override at all --
    it is a routing or launcher variable. ``JARVIS_HOME`` and ``JARVIS_CONFIG`` say
    where the files live, and ``启动小夜.bat`` exports its own ``JARVIS_ROOT`` as the
    base for the model-cache variables. Treating those as configuration used to
    make the double-click launcher die on ``unknown key 'root'`` before it opened
    a window, which is a hard way to learn that a helper variable and a setting
    share a prefix.
    """
    env = os.environ if environ is None else environ
    overrides: dict[str, object] = {}
    for name, raw_value in env.items():
        if not name.startswith(ENV_PREFIX):
            continue
        key_path = name.removeprefix(ENV_PREFIX)
        if _OVERRIDE_SEPARATOR not in key_path:
            continue
        segments = [segment.lower() for segment in key_path.split(_OVERRIDE_SEPARATOR)]
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
