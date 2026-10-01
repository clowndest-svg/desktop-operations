"""One assistant per account, so a tray icon cannot be multiplied by impatience.

Why this exists at all
----------------------
Before the tray, double-clicking the shortcut twice started a second copy and the
first one's window was obvious enough that nobody did it. Now the app can be
*running with nothing on screen*, which is exactly the state that makes a person
click the icon again -- and each copy that starts keeps its own tray icon and tries
to open the microphone. Two processes fighting over one capture device is not a
cosmetic problem: the second one reports 语音启动失败 while the first answers the
wake word, and the operator has no window open to see which is which.

So the second launch does not launch. It wakes the first.

Why a lock file plus a loopback socket, not a named mutex
--------------------------------------------------------
A Win32 named mutex would be one ``ctypes`` call, but it gives the newcomer no way
to tell the incumbent *what to do* -- it can only learn "someone is here", and
"someone is here, and I cannot reach it" is the failure that ends with Task
Manager. A listening socket is both the liveness check and the message: connecting
to it proves the other copy is not merely a pid that has not been reaped yet, and
the one line written to it is the request to show the window. Bound to loopback on
an ephemeral port, and nothing but ``ACTIVATE`` is accepted.

Never use ``os.kill(pid, 0)`` for the liveness check here: on Windows that call
terminates the process rather than probing it. The socket probe above is what makes
this file safe to run on the only platform the desktop shell supports.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import socket
import threading
from collections.abc import Callable
from pathlib import Path

logger = logging.getLogger("jarvis.ui.instance")

ACTIVATE = "ACTIVATE"
"""The only verb the incumbent answers. See the module docstring for why."""

ACK = b"OK\n"

_TIMEOUT_SECONDS = 1.5
"""How long to wait for the other copy. Loopback, an in-process handler: anything
past this is a process that is hung, not one that is slow, and being late is worse
than being the second instance for two more seconds."""


def read_lock(path: Path) -> tuple[int, int] | None:
    """``(pid, port)`` from the lock file, or ``None`` when there is no readable one."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    pid = raw.get("pid")
    port = raw.get("port")
    if not isinstance(pid, int) or not isinstance(port, int) or isinstance(pid, bool):
        return None
    return pid, port


def send_activate(port: int) -> bool:
    """Ask whoever owns the port to show its window. ``False`` means nobody is home."""
    if port <= 0 or port > 65535:
        return False
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=_TIMEOUT_SECONDS) as sock:
            sock.sendall(f"{ACTIVATE}\n".encode("ascii"))
            sock.settimeout(_TIMEOUT_SECONDS)
            return sock.recv(16).startswith(b"OK")
    except OSError:
        return False


class InstanceGate:
    """The claim-or-wake decision, and the small server that answers the wake.

    Two moves, in this order, because the second needs something to call:
    :meth:`claim` decides whether this process gets a window, and :meth:`start` --
    only ever reached by the winner -- listens for the next double-click.
    """

    def __init__(self, lock_file: Path, *, on_activate: Callable[[], object] | None = None) -> None:
        self._path = lock_file
        self.on_activate = on_activate
        self._port = 0
        self._socket: socket.socket | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def port(self) -> int:
        return self._port

    @property
    def owns_lock(self) -> bool:
        return self._socket is not None

    def claim(self) -> bool:
        """Whether this process is the one that gets to open a window.

        ``False`` means "another copy is already running and has just been asked to
        show itself"; the caller must leave, quietly. A lock file pointing at
        nothing answerable -- a crash, a reboot, a port the OS has since handed to
        something else -- is overwritten rather than honoured: refusing to start
        because of a file a dead process left behind is how an app ends up
        unstartable until someone finds and deletes it.
        """
        recorded = read_lock(self._path)
        if recorded is not None and send_activate(recorded[1]):
            logger.info("another copy owns port %s; asked it to show its window", recorded[1])
            return False
        self._bind()
        if not self._write_lock():
            # A concurrent launch can take the file while we are binding. Re-read:
            # if the slot now holds someone reachable, be the guest after all.
            recorded = read_lock(self._path)
            if recorded is not None and recorded[1] != self._port and send_activate(recorded[1]):
                logger.info("lost the lock race; woke the copy that won it")
                self._close_socket()
                return False
            self._write_lock(force=True)
        return True

    def _bind(self) -> None:
        """Grab an ephemeral loopback port and keep it for the life of the process."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))
        sock.listen(4)
        self._socket = sock
        self._port = int(sock.getsockname()[1])

    def _write_lock(self, *, force: bool = False) -> bool:
        """Claim the file. ``False`` means somebody else got there first.

        A leftover file from a crashed or hard-killed copy is the normal case here,
        not an error -- ``claim`` probes it and takes over -- so the refusal is
        returned quietly and only a real I/O failure is worth a traceback.
        """
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            mode = "w" if force else "x"
            with open(self._path, mode, encoding="utf-8") as handle:
                json.dump({"pid": os.getpid(), "port": self._port}, handle)
            return True
        except FileExistsError:
            return False
        except OSError:
            logger.warning("could not write the instance lock at %s", self._path, exc_info=True)
            return False

    def start(self) -> None:
        """Answer future activations. No-op for a process that is not the owner."""
        if self._socket is None or self._thread is not None:
            return
        self._thread = threading.Thread(target=self._serve, name="jarvis-instance", daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        sock = self._socket
        if sock is None:
            return
        sock.settimeout(1.0)
        while not self._stop.is_set():
            try:
                connection, _ = sock.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            self._answer(connection)

    def _answer(self, connection: socket.socket) -> None:
        """One connection, one line, then it is closed. Never raises."""
        try:
            with connection:
                connection.settimeout(_TIMEOUT_SECONDS)
                request = connection.recv(64).decode("utf-8", "replace").strip()
                if request != ACTIVATE:
                    return
                handler = self.on_activate
                if handler is not None:
                    try:
                        handler()
                    except Exception:  # pragma: no cover - a GUI that will not show
                        logger.exception("activation reached a window that would not show")
                connection.sendall(ACK)
        except OSError:
            logger.debug("instance activation connection dropped", exc_info=True)

    def _close_socket(self) -> None:
        sock = self._socket
        self._socket = None
        if sock is not None:
            with contextlib.suppress(OSError):
                sock.close()

    def release(self) -> None:
        """Stop listening and remove the lock file, but only if it is still ours."""
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=_TIMEOUT_SECONDS)
            self._thread = None
        mine = read_lock(self._path) == (os.getpid(), self._port)
        self._close_socket()
        if mine:
            try:
                self._path.unlink()
            except OSError:
                logger.debug("instance lock file was already gone", exc_info=True)


__all__ = ["ACTIVATE", "InstanceGate", "read_lock", "send_activate"]
