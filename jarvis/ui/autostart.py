"""Start with Windows, when the operator asks for it and only then.

Why the registry ``Run`` key
---------------------------
It is the one mechanism that needs no administrator, no scheduled task, no service,
and no second process to keep it alive -- and it is the one a person can inspect and
delete themselves with ``regedit``. A Task Scheduler entry would be more powerful and
much harder to notice.

Why the checkbox reads its state back instead of remembering it
---------------------------------------------------------------
The registry is the truth about whether this will happen at logon. A second copy of
that fact -- in a preference file, or in a variable -- is a second thing that can be
wrong, and the failure is the confusing kind: the menu says 「开机自启」 is on while
Windows has no idea it exists. So ``enabled()`` asks the key, every time.

Scope
-----
``HKEY_CURRENT_USER`` only. ``HKLM`` would put the assistant on every account on the
machine and needs elevation to write; nobody asked for that, and an assistant that
starts under someone else's name is a different privacy conversation than one that
starts under yours.
"""

from __future__ import annotations

import contextlib
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger("jarvis.ui.autostart")

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "XiaoYeDesktop"
"""ASCII on purpose: a Chinese value name works, but it is one more thing that can
look like corruption in ``regedit`` to whoever has to clean this up later."""


def _winreg() -> Any:
    import winreg

    return winreg


def command_for(executable: Path) -> str:
    """The exact string Windows will run. Quoted, because ``E:\\Program Files`` exists."""
    return f'"{Path(executable).as_posix()}"'


def enabled(executable: Path) -> bool:
    """Whether Windows will start this exe at logon. ``False`` on any doubt."""
    wanted = command_for(executable)
    try:
        winreg = _winreg()
    except Exception:  # pragma: no cover - not Windows
        return False
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            current, _kind = winreg.QueryValueEx(key, VALUE_NAME)
    except FileNotFoundError:
        return False
    except OSError:
        logger.debug("could not read the autostart value", exc_info=True)
        return False
    return str(current).strip().strip('"') == wanted.strip().strip('"')


def set_enabled(executable: Path, on: bool) -> tuple[bool, str]:
    """Write or remove the value. Returns the state that is now true, and why not.

    The return value is re-read rather than assumed: a write that a policy blocks
    still looks like a success from here, and the checkbox would then be lying.
    """
    try:
        winreg = _winreg()
    except Exception as exc:  # pragma: no cover - not Windows
        return False, f"这个系统上没有注册表可写：{exc}"
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            if on:
                winreg.SetValueEx(key, VALUE_NAME, 0, winreg.REG_SZ, command_for(executable))
            else:
                # Already absent is exactly what "off" means, not a failure.
                with contextlib.suppress(FileNotFoundError):
                    winreg.DeleteValue(key, VALUE_NAME)
    except OSError as exc:
        logger.warning("could not change the autostart setting: %s", exc)
        return False, f"改不了开机自启：{exc}"
    return enabled(executable), ""


__all__ = ["VALUE_NAME", "command_for", "enabled", "set_enabled"]
