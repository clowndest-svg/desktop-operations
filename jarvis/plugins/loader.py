"""Plugin discovery and loading.

Discovery reads ``plugin.yaml`` from every subdirectory of the plugins folder.
Loading imports the declared module and calls its module-level
``register(registry)``. Both are defensive on purpose: a plugin is untrusted
third-party code, so a malformed manifest or a raising plugin must cost *that
plugin* and nothing else.

Loading executes arbitrary code from the plugin directory, which is why the
loader logs the exact path at INFO before it imports anything — "what ran on my
machine" must be answerable from the log alone.
"""

from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import logging
import sys
from collections.abc import Callable, Mapping
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING

import yaml

from jarvis.core.constants import DEFAULT_ENCODING
from jarvis.core.exceptions import PluginError
from jarvis.plugins.types import PluginManifest
from jarvis.tools import SOURCE_PLUGIN_PREFIX

if TYPE_CHECKING:  # pragma: no cover - typing only
    from jarvis.tools import ToolRegistry

logger = logging.getLogger("jarvis.plugins.loader")

MANIFEST_FILENAME = "plugin.yaml"
"""Name of the manifest inside each plugin directory."""

PLUGIN_SOURCE_PREFIX: str = SOURCE_PLUGIN_PREFIX
"""``ToolSpec.source`` prefix for plugin-provided tools (from ``jarvis.tools``)."""

_REQUIRED_KEYS: tuple[str, ...] = ("name", "version", "entrypoint")
_OPTIONAL_KEYS: tuple[str, ...] = ("description", "enabled")
_ALLOWED_KEYS: frozenset[str] = frozenset((*_REQUIRED_KEYS, *_OPTIONAL_KEYS))


def plugin_source(plugin_name: str) -> str:
    """The ``ToolSpec.source`` value a plugin's tools carry."""
    return f"{PLUGIN_SOURCE_PREFIX}{plugin_name}"


def source_tool_names(registry: ToolRegistry, source: str) -> set[str]:
    """Names of the tools currently registered under ``source``."""
    return {spec.name for spec in registry.specs() if spec.source == source}


class PluginLoader:
    """Discover plugins in a directory and load their entrypoint modules."""

    def __init__(
        self,
        directory: Path,
        *,
        registry_provider: Callable[[], ToolRegistry] | None = None,
    ) -> None:
        """Create the loader.

        Args:
            directory: Folder holding one subdirectory per plugin.
            registry_provider: Returns the registry a plugin's ``register``
                receives. Injected (rather than passed to :meth:`load`) so the
                discovery API stays a pure directory read; ``None`` makes
                :meth:`load` fail loudly instead of importing code it cannot
                register into.
        """
        self._directory = directory
        self._registry_provider = registry_provider

    @property
    def directory(self) -> Path:
        """The directory being scanned."""
        return self._directory

    # -- discovery ---------------------------------------------------------

    def discover(self) -> list[PluginManifest]:
        """Read every valid ``plugin.yaml``; skip the rest with a warning.

        A missing directory is not an error: a fresh install has no plugins and
        must still start. A bad manifest is logged and skipped so one typo does
        not hide the plugins next to it.
        """
        if not self._directory.is_dir():
            logger.debug("插件目录不存在：%s", self._directory)
            return []
        manifests: list[PluginManifest] = []
        for child in sorted(self._directory.iterdir()):
            if not child.is_dir():
                continue
            manifest_file = child / MANIFEST_FILENAME
            if not manifest_file.is_file():
                continue
            try:
                manifests.append(self._read_manifest(manifest_file, child))
            except PluginError as exc:
                logger.warning("跳过插件目录 %s：%s", child, exc)
            except OSError as exc:  # pragma: no cover - unreadable file
                logger.warning("无法读取插件清单 %s：%s", manifest_file, exc)
        return manifests

    # -- loading -----------------------------------------------------------

    def load(self, manifest: PluginManifest) -> list[str]:
        """Import ``manifest`` and call its ``register(registry)``.

        Returns:
            The names of the tools the plugin registered.

        Raises:
            PluginError: if the entrypoint cannot be found/imported, does not
                expose a callable ``register``, or ``register`` raises. Any
                tools it managed to register before failing are rolled back, so
                a half-loaded plugin never leaves stray tools behind.
        """
        registry_provider = self._registry_provider
        if registry_provider is None:
            raise PluginError(
                f"插件加载器缺少工具注册表，无法加载 {manifest.name}",
                details={"plugin": manifest.name},
            )
        registry = registry_provider()
        source = plugin_source(manifest.name)
        before = source_tool_names(registry, source)
        module = self._import_module(manifest)
        register = getattr(module, "register", None)
        if not callable(register):
            raise PluginError(
                f"插件 {manifest.name} 未定义 register(registry) 函数",
                details={"plugin": manifest.name, "path": str(manifest.path)},
            )
        try:
            register(registry)
        except Exception as exc:
            registry.unregister_source(source)
            raise PluginError(
                f"插件 {manifest.name} 的 register 抛出异常：{exc}",
                details={"plugin": manifest.name, "reason": str(exc)},
            ) from exc
        after = source_tool_names(registry, source)
        return sorted(after - before)

    # -- internals ---------------------------------------------------------

    def _import_module(self, manifest: PluginManifest) -> ModuleType:
        module_path = _resolve_entrypoint(manifest.path, manifest.entrypoint)
        module_name = _module_name(manifest)
        spec = importlib.util.spec_from_file_location(module_name, module_path)
        if spec is None or spec.loader is None:
            raise PluginError(
                f"无法为插件 {manifest.name} 创建模块规格",
                details={"plugin": manifest.name, "path": str(module_path)},
            )
        module = importlib.util.module_from_spec(spec)
        _purge_module(module_name)
        sys.modules[module_name] = module
        plugin_dir = str(manifest.path)
        inserted = plugin_dir not in sys.path
        if inserted:
            sys.path.insert(0, plugin_dir)
        try:
            spec.loader.exec_module(module)
        except Exception as exc:
            _purge_module(module_name)
            raise PluginError(
                f"导入插件 {manifest.name} 失败：{exc}",
                details={"plugin": manifest.name, "path": str(module_path)},
            ) from exc
        finally:
            if inserted:
                _remove_path(plugin_dir)
        return module

    def _read_manifest(self, manifest_file: Path, plugin_dir: Path) -> PluginManifest:
        try:
            text = manifest_file.read_text(encoding=DEFAULT_ENCODING)
        except OSError as exc:
            raise PluginError(
                f"无法读取插件清单：{manifest_file}",
                details={"path": str(manifest_file), "reason": str(exc)},
            ) from exc
        try:
            raw: object = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            raise PluginError(
                f"插件清单不是合法 YAML：{manifest_file}",
                details={"path": str(manifest_file), "reason": str(exc)},
            ) from exc
        if not isinstance(raw, Mapping):
            raise PluginError(
                f"插件清单必须是键值映射：{manifest_file}",
                details={"path": str(manifest_file)},
            )
        unknown = sorted(set(raw) - _ALLOWED_KEYS)
        if unknown:
            raise PluginError(
                f"插件清单包含未知字段 {unknown[0]!r}：{manifest_file}",
                details={
                    "path": str(manifest_file),
                    "key": f"{MANIFEST_FILENAME}.{unknown[0]}",
                    "unknown": unknown,
                },
            )
        enabled = raw.get("enabled", True)
        if not isinstance(enabled, bool):
            raise PluginError(
                f"插件清单字段 enabled 必须是布尔值：{manifest_file}",
                details={"path": str(manifest_file), "key": f"{MANIFEST_FILENAME}.enabled"},
            )
        return PluginManifest(
            name=_require_str(raw, "name", manifest_file),
            version=_require_str(raw, "version", manifest_file),
            description=_optional_str(raw, "description", manifest_file),
            entrypoint=_require_str(raw, "entrypoint", manifest_file),
            path=plugin_dir,
            enabled=enabled,
        )


def _require_str(data: Mapping[str, object], key: str, manifest_file: Path) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise PluginError(
            f"插件清单缺少必填字段 {key}：{manifest_file}",
            details={"path": str(manifest_file), "key": f"{MANIFEST_FILENAME}.{key}"},
        )
    return value.strip()


def _optional_str(data: Mapping[str, object], key: str, manifest_file: Path) -> str:
    value = data.get(key, "")
    if not isinstance(value, str):
        raise PluginError(
            f"插件清单字段 {key} 必须是字符串：{manifest_file}",
            details={"path": str(manifest_file), "key": f"{MANIFEST_FILENAME}.{key}"},
        )
    return value.strip()


def _resolve_entrypoint(plugin_dir: Path, entrypoint: str) -> Path:
    """Turn a manifest entrypoint into a module file on disk.

    Two shapes are accepted because both are natural to write: a relative file
    (``plugin.py``, ``sub/mod.py``) and a dotted module (``pkg.mod``). The file
    form is checked first so an entrypoint that happens to contain a dot but is
    really a filename still resolves.
    """
    candidate = plugin_dir / entrypoint
    if candidate.is_file():
        return candidate
    parts = entrypoint.split(".")
    module_file = plugin_dir.joinpath(*parts).with_suffix(".py")
    if module_file.is_file():
        return module_file
    package_init = plugin_dir.joinpath(*parts) / "__init__.py"
    if package_init.is_file():
        return package_init
    raise PluginError(
        f"找不到插件入口模块：{entrypoint}",
        details={"entrypoint": entrypoint, "plugin_dir": str(plugin_dir)},
    )


def _module_name(manifest: PluginManifest) -> str:
    """A stable, unique ``sys.modules`` key for a plugin's top-level module."""
    digest = hashlib.sha1(str(manifest.path).encode("utf-8")).hexdigest()[:8]
    safe = "".join(character if character.isalnum() else "_" for character in manifest.name)
    return f"jarvis_plugin_{safe}_{digest}"


def _purge_module(module_name: str) -> None:
    """Drop a previously loaded copy so a reload re-executes the source."""
    prefix = f"{module_name}."
    for key in [key for key in sys.modules if key == module_name or key.startswith(prefix)]:
        del sys.modules[key]


def _remove_path(path: str) -> None:
    with contextlib.suppress(ValueError):
        sys.path.remove(path)


__all__ = [
    "MANIFEST_FILENAME",
    "PLUGIN_SOURCE_PREFIX",
    "PluginLoader",
    "plugin_source",
    "source_tool_names",
]
