"""``ToolRegistry`` — the one place tools are registered, listed and called.

Everything that can add a capability goes through here: the built-in set, MCP
bridges, plugins. That is what makes the safety policy enforceable — there is no
second path into the machine that skips :meth:`invoke`.

``invoke`` never raises. Every outcome — unknown tool, policy refusal, bad
arguments, handler crash — comes back as a :class:`~jarvis.tools.types.ToolResult`,
because the callers are a voice pipeline and a JS bridge, and an exception
reaching either of those turns a correctable mistake into a dead turn.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING

from jarvis.core.exceptions import ToolError
from jarvis.tools.policy import ToolPolicy
from jarvis.tools.types import (
    RegisteredTool,
    ToolCall,
    ToolResult,
    ToolSpec,
    validate_arguments,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from jarvis.config.schema import ToolsSection

logger = logging.getLogger("jarvis.tools.registry")

RECENT_CALLS = 40
"""How many finished invocations the registry remembers.

Bounded on purpose. This is "what did it just do", not an audit trail -- the audit
trail is the deletion log on disk, which is kept forever and cannot be turned off.
"""

_DETAIL_CHARS = 120
"""One line of result is enough to recognise an action; more is a second panel."""


def _one_line(text: str) -> str:
    """First non-blank line, shortened. Tool output is for the model, not the HUD."""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped if len(stripped) <= _DETAIL_CHARS else stripped[:_DETAIL_CHARS] + "…"
    return ""


TRUNCATION_NOTE: str = "\n\n…（输出过长已截断）"
"""Appended when a tool's output is cut.

A silent truncation is worse than a visible one: the model would reason about a
half-listed directory as if it were the whole thing.
"""


class ToolRegistry:
    """Holds the registered tools and applies the policy to every call."""

    def __init__(self, settings_provider: Callable[[], ToolsSection]) -> None:
        """Create the registry.

        Args:
            settings_provider: Returns the validated ``tools`` config section.
                A provider rather than the section itself, because the registry
                is constructed before configuration exists.
        """
        self._settings_provider = settings_provider
        self._policy = ToolPolicy(settings_provider)
        self._tools: dict[str, RegisteredTool] = {}
        self._calls: deque[ToolCall] = deque(maxlen=RECENT_CALLS)
        self._lock = threading.RLock()

    # -- registration ------------------------------------------------------

    def register(self, spec: ToolSpec, handler: Callable[[Mapping[str, object]], str]) -> None:
        """Add a tool.

        Registering a name that already exists from the *same* source replaces
        it, so a plugin reload is idempotent. A name collision across sources is
        refused: two different packages both wanting ``read_file`` is a
        configuration problem, and silently letting the later one win would make
        the tool list depend on load order.

        Raises:
            ToolError: on an empty name or a cross-source collision.
        """
        if not spec.name.strip():
            raise ToolError("工具名不能为空")
        with self._lock:
            existing = self._tools.get(spec.name)
            if existing is not None and existing.spec.source != spec.source:
                raise ToolError(
                    f"工具名冲突：{spec.name} 已由 {existing.spec.source} 注册，"
                    f"又被 {spec.source} 占用",
                    details={"name": spec.name, "sources": [existing.spec.source, spec.source]},
                )
            self._tools[spec.name] = RegisteredTool(spec=spec, handler=handler)
        logger.debug("registered tool %s (%s/%s)", spec.name, spec.source, spec.risk.value)

    def unregister(self, name: str) -> bool:
        """Remove one tool. Returns whether it existed."""
        with self._lock:
            return self._tools.pop(name, None) is not None

    def unregister_source(self, source: str) -> int:
        """Remove every tool from one source (plugin unload, MCP disconnect)."""
        with self._lock:
            doomed = [name for name, tool in self._tools.items() if tool.spec.source == source]
            for name in doomed:
                del self._tools[name]
        if doomed:
            logger.info("unregistered %d tool(s) from %s", len(doomed), source)
        return len(doomed)

    def clear(self) -> int:
        """Remove every tool; returns how many were removed."""
        with self._lock:
            count = len(self._tools)
            self._tools.clear()
        return count

    # -- inspection --------------------------------------------------------

    @property
    def policy(self) -> ToolPolicy:
        """The active policy, so callers can resolve paths through the same roots."""
        return self._policy

    def get(self, name: str) -> RegisteredTool | None:
        """Look a tool up by name."""
        with self._lock:
            return self._tools.get(name)

    def specs(self) -> list[ToolSpec]:
        """Every registered spec, sorted by name for a stable tool list.

        Sorted rather than insertion-ordered because the tool list is part of
        the prompt: an unstable order changes the prefix on every boot and
        defeats provider-side prompt caching.
        """
        with self._lock:
            return [self._tools[name].spec for name in sorted(self._tools)]

    def names(self) -> list[str]:
        """Every registered tool name, sorted."""
        with self._lock:
            return sorted(self._tools)

    def to_openai_tools(self) -> list[dict[str, object]]:
        """The ``tools`` array for a chat-completion request."""
        return [spec.to_openai_schema() for spec in self.specs()]

    def stats(self) -> dict[str, object]:
        """Read-only snapshot for the HUD and the health check."""
        specs = self.specs()
        by_source: dict[str, int] = {}
        by_risk: dict[str, int] = {}
        for spec in specs:
            by_source[spec.source] = by_source.get(spec.source, 0) + 1
            by_risk[spec.risk.value] = by_risk.get(spec.risk.value, 0) + 1
        return {
            "count": len(specs),
            "by_source": by_source,
            "by_risk": by_risk,
            "policy": self._policy.describe(),
        }

    # -- invocation --------------------------------------------------------

    def invoke(
        self,
        name: str,
        arguments: Mapping[str, object] | None = None,
        *,
        confirmed: bool = False,
    ) -> ToolResult:
        """Call a tool, applying the policy and the argument check first.

        Every outcome is recorded in the recent-call list on the way out -- refusals
        included, because "the assistant tried and was told no" is the half an
        operator most needs to see, and it is the half that is invisible otherwise.

        Args:
            name: Registered tool name.
            arguments: Arguments as the model produced them; may be ``None``.
            confirmed: ``True`` only when a human has approved this specific
                call. Never set from model output.
        """
        started = time.monotonic()
        result = self._dispatch(name, arguments, confirmed=confirmed)
        self._note(name, result, (time.monotonic() - started) * 1000.0)
        return result

    def _dispatch(
        self,
        name: str,
        arguments: Mapping[str, object] | None,
        *,
        confirmed: bool,
    ) -> ToolResult:
        tool = self.get(name)
        if tool is None:
            logger.warning("tool %r is not registered", name)
            return ToolResult(ok=False, error=f"未注册的工具：{name}", tool=name)

        refusal = self._policy.check(tool.spec, confirmed=confirmed)
        if refusal:
            logger.info("tool %s refused by policy", name)
            return ToolResult(ok=False, error=refusal, tool=name)

        payload: Mapping[str, object] = arguments or {}
        problems = validate_arguments(tool.spec, payload)
        if problems:
            return ToolResult(ok=False, error="参数不合法：" + "；".join(problems), tool=name)

        started = time.monotonic()
        try:
            output = tool.handler(payload)
        except ToolError as exc:
            logger.warning("tool %s failed: %s", name, exc)
            return ToolResult(ok=False, error=str(exc), tool=name)
        except Exception as exc:  # a tool bug must not kill the turn
            logger.exception("tool %s raised unexpectedly", name)
            return ToolResult(ok=False, error=f"{type(exc).__name__}: {exc}", tool=name)
        logger.debug("tool %s ok in %dms", name, int((time.monotonic() - started) * 1000))
        return ToolResult(ok=True, output=self._truncate(output), tool=name)

    # -- what the operator can look back at --------------------------------

    def recent(self) -> tuple[ToolCall, ...]:
        """The last calls, newest first."""
        with self._lock:
            return tuple(reversed(self._calls))

    def _note(self, name: str, result: ToolResult, milliseconds: float) -> None:
        entry = ToolCall(
            at=time.time(),
            tool=name,
            ok=result.ok,
            milliseconds=int(milliseconds),
            detail=_one_line(result.output if result.ok else result.error),
        )
        with self._lock:
            self._calls.append(entry)

    def _truncate(self, text: str) -> str:
        """Cap output at ``tools.max_result_chars``.

        One listing of a large directory can otherwise consume the entire
        context window, leaving no room for the question that was asked.
        """
        settings = self._settings_provider()
        limit = settings.max_result_chars
        if limit <= 0 or len(text) <= limit:
            return text
        return text[:limit] + TRUNCATION_NOTE


__all__ = ["TRUNCATION_NOTE", "ToolRegistry"]
