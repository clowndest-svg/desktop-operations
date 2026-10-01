"""Desktop presentation layer (L5): the pywebview HUD and its state plumbing.

Composition:
    * :mod:`jarvis.ui.desktop` — the window, the JS bridge the page calls, and the
      telemetry/disk/voice entry points it exposes.
    * :mod:`jarvis.ui.state_bridge` — turns voice-pipeline events into immutable
      :class:`~jarvis.ui.state_bridge.UiState` snapshots. Deliberately Qt-free and
      webview-free so it is unit-testable headlessly.
    * :mod:`jarvis.ui.audio_bridge` — the other direction: synthesized speech going
      *into* the page, so the visuals can be measured off the voice rather than
      guessed from a state machine.
    * ``jarvis/ui/web/`` — the built Vue bundle (Vite output). Not in git: rebuild
      with ``python scripts/build_desktop.py`` before packaging a wheel.

The earlier PySide6 chat window lived here too; it was replaced by this HUD and
removed. Its history is in commit ``5474944`` under the same file names.

UI is a pure presentation layer: it reads application services
(:class:`~jarvis.app.system_service.SystemService`,
:class:`~jarvis.app.disk_service.DiskService`, and friends) and contains no
business logic. Allowed dependencies: ``core``, ``config``, ``app``.
"""
