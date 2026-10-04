"""The tool surface as one set: what the desktop gets, what the CLI does not.

Each of these tools has its own real-service test elsewhere. This file pins the
assembler instead, because the failure mode it is guarding is a wiring one: a service
handed to ``ToolService`` but never forwarded to ``build_builtin_tools`` -- which leaves
every unit test green while the model simply cannot see the panel.

The counts are asserted as a *set of names*, not a number, so adding an unrelated tool
somewhere else does not make this red for the wrong reason.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

from jarvis.tools.builtins import build_builtin_tools
from jarvis.tools.disk_cleaner import DiskCleaner
from jarvis.tools.monitor import SystemMonitor
from tests.test_insight_tools import _FakePs
from tests.test_tools_process_control import FakeProcess

PANEL_TOOLS: frozenset[str] = frozenset(
    {
        "memory_search",
        "remember",
        "knowledge_search",
        "system_trend",
        "usage_stats",
        "list_processes",
        "propose_kill_process",
        "chat_search",
        "settings_read",
        "settings_apply",
        "voice_pick",
    }
)


class _Surface:
    """Every backing object, present and cooperative, as far as a tool reads it."""

    def __init__(self, name: str) -> None:
        self._name = name

    @property
    def running(self) -> bool:
        return True

    @property
    def enabled(self) -> bool:
        return True


class _Memory(_Surface):
    def recall(self, query: str, *, top_k: int = 0) -> list[Any]:
        del query, top_k
        return []

    def remember(self, content: str, *, kind: str = "fact", source: str = "") -> Any:
        del content, kind, source
        return _Record()


class _Record:
    def to_dict(self) -> dict[str, object]:
        return {"content": "x", "kind": "fact", "created_at": "1", "updated_at": "1"}


class _Knowledge(_Surface):
    def retrieve(self, query: str, *, top_k: int = 0) -> list[Any]:
        del query, top_k
        return []


class _Ledger(_Surface):
    def summary(self, days: int = 7) -> Any:
        del days

        class S:
            def to_dict(self) -> dict[str, object]:
                return {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0}

        return S()

    def daily(self, days: int = 7) -> list[dict[str, object]]:
        del days
        return []

    def by_provider(self, days: int = 7) -> list[dict[str, object]]:
        del days
        return []


class _Settings(_Surface):
    def snapshot(self) -> Mapping[str, object]:
        return {}

    def apply(self, patch: dict[str, Any], *, by: str = "operator") -> Mapping[str, object]:
        del patch, by
        return {}


class _Voices(_Surface):
    def voices(self) -> Mapping[str, object]:
        return {"current": "a", "choices": []}

    def pick(self, voice_id: object) -> Mapping[str, object]:
        del voice_id
        return {"current": "", "error": "n/a"}


class _Conversations(_Surface):
    def search(self, query: str, *, limit: int = 8) -> list[dict[str, object]]:
        del query, limit
        return []


class _Proposals(_Surface):
    def propose(self, pid: int, name: str, reason: str = "") -> Mapping[str, object]:
        del pid, reason
        return {"ok": True, "name": name, "error": ""}


def _monitor() -> SystemMonitor:
    process = FakeProcess(1, "a.exe")
    ps: Any = _FakePs([0.0, 0.0])
    ps.process_iter = lambda attrs: [process]
    return SystemMonitor(top_processes=5, psutil_module=ps)


def _names(tmp_path: Path, **injected: Any) -> set[str]:
    built = build_builtin_tools(
        cast(Any, _Policy()),
        monitor_factory=lambda: _monitor(),
        cleaner_factory=lambda: DiskCleaner(tmp_path / "audit.jsonl", sources={}),
        **injected,
    )
    return {spec.name for spec, _ in built}


class _Policy:
    """``ToolPolicy`` narrowed to the two reads the assembler makes."""

    allows_shell = False

    def file_roots(self) -> tuple[Any, ...]:
        return ()

    def resolve_path(self, raw: str) -> Any:
        raise AssertionError(f"no tool should resolve a path in this test: {raw}")


def test_the_desktop_gets_every_panel_tool(tmp_path: Path) -> None:
    """One missing forward is exactly what this catches."""
    names = _names(
        tmp_path,
        speaker=_Surface("speaker"),
        reminders=_Surface("reminders"),
        memory=_Memory("memory"),
        knowledge=_Knowledge("knowledge"),
        monitor=_monitor(),
        usage=_Ledger("usage"),
        proposals=_Proposals("proposals"),
        settings=_Settings("settings"),
        voices=_Voices("voices"),
        conversations=_Conversations("conversations"),
    )
    missing = PANEL_TOOLS - names
    assert not missing, f"这些面板能力没被转给模型：{sorted(missing)}"


def test_an_empty_wiring_gets_none_of_them(tmp_path: Path) -> None:
    """The same list, injected as nothing. A tool that appears here anyway is a tool
    that will be advertised with nothing behind it."""
    names = _names(
        tmp_path,
    )
    assert PANEL_TOOLS & names == set()


def test_a_disabled_knowledge_base_or_stopped_memory_advertises_nothing(tmp_path: Path) -> None:
    class Off(_Knowledge):
        enabled = False

    class Stopped(_Memory):
        running = False

    names = _names(tmp_path, memory=Stopped("m"), knowledge=Off("k"))
    assert {"memory_search", "remember", "knowledge_search"} & names == set()
