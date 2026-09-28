"""Plugin system: install / uninstall / enable / disable / hot reload.

Responsibility (delivered in phase 18):
    * Plugin manifest format, sandboxed discovery & loading, lifecycle
      management and hot reload — all without modifying the main program.

Allowed dependencies: ``core``, ``config``, ``tools`` (plugins typically
contribute tools).
"""
