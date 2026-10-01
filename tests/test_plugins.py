"""Tests for the plugin layer (jarvis.plugins).

Plugins are real directories under ``tmp_path`` with real ``plugin.yaml`` files
and real Python modules, because the interesting behaviour *is* the filesystem
and import machinery: a bad manifest must be skipped, a raising plugin must be
isolated, and a reload must re-read the directory. The tool registry is the
real one (driven by a stub config), so a plugin's registration is verified end
to end. Nothing here touches the user's actual plugins folder.
"""

from __future__ import annotations

import logging
import shutil
from collections.abc import Callable, Sequence
from pathlib import Path

import pytest

from jarvis.config.schema import PluginsSection, ToolsSection
from jarvis.core.exceptions import PluginError
from jarvis.plugins import PluginLoader, PluginManifest, PluginService
from jarvis.plugins.service import EntryPointLike
from jarvis.tools import ToolRegistry

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _tools_section() -> ToolsSection:
    return ToolsSection(
        enabled=True,
        confirm_dangerous=True,
        allow_write=False,
        allow_shell=False,
        file_roots=(),
        max_result_chars=10_000,
    )


def _registry() -> ToolRegistry:
    return ToolRegistry(_tools_section)


def _write_plugin(
    root: Path,
    name: str,
    *,
    manifest: str | None = None,
    entrypoint: str = "plugin.py",
    source: str = "",
) -> Path:
    plugin_dir = root / name
    plugin_dir.mkdir(parents=True, exist_ok=True)
    if manifest is not None:
        (plugin_dir / "plugin.yaml").write_text(manifest, encoding="utf-8")
    if source:
        target = plugin_dir / entrypoint
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(source, encoding="utf-8")
    return plugin_dir


def _manifest(name: str, *, entrypoint: str = "plugin.py", enabled: bool | None = None) -> str:
    lines = [
        f"name: {name}",
        "version: 1.2.3",
        "description: 测试插件",
        f"entrypoint: {entrypoint}",
    ]
    if enabled is not None:
        lines.append(f"enabled: {'true' if enabled else 'false'}")
    return "\n".join(lines) + "\n"


def _register_source(name: str, tool: str | None = None) -> str:
    """A plugin module that registers one tool under ``plugin:<name>``.

    The tool name defaults to ``<name>_tool`` so two plugins in the same test
    do not collide on one name — the registry refuses a cross-source collision,
    which is correct behaviour and would otherwise mask what is being tested.
    """
    tool_name = tool or f"{name}_tool"
    return (
        "from jarvis.tools import ToolSpec\n\n"
        "def register(registry):\n"
        f'    registry.register(ToolSpec(name="{tool_name}", description="打招呼", '
        f'source="plugin:{name}"), lambda arguments: "你好")\n'
    )


def _loader(root: Path, registry: ToolRegistry) -> PluginLoader:
    return PluginLoader(root, registry_provider=lambda: registry)


def _service(
    root: Path,
    registry: ToolRegistry,
    *,
    allow_entrypoints: bool = False,
    entry_points_provider: Callable[[], Sequence[EntryPointLike]] | None = None,
) -> PluginService:
    settings = PluginsSection(
        enabled=True,
        directory="plugins",
        allow_entrypoints=allow_entrypoints,
    )
    return PluginService(
        lambda: settings,
        lambda: registry,
        directory_provider=lambda: root,
        entry_points_provider=entry_points_provider,
    )


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


class TestPluginDiscovery:
    def test_reads_manifest_fields(self, tmp_path: Path) -> None:
        plugin_dir = _write_plugin(tmp_path, "demo", manifest=_manifest("demo"))
        manifests = _loader(tmp_path, _registry()).discover()

        assert len(manifests) == 1
        manifest = manifests[0]
        assert manifest.name == "demo"
        assert manifest.version == "1.2.3"
        assert manifest.entrypoint == "plugin.py"
        assert manifest.path == plugin_dir
        assert manifest.enabled

    def test_missing_directory_is_not_an_error(self, tmp_path: Path) -> None:
        """A fresh install has no plugins folder; discovery must return empty
        rather than raise, or the application could not start."""
        assert _loader(tmp_path / "nope", _registry()).discover() == []

    def test_directory_without_a_manifest_is_ignored(self, tmp_path: Path) -> None:
        (tmp_path / "not-a-plugin").mkdir()
        assert _loader(tmp_path, _registry()).discover() == []

    def test_unknown_key_is_rejected_and_named(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A typo in a manifest must point at the exact key, and must not hide
        the plugins next to it."""
        _write_plugin(tmp_path, "bad", manifest=_manifest("bad") + "bogus: 1\n")
        _write_plugin(tmp_path, "good", manifest=_manifest("good"))

        with caplog.at_level(logging.WARNING, logger="jarvis.plugins.loader"):
            manifests = _loader(tmp_path, _registry()).discover()

        assert [manifest.name for manifest in manifests] == ["good"]
        assert "plugin.yaml.bogus" in caplog.text

    def test_missing_required_field_is_skipped(self, tmp_path: Path) -> None:
        _write_plugin(tmp_path, "bad", manifest="name: bad\nversion: 1.0\n")
        assert _loader(tmp_path, _registry()).discover() == []

    def test_disabled_flag_is_read(self, tmp_path: Path) -> None:
        _write_plugin(tmp_path, "disabled", manifest=_manifest("disabled", enabled=False))
        manifests = _loader(tmp_path, _registry()).discover()
        assert manifests[0].enabled is False

    def test_non_boolean_enabled_is_skipped(self, tmp_path: Path) -> None:
        _write_plugin(tmp_path, "bad", manifest=_manifest("bad") + "enabled: yes please\n")
        assert _loader(tmp_path, _registry()).discover() == []


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


class TestPluginLoading:
    def test_load_registers_tools_and_returns_their_names(self, tmp_path: Path) -> None:
        _write_plugin(tmp_path, "demo", manifest=_manifest("demo"), source=_register_source("demo"))
        registry = _registry()
        loader = _loader(tmp_path, registry)

        tools = loader.load(loader.discover()[0])

        assert tools == ["demo_tool"]
        registered = registry.get("demo_tool")
        assert registered is not None
        assert registered.spec.source == "plugin:demo"
        assert registered.handler({}) == "你好"

    def test_dotted_entrypoint_module_is_resolved(self, tmp_path: Path) -> None:
        """``entrypoint: pkg.mod`` is as natural to write as a filename, so both
        shapes have to work."""
        _write_plugin(
            tmp_path,
            "demo",
            manifest=_manifest("demo", entrypoint="pkg.mod"),
            entrypoint="pkg/mod.py",
            source=_register_source("demo"),
        )
        registry = _registry()
        loader = _loader(tmp_path, registry)

        assert loader.load(loader.discover()[0]) == ["demo_tool"]

    def test_module_without_register_is_reported(self, tmp_path: Path) -> None:
        _write_plugin(tmp_path, "demo", manifest=_manifest("demo"), source="x = 1\n")
        loader = _loader(tmp_path, _registry())
        with pytest.raises(PluginError, match="register"):
            loader.load(loader.discover()[0])

    def test_syntax_error_is_reported_as_plugin_error(self, tmp_path: Path) -> None:
        _write_plugin(tmp_path, "demo", manifest=_manifest("demo"), source="def register(:\n")
        loader = _loader(tmp_path, _registry())
        with pytest.raises(PluginError, match="导入插件"):
            loader.load(loader.discover()[0])

    def test_raising_register_is_rolled_back(self, tmp_path: Path) -> None:
        """A plugin that registers one tool and then throws must leave no trace:
        a half-loaded plugin is worse than one that failed cleanly."""
        source = (
            "from jarvis.tools import ToolSpec\n\n"
            "def register(registry):\n"
            '    registry.register(ToolSpec(name="half", description="", source="plugin:demo"),'
            " lambda arguments: 'x')\n"
            "    raise RuntimeError('boom')\n"
        )
        _write_plugin(tmp_path, "demo", manifest=_manifest("demo"), source=source)
        registry = _registry()
        loader = _loader(tmp_path, registry)

        with pytest.raises(PluginError, match="boom"):
            loader.load(loader.discover()[0])
        assert registry.names() == []


# ---------------------------------------------------------------------------
# Service lifecycle
# ---------------------------------------------------------------------------


class TestPluginService:
    def test_start_loads_plugins_and_registers_tools(self, tmp_path: Path) -> None:
        _write_plugin(tmp_path, "demo", manifest=_manifest("demo"), source=_register_source("demo"))
        registry = _registry()
        service = _service(tmp_path, registry)

        service.start()

        assert service.running
        assert registry.names() == ["demo_tool"]
        records = service.plugins()
        assert len(records) == 1
        assert records[0].loaded
        assert records[0].tools == ("demo_tool",)

    def test_broken_plugin_does_not_stop_the_others(self, tmp_path: Path) -> None:
        _write_plugin(tmp_path, "good", manifest=_manifest("good"), source=_register_source("good"))
        _write_plugin(tmp_path, "bad", manifest=_manifest("bad"), source="def register(:\n")
        registry = _registry()
        service = _service(tmp_path, registry)

        service.start()

        assert registry.names() == ["good_tool"]
        records = {record.manifest.name: record for record in service.plugins()}
        assert records["good"].loaded
        assert not records["bad"].loaded
        assert records["bad"].error

    def test_disabled_plugin_is_listed_but_not_imported(self, tmp_path: Path) -> None:
        _write_plugin(
            tmp_path,
            "disabled",
            manifest=_manifest("disabled", enabled=False),
            source=_register_source("disabled"),
        )
        registry = _registry()
        service = _service(tmp_path, registry)

        service.start()

        record = service.plugins()[0]
        assert record.manifest.enabled is False
        assert not record.loaded
        assert registry.names() == []

    def test_loading_logs_the_path_it_imports(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Loading runs arbitrary third-party code; the log must answer "what
        ran on my machine"."""
        plugin_dir = _write_plugin(
            tmp_path, "demo", manifest=_manifest("demo"), source=_register_source("demo")
        )
        service = _service(tmp_path, _registry())

        with caplog.at_level(logging.INFO, logger="jarvis.plugins.service"):
            service.start()

        assert str(plugin_dir) in caplog.text
        assert "demo" in caplog.text

    def test_reload_picks_up_a_newly_added_plugin(self, tmp_path: Path) -> None:
        _write_plugin(tmp_path, "one", manifest=_manifest("one"), source=_register_source("one"))
        registry = _registry()
        service = _service(tmp_path, registry)
        service.start()
        assert registry.names() == ["one_tool"]

        _write_plugin(tmp_path, "two", manifest=_manifest("two"), source=_register_source("two"))
        records = service.reload()

        assert {record.manifest.name for record in records} == {"one", "two"}
        assert all(record.loaded for record in records)
        assert registry.names() == ["one_tool", "two_tool"]

    def test_reload_drops_tools_from_a_removed_plugin(self, tmp_path: Path) -> None:
        first = _write_plugin(
            tmp_path, "one", manifest=_manifest("one"), source=_register_source("one")
        )
        _write_plugin(tmp_path, "two", manifest=_manifest("two"), source=_register_source("two"))
        registry = _registry()
        service = _service(tmp_path, registry)
        service.start()

        shutil.rmtree(first)
        records = service.reload()

        assert [record.manifest.name for record in records] == ["two"]
        # The removed plugin's tool is gone; the surviving one re-registered
        # cleanly because its source was unregistered before re-loading.
        assert registry.names() == ["two_tool"]
        surviving = registry.get("two_tool")
        assert surviving is not None
        assert surviving.spec.source == "plugin:two"

    def test_stop_unregisters_every_plugin_tool(self, tmp_path: Path) -> None:
        _write_plugin(tmp_path, "demo", manifest=_manifest("demo"), source=_register_source("demo"))
        registry = _registry()
        service = _service(tmp_path, registry)
        service.start()
        assert registry.names()

        service.stop()

        assert registry.names() == []
        assert service.plugins() == []
        assert not service.running

    def test_plugins_and_stats_answer_when_the_directory_is_missing(self, tmp_path: Path) -> None:
        service = _service(tmp_path / "absent", _registry())
        service.start()
        assert service.plugins() == []
        stats = service.stats()
        assert stats["installed"] == 0
        assert stats["loaded"] == 0

    def test_disabled_system_does_nothing(self, tmp_path: Path) -> None:
        _write_plugin(tmp_path, "demo", manifest=_manifest("demo"), source=_register_source("demo"))
        registry = _registry()
        settings = PluginsSection(enabled=False, directory="plugins", allow_entrypoints=False)
        service = PluginService(
            lambda: settings,
            lambda: registry,
            directory_provider=lambda: tmp_path,
        )

        service.start()

        assert service.running
        assert registry.names() == []
        assert service.plugins() == []


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------


class FakeEntryPoint:
    """Stands in for :class:`importlib.metadata.EntryPoint`."""

    def __init__(self, name: str, register: object) -> None:
        self.name = name
        self.value = f"fake:{name}"
        self._register = register

    def load(self) -> object:
        return self._register


def _entry_point_register(registry: ToolRegistry) -> None:
    from jarvis.tools import ToolSpec

    registry.register(
        ToolSpec(name="ep_tool", description="入口点工具", source="plugin:ep"),
        lambda arguments: "ok",
    )


class TestEntryPoints:
    def test_entry_points_are_not_scanned_by_default(self, tmp_path: Path) -> None:
        """Scanning entry points runs code from packages the user never chose
        for JARVIS; it must stay off unless explicitly enabled."""
        calls: list[int] = []

        def provider() -> list[EntryPointLike]:
            calls.append(1)
            return [FakeEntryPoint("ep", _entry_point_register)]

        registry = _registry()
        service = _service(tmp_path, registry, entry_points_provider=provider)

        service.start()

        assert calls == []
        assert registry.names() == []

    def test_entry_points_are_loaded_when_allowed(self, tmp_path: Path) -> None:
        registry = _registry()
        service = _service(
            tmp_path,
            registry,
            allow_entrypoints=True,
            entry_points_provider=lambda: [FakeEntryPoint("ep", _entry_point_register)],
        )

        service.start()

        assert registry.names() == ["ep_tool"]
        records = {record.manifest.name: record for record in service.plugins()}
        assert records["ep"].loaded
        assert records["ep"].tools == ("ep_tool",)

    def test_failing_entry_point_is_isolated(self, tmp_path: Path) -> None:
        def explode(registry: ToolRegistry) -> None:
            raise RuntimeError("nope")

        registry = _registry()
        service = _service(
            tmp_path,
            registry,
            allow_entrypoints=True,
            entry_points_provider=lambda: [FakeEntryPoint("ep", explode)],
        )

        service.start()

        record = service.plugins()[0]
        assert not record.loaded
        assert "nope" in record.error


class TestPluginManifestType:
    def test_manifest_serialises_for_the_ui(self, tmp_path: Path) -> None:
        manifest = PluginManifest(
            name="demo",
            version="1.0",
            description="d",
            entrypoint="plugin.py",
            path=tmp_path,
        )
        assert manifest.to_dict()["path"] == str(tmp_path)
        assert manifest.enabled is True
