"""Loopback HTTP server for the built HUD bundle.

Why this exists: the window used to load ``index.html`` over ``file://``, and
that cannot work. Vite emits an ES module (``<script type="module">``), and the
HTML spec requires module scripts to be fetched with CORS — regardless of any
``crossorigin`` attribute. The origin of a ``file://`` page is ``null``, a local
file sends no ``Access-Control-Allow-Origin``, so the browser blocks the script
and the window renders as an empty near-black page.

The failure is close to undebuggable from the Python side: pywebview reports a
successful window, the log says "opening desktop window", and the actual error
exists only in the webview's own console. Serving the bundle over loopback is the
standard fix for a desktop app that ships a web UI, and it removes the whole
class of ``file://`` restrictions rather than this one instance of it — fonts,
``fetch`` of local JSON and web workers all hit the same wall.

Posture, since this opens a listening socket:

* **127.0.0.1 only.** Never ``0.0.0.0``: a desktop UI has no business being
  reachable from the network.
* **Ephemeral port** (``0``), so nothing can be assumed about where it lives.
* **Rooted at the bundle directory.** ``SimpleHTTPRequestHandler`` refuses ``..``
  traversal on its own, and the root here has no parent to escape into.
* **Directory listing disabled.** The only subdirectory is ``assets/``; listing
  it would disclose the hashed file names to anything that can reach the port.
* **No caching headers.** The assets are hash-named and immutable, but a stale
  ``index.html`` is exactly the bug this module was written to fix, so the server
  never invites the webview to keep one.
"""

from __future__ import annotations

import functools
import http.server
import logging
import threading
from pathlib import Path

logger = logging.getLogger("jarvis.ui.static_server")

HOST: str = "127.0.0.1"
"""Loopback only. A desktop UI must not be reachable from the network."""


class _BundleRequestHandler(http.server.SimpleHTTPRequestHandler):
    """Serves one directory, quietly, with no listing and no caching."""

    def list_directory(self, path: str) -> None:  # type: ignore[override]
        """Refuse directory listings.

        Signature matches the stdlib (which returns an open file object or
        ``None``); returning ``None`` makes the base class answer 404.
        """
        del path
        self.send_error(404, "Not Found")
        return None

    def end_headers(self) -> None:
        """Add no-store so the webview never keeps a stale ``index.html``."""
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def log_message(self, format: str, *args: object) -> None:
        """Route access lines to our logger at DEBUG instead of stderr.

        The stdlib handler writes every request to stderr, which would put a line
        of noise in the console for each of the five bundle files on every start.
        """
        logger.debug("bundle request: %s", format % args)


class BundleServer:
    """Serves :data:`jarvis.ui.desktop.WEB_DIR` on an ephemeral loopback port."""

    def __init__(self, directory: Path) -> None:
        self._directory = directory
        self._server: http.server.ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> str:
        """Bind and serve in a background thread; returns the base URL.

        Raises:
            OSError: if the socket cannot be bound. Callers turn this into a
                readable message rather than a black window.
        """
        if self._server is not None:
            return self.url
        handler = functools.partial(_BundleRequestHandler, directory=str(self._directory))
        server = http.server.ThreadingHTTPServer((HOST, 0), handler)
        server.daemon_threads = True
        thread = threading.Thread(
            target=server.serve_forever,
            name="jarvis-ui-http",
            daemon=True,
        )
        thread.start()
        self._server = server
        self._thread = thread
        logger.info("HUD bundle served at %s", self.url)
        return self.url

    def stop(self) -> None:
        """Shut the server down and wait for its thread. Safe to call twice."""
        server, thread = self._server, self._thread
        self._server = None
        self._thread = None
        if server is None:
            return
        server.shutdown()
        server.server_close()
        if thread is not None:
            thread.join(timeout=5.0)
        logger.debug("HUD bundle server stopped")

    @property
    def running(self) -> bool:
        """Whether the server is accepting connections."""
        return self._server is not None

    @property
    def url(self) -> str:
        """Base URL of the running server.

        Built from :data:`HOST` and ``server_port`` rather than from
        ``server_address``: the latter is typed as a union of a string and a
        bytes form, and formatting the bytes branch would produce a URL with a
        ``b'...'`` in it.

        Raises:
            RuntimeError: if :meth:`start` has not run or already stopped.
        """
        server = self._server
        if server is None:
            raise RuntimeError("界面服务未启动")
        return f"http://{HOST}:{server.server_port}/"

    def __enter__(self) -> BundleServer:
        self.start()
        return self

    def __exit__(self, *_: object) -> None:
        self.stop()


__all__ = ["HOST", "BundleServer"]
