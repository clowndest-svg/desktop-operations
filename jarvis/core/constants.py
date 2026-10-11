"""Application-wide constants.

Single source of truth for identifiers that would otherwise be hardcoded
across the codebase (forbidden by project policy). Behavioural values
(paths, model names, thresholds...) do NOT belong here — they go into the
YAML configuration system delivered in phase 3.
"""

from __future__ import annotations

from typing import Final

from jarvis import __version__

APP_NAME: Final[str] = "JARVIS"
"""Product name, used in UI and logs."""

APP_SLUG: Final[str] = "jarvis"
"""Machine-friendly identifier: package name, folders, registry keys."""

USER_AGENT: Final[str] = f"xiaoye/{__version__} (desktop AI assistant)"
"""What outbound HTTP says who we are.

The stdlib default is ``Python-urllib/3.x``, and Cloudflare-fronted gateways answer that
with **403 "error code: 1010"** -- which reaches the operator as "连不上" about a perfectly
healthy endpoint. Measured on this machine: the same curl request flips from 403 to 200 by
changing nothing but this header. ASCII only: a header value must be latin-1 encodable, so
the product's own name in Chinese would fail before the request left the process.
"""

ENV_PREFIX: Final[str] = "JARVIS_"
"""Prefix for every environment variable read by the application."""

DEFAULT_ENCODING: Final[str] = "utf-8"
"""Encoding used for all text I/O unless a format dictates otherwise."""
