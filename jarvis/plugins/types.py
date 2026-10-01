"""Plugin vocabulary: the on-disk manifest and the runtime record.

A manifest is *data* — a directory, a ``plugin.yaml`` and a module — so adding
a plugin is dropping a folder in the plugins directory, never editing JARVIS.
The record is what the UI reads back: whether the manifest actually loaded, the
tools it contributed, and the error when it did not. Separating the two means a
broken plugin still appears in the list (with its error) instead of vanishing.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class PluginManifest:
    """One plugin as declared by its ``plugin.yaml``."""

    name: str
    """Stable identifier, also used as the ``plugin:<name>`` tool source."""

    version: str
    """Declared version, shown in the UI; JARVIS does not interpret it."""

    description: str
    """Human-readable summary (may be empty)."""

    entrypoint: str
    """Module to import, relative to the plugin directory — either a file
    (``plugin.py``) or a dotted module (``pkg.mod``)."""

    path: Path
    """Directory the plugin was discovered in."""

    enabled: bool = True
    """Disabled plugins are listed but not imported."""

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "version": self.version,
            "description": self.description,
            "entrypoint": self.entrypoint,
            "path": str(self.path),
            "enabled": self.enabled,
        }


@dataclass(frozen=True, slots=True)
class PluginRecord:
    """The outcome of loading one plugin."""

    manifest: PluginManifest
    loaded: bool
    tools: tuple[str, ...] = ()
    """Names of the tools the plugin registered (empty when it contributed none)."""

    error: str = ""
    """Empty on success; the reason the plugin was skipped otherwise."""

    def to_dict(self) -> dict[str, object]:
        return {
            "manifest": self.manifest.to_dict(),
            "loaded": self.loaded,
            "tools": list(self.tools),
            "error": self.error,
        }


__all__ = ["PluginManifest", "PluginRecord"]
