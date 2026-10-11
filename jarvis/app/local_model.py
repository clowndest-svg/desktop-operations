"""The 自带模型 as the UI sees it: three verbs, no process management in the shell.

``jarvis.ui`` may not import ``jarvis.llm`` (the layer table says so, and the reason is
that the window is where things get *shown*, not where they get decided), so the desktop
bridge talks to this module and this module is the one that knows llama.cpp lives in
:mod:`jarvis.llm.local_server`.

Nothing here starts a model on its own: ``status`` is what the settings row polls, and a
poll that allocated 5 GB would be the worst surprise in the program.
"""

from __future__ import annotations

from jarvis.llm import local_server


def status() -> dict[str, object]:
    """Phase, port, and the Chinese sentence explaining why not more."""
    return local_server.shared_server().status()


def preflight(model: str, context: int) -> dict[str, object]:
    """Would this machine take *that* model at *that* window? Changes nothing, starts nothing."""
    return local_server.preflight(model, context)


def start() -> dict[str, object]:
    """Load the bundled model now. Blocks while the weights come in.

    Returns the status with ``ok`` and ``error`` filled in, because the button needs to
    know whether to say 在跑 or to show the reason it is not.
    """
    server = local_server.shared_server()
    ok, reason = server.ensure_running()
    return {**server.status(), "ok": ok, "error": reason}


def stop() -> dict[str, object]:
    """Hand the memory back; the next local question starts her up again."""
    server = local_server.shared_server()
    server.stop(reason="界面上停掉了")
    return server.status()
