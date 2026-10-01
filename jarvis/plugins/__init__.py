"""Plugin system: install / uninstall / enable / disable / hot reload.

Responsibility (delivered in phase 18):
    * Plugin manifest format, sandboxed discovery & loading, lifecycle
      management and hot reload — all without modifying the main program.

Allowed dependencies: ``core``, ``config``, ``tools`` (plugins typically
contribute tools).

A plugin is a directory holding a ``plugin.yaml`` and an entrypoint module that
exposes ``register(registry)``. Loading runs that code in-process, so the
service treats a plugin as untrusted: it logs the path it is about to import
and turns every failure into a record rather than an exception.
"""

from jarvis.plugins.loader import PluginLoader, plugin_source
from jarvis.plugins.service import ENTRY_POINT_GROUP, PluginService
from jarvis.plugins.types import PluginManifest, PluginRecord

__all__ = [
    "ENTRY_POINT_GROUP",
    "PluginLoader",
    "PluginManifest",
    "PluginRecord",
    "PluginService",
    "plugin_source",
]
