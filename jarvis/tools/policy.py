"""Tool safety policy: the only place that decides whether a call may proceed.

Two independent gates, and both have to open:

* **Risk** — ``DANGEROUS`` tools need an explicit ``confirmed=True`` from the
  caller *and* ``tools.confirm_dangerous`` to still be on. A model cannot open
  this gate by itself; confirmation comes from the human-facing layer.
* **Permission** — a tool that declares ``write`` is refused outright while
  ``tools.allow_write`` is false, confirmed or not. This is the switch that
  keeps a model from rewriting documents on a machine where the operator never
  asked for that.

Path containment is the third rule and the subtle one: a tool that takes a path
argument must not be able to reach outside the configured roots, and it must not
be fooled by ``..`` or by a symlink. Resolution happens before the check, never
after.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from jarvis.core.exceptions import ToolExecutionError
from jarvis.tools.types import (
    PERMISSION_SHELL,
    PERMISSION_WRITE,
    RiskLevel,
    ToolSpec,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from jarvis.config.schema import ToolsSection

logger = logging.getLogger("jarvis.tools.policy")


class ToolPolicy:
    """Applies the ``tools`` config section to individual invocations."""

    def __init__(self, settings_provider: Callable[[], ToolsSection]) -> None:
        self._settings_provider = settings_provider

    # -- gates -------------------------------------------------------------

    def check(self, spec: ToolSpec, *, confirmed: bool) -> str:
        """Return a refusal message, or ``""`` when the call may proceed.

        A message rather than an exception: the registry turns it into a
        :class:`~jarvis.tools.types.ToolResult` so the model can be told what
        happened and try something else, instead of the turn dying.
        """
        settings = self._settings_provider()
        if PERMISSION_SHELL in spec.permissions and not settings.allow_shell:
            return (
                f"工具 {spec.name} 需要执行系统命令，但配置里 tools.allow_shell 为 false。"
                "请让操作者显式开启后再试。"
            )
        if PERMISSION_WRITE in spec.permissions and not settings.allow_write:
            return (
                f"工具 {spec.name} 会写入磁盘，但配置里 tools.allow_write 为 false。"
                "请让操作者显式开启后再试。"
            )
        if spec.risk is RiskLevel.DANGEROUS and settings.confirm_dangerous and not confirmed:
            return f"工具 {spec.name} 属于危险操作，需要用户确认后才会执行。"
        return ""

    @property
    def allows_shell(self) -> bool:
        """Whether the config lets any command run at all.

        The registry asks this before advertising ``run_shell``: a tool that
        answers '配置里 tools.allow_shell 为 false' every single time teaches the
        model that the capability exists and sends it looking for a door that
        is not there. Same rule the desktop and PowerShell tools already follow.
        """
        return bool(self._settings_provider().allow_shell)

    # -- paths -------------------------------------------------------------

    def file_roots(self) -> tuple[Path, ...]:
        """Directories tools may touch, resolved.

        Empty config means "the user's home directory": a sensible default that
        is still a boundary, unlike "no restriction", which is what an empty
        list usually ends up meaning.
        """
        settings = self._settings_provider()
        if not settings.file_roots:
            return (Path.home().resolve(),)
        return tuple(Path(root).expanduser().resolve() for root in settings.file_roots)

    def resolve_path(self, raw: str) -> Path:
        """Resolve ``raw`` and refuse it if it escapes every allowed root.

        Two decisions worth stating, because both are the difference between a
        boundary and a suggestion:

        * **A relative path resolves against the first allowed root**, not
          against the process working directory. A desktop app launched from a
          shortcut can have any cwd at all (``C:\\Windows\\System32`` is a real
          example), and resolving against that would put the file somewhere the
          user never meant. Resolving against a root also means a relative path
          is *always* inside a root by construction.
        * **``resolve()`` happens before the containment check.** Doing it the
          other way round lets ``<root>/../../etc`` pass a prefix test and only
          afterwards turn into something else, and lets a symlink point out of
          the tree after the check has already said yes.

        Raises:
            ToolExecutionError: if the path is empty, unparsable, or outside the
                configured roots.
        """
        text = raw.strip()
        if not text:
            raise ToolExecutionError("路径不能为空")
        roots = self.file_roots()
        candidate_path = Path(text).expanduser()
        if not candidate_path.is_absolute():
            candidate_path = roots[0] / candidate_path
        try:
            candidate = candidate_path.resolve()
        except (OSError, RuntimeError) as exc:
            raise ToolExecutionError(f"无法解析路径：{raw}", details={"path": raw}) from exc
        if any(candidate == root or root in candidate.parents for root in roots):
            return candidate
        raise ToolExecutionError(
            f"路径不在允许的目录内：{candidate}",
            details={"path": str(candidate), "allowed": [str(root) for root in roots]},
        )

    def describe(self) -> dict[str, object]:
        """Read-only snapshot of the active policy, for the HUD's panel."""
        settings = self._settings_provider()
        return {
            "allow_write": settings.allow_write,
            "allow_shell": settings.allow_shell,
            "confirm_dangerous": settings.confirm_dangerous,
            "file_roots": [str(root) for root in self.file_roots()],
        }


__all__ = ["ToolPolicy"]
