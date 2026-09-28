"""Application-wide constants.

Single source of truth for identifiers that would otherwise be hardcoded
across the codebase (forbidden by project policy). Behavioural values
(paths, model names, thresholds...) do NOT belong here — they go into the
YAML configuration system delivered in phase 3.
"""

from __future__ import annotations

from typing import Final

APP_NAME: Final[str] = "JARVIS"
"""Product name, used in UI and logs."""

APP_SLUG: Final[str] = "jarvis"
"""Machine-friendly identifier: package name, folders, registry keys."""

ENV_PREFIX: Final[str] = "JARVIS_"
"""Prefix for every environment variable read by the application."""

DEFAULT_ENCODING: Final[str] = "utf-8"
"""Encoding used for all text I/O unless a format dictates otherwise."""
