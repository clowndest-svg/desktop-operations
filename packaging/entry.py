"""Frozen entry point for the packaged desktop app.

Two jobs that ``jarvis/__main__.py`` cannot do for itself:

* **Default to the window.** Double-clicking an .exe has to open the HUD; the
  console voice loop is a developer thing and stays behind an explicit
  ``python -m jarvis``.
* **Keep ``__main__`` importable as a module.** PyInstaller runs this file as a
  script, and ``python -m jarvis`` relies on ``__main__`` being a module, so the
  two entry styles stay separate instead of one breaking the other.
"""

from __future__ import annotations

import sys

from jarvis.__main__ import main

if __name__ == "__main__":
    argv = ["--desktop", *sys.argv[1:]]
    sys.exit(main(argv))
