"""JARVIS logging system (L1 infrastructure).

Responsibility
    Configure the standard :mod:`logging` machinery for the whole process:
    console + rotating-file handlers, third-party noise suppression,
    structured metric fields (latency / tokens / cost) and a last-resort
    hook that records uncaught exceptions before the process dies.

Allowed dependencies
    ``core``, ``config`` (see ``docs/architecture.md``).

Delivery
    Phase 4.

Notes
    The package intentionally shares its name with the standard library
    module. Under Python 3 absolute imports this is unambiguous:
    ``import logging`` inside this package always resolves to the standard
    library, while external code imports us as ``jarvis.logging``.

Public API
    - :class:`LoggingService` — lifecycle component owning handler setup.
    - :class:`LoggingSettings` — frozen settings snapshot it consumes.
    - :func:`metrics` — build ``extra=`` mappings carrying metric fields.
    - :data:`LOG_FORMAT` / :class:`StructuredFormatter` — record layout.
"""

from jarvis.logging.formats import LOG_FORMAT, StructuredFormatter, metrics
from jarvis.logging.service import LoggingService, LoggingSettings

__all__ = [
    "LOG_FORMAT",
    "LoggingService",
    "LoggingSettings",
    "StructuredFormatter",
    "metrics",
]
