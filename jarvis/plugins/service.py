"""``PluginService`` — the lifecycle component for third-party plugins.

Owns discovery, loading, hot reload and teardown of everything under the
plugins directory, plus the optional entry-point scan. The one invariant that
matters: a broken plugin is a *record with an error*, never an exception that
stops the others from loading or the application from starting.
"""

from __future__ import annotations

import importlib.metadata as metadata
import logging
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from jarvis.config import AppPaths
from jarvis.config.schema import PluginsSection
from jarvis.core.exceptions import PluginError
from jarvis.plugins.loader import PluginLoader, plugin_source, source_tool_names
from jarvis.plugins.types import PluginManifest, PluginRecord

if TYPE_CHECKING:  # pragma: no cover - typing only
    from jarvis.tools import ToolRegistry

logger = logging.getLogger("jarvis.plugins.service")

ENTRY_POINT_GROUP = "jarvis.plugins"
"""Distribution entry-point group a plugin may advertise itself under."""


@runtime_checkable
class EntryPointLike(Protocol):
    """The slice of :class:`importlib.metadata.EntryPoint` this service needs.

    A protocol rather than the concrete class so tests inject a fake and the
    entry-point path is exercised without installing a distribution.
    """

    @property
    def name(self) -> str: ...

    @property
    def value(self) -> str: ...

    def load(self) -> object: ...


def default_entry_points() -> list[EntryPointLike]:
    """Read the ``jarvis.plugins`` entry points of installed distributions."""
    return list(metadata.entry_points(group=ENTRY_POINT_GROUP))


class PluginService:
    """Discover, load and hot-reload plugins; contribute their tools."""

    name = "plugins"

    def __init__(
        self,
        settings_provider: Callable[[], PluginsSection],
        registry_provider: Callable[[], ToolRegistry],
        *,
        directory_provider: Callable[[], Path] | None = None,
        loader: PluginLoader | None = None,
        entry_points_provider: Callable[[], Sequence[EntryPointLike]] | None = None,
    ) -> None:
        """Create the service.

        Args:
            settings_provider: Returns the validated ``plugins`` config section.
            registry_provider: Returns the shared tool registry. Injected so
                tests hand in a fake.
            directory_provider: Resolves the plugins folder. Defaults to
                ``<data root>/<plugins.directory>``; injectable so tests point
                at ``tmp_path`` without touching the real data directory.
            loader: Optional loader override; tests inject one whose registry is
                a fake.
            entry_points_provider: Source of ``jarvis.plugins`` entry points.
                Defaults to :func:`default_entry_points`; injectable because the
                real one executes code from installed packages.
        """
        self._settings_provider = settings_provider
        self._registry_provider = registry_provider
        self._directory_provider = directory_provider
        self._loader = loader
        self._entry_points_provider = entry_points_provider
        self._created_loader: PluginLoader | None = None
        self._records: dict[str, PluginRecord] = {}
        self._started = False

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        """Discover and load every plugin (idempotent).

        Off by default in configuration, and the entry-point scan is off even
        when plugins are on: an entry point runs code from a package the user
        never explicitly installed *for JARVIS*.
        """
        if self._started:
            return
        self._started = True
        settings = self._settings_provider()
        if not settings.enabled:
            logger.info("插件系统未启用")
            return
        # Created here, not in the loader: discovery must stay read-only, but an
        # operator needs somewhere to drop a plugin folder.
        self._ensure_directory(settings)
        self._load_all(settings)
        logger.info(
            "插件服务就绪（已加载 %d/%d 个插件）",
            sum(1 for record in self._records.values() if record.loaded),
            len(self._records),
        )

    def stop(self) -> None:
        """Unregister every plugin tool and forget every record (idempotent)."""
        self._unregister_all()
        self._records.clear()
        self._started = False

    def reload(self) -> list[PluginRecord]:
        """Hot reload: drop all plugin tools, then re-discover and re-load.

        Returns the fresh records. Works whether or not :meth:`start` ran, so a
        UI can call it to pick up a newly dropped-in plugin folder.
        """
        self._unregister_all()
        self._records.clear()
        self._created_loader = None
        settings = self._settings_provider()
        if not settings.enabled:
            self._started = False
            return []
        self._started = True
        self._load_all(settings)
        return self.plugins()

    @property
    def running(self) -> bool:
        """Whether :meth:`start` has run."""
        return self._started

    # -- queries -----------------------------------------------------------

    def plugins(self) -> list[PluginRecord]:
        """Every known plugin, sorted by name."""
        return [self._records[name] for name in sorted(self._records)]

    def stats(self) -> dict[str, object]:
        """A JSON-ready snapshot for the UI. Never raises."""
        try:
            settings = self._settings_provider()
            enabled = bool(settings.enabled)
            allow_entrypoints = bool(settings.allow_entrypoints)
        except Exception:  # pragma: no cover - config provider is injected
            enabled = False
            allow_entrypoints = False
        return {
            "enabled": enabled,
            "running": self._started,
            "allow_entrypoints": allow_entrypoints,
            "installed": len(self._records),
            "loaded": sum(1 for record in self._records.values() if record.loaded),
            "tools": sum(len(record.tools) for record in self._records.values()),
            "errors": {
                name: record.error for name, record in sorted(self._records.items()) if record.error
            },
        }

    # -- internals ---------------------------------------------------------

    def _load_all(self, settings: PluginsSection) -> None:
        loader = self._resolve_loader(settings)
        manifests = loader.discover()
        if settings.allow_entrypoints:
            for record in self._load_entry_points():
                self._records[record.manifest.name] = record
        for manifest in manifests:
            if not manifest.enabled:
                logger.info("插件 %s 已禁用，跳过加载", manifest.name)
                self._records[manifest.name] = PluginRecord(manifest=manifest, loaded=False)
                continue
            self._load_one(loader, manifest)

    def _resolve_loader(self, settings: PluginsSection) -> PluginLoader:
        if self._loader is not None:
            return self._loader
        if self._created_loader is None:
            self._created_loader = PluginLoader(
                self._resolve_directory(settings),
                registry_provider=self._registry_provider,
            )
        return self._created_loader

    def _ensure_directory(self, settings: PluginsSection) -> None:
        """Create the plugin folder, tolerating a read-only data root."""
        try:
            self._resolve_directory(settings).mkdir(parents=True, exist_ok=True)
        except OSError as exc:  # pragma: no cover - depends on the filesystem
            logger.warning("无法创建插件目录：%s", exc)

    def _resolve_directory(self, settings: PluginsSection) -> Path:
        if self._directory_provider is not None:
            return self._directory_provider()
        return AppPaths.resolve().data_dir / settings.directory

    def _load_one(self, loader: PluginLoader, manifest: PluginManifest) -> None:
        # Logged *before* the import: this line is the record that arbitrary code
        # from this path ran on the machine.
        logger.info("加载插件 %s v%s 自 %s", manifest.name, manifest.version, manifest.path)
        try:
            tools = loader.load(manifest)
        except PluginError as exc:
            logger.warning("插件 %s 加载失败：%s", manifest.name, exc)
            self._records[manifest.name] = PluginRecord(
                manifest=manifest, loaded=False, error=str(exc)
            )
        except Exception as exc:  # a plugin may raise anything at import time
            logger.exception("插件 %s 加载时发生未预期错误", manifest.name)
            self._records[manifest.name] = PluginRecord(
                manifest=manifest, loaded=False, error=f"{type(exc).__name__}: {exc}"
            )
        else:
            self._records[manifest.name] = PluginRecord(
                manifest=manifest, loaded=True, tools=tuple(tools)
            )

    def _load_entry_points(self) -> list[PluginRecord]:
        provider = self._entry_points_provider or default_entry_points
        try:
            points = list(provider())
        except Exception:  # pragma: no cover - metadata is best effort
            logger.warning("扫描插件入口点失败", exc_info=True)
            return []
        return [self._load_entry_point(point) for point in points]

    def _load_entry_point(self, point: EntryPointLike) -> PluginRecord:
        manifest = PluginManifest(
            name=point.name,
            version="",
            description="",
            entrypoint=point.value,
            path=Path("."),
        )
        logger.info("加载入口点插件 %s（%s）——将执行第三方代码", point.name, point.value)
        try:
            registry = self._registry_provider()
        except Exception:  # pragma: no cover - registry provider is injected
            return PluginRecord(manifest=manifest, loaded=False, error="无法获取工具注册表")
        source = plugin_source(point.name)
        before = source_tool_names(registry, source)
        try:
            loaded = point.load()
            register: object = loaded if callable(loaded) else getattr(loaded, "register", None)
            if not callable(register):
                raise PluginError(f"入口点 {point.name} 未提供可调用的 register")
            register(registry)
        except Exception as exc:
            registry.unregister_source(source)
            logger.warning("入口点插件 %s 加载失败：%s", point.name, exc)
            return PluginRecord(
                manifest=manifest, loaded=False, error=f"{type(exc).__name__}: {exc}"
            )
        after = source_tool_names(registry, source)
        return PluginRecord(manifest=manifest, loaded=True, tools=tuple(sorted(after - before)))

    def _unregister_all(self) -> None:
        if not self._records:
            return
        try:
            registry = self._registry_provider()
        except Exception:  # pragma: no cover - registry provider is injected
            logger.warning("无法获取工具注册表，跳过插件工具注销")
            return
        for name in sorted(self._records):
            try:
                registry.unregister_source(plugin_source(name))
            except Exception:  # pragma: no cover - registry is ours
                logger.warning("注销插件工具失败：%s", name, exc_info=True)


__all__ = ["ENTRY_POINT_GROUP", "EntryPointLike", "PluginService", "default_entry_points"]
