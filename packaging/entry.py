"""Frozen entry point for the packaged desktop app.

Two jobs that ``jarvis/__main__`` cannot do for itself:

* **Default to the window.** Double-clicking an .exe has to open the HUD; the
  console voice loop is a developer thing and stays behind an explicit
  ``python -m jarvis``.
* **Permit, but never start, the microphone.** ``--voice`` clears the
  configuration gate so the HUD's 「启用语音」 can do anything at all -- without it
  a double-clicked exe is a voice assistant that refuses to speak, which is the
  complaint this whole entry point exists to answer. It does not open the capture
  device: that still takes a press, and after that a remembered one. From source
  the flag stays opt-in, because ``python -m jarvis`` is a console program that
  other modes share.

Keeping the system drive out of the default install is **not** this file's job any
more -- see ``packaging/hooks/runtime_hook_data_root.py`` for why the decision has
to be made before the entry script runs.
"""

from __future__ import annotations

import sys
from collections.abc import Sequence

from jarvis.__main__ import main


def build_argv(rest: Sequence[str]) -> list[str]:
    """The flags a double-clicked .exe runs with, ahead of whatever was passed in.

    Pinned by a test rather than trusted: the shipped program and the developer's
    ``python -m jarvis`` differ by exactly these two tokens, and when they drift the
    only symptom is a feature that works on one machine and refuses on the other.
    """
    return ["--desktop", "--voice", *rest]


if __name__ == "__main__":
    sys.exit(main(build_argv(sys.argv[1:])))
