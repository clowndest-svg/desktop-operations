"""What goes in the prompt vs what is merely registered.

The tier popups promise that 关闭 means the model cannot even ask. The tools are
registered regardless -- the level only refuses at call time -- so without this filter
a fresh install spends a fifth of a 4K local window advertising six things that can
only answer 「已关闭」, and the model then reports to the operator that it tried.

``computer_status`` is the deliberate exception: reading the switch is how she
explains why the other six went away.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from jarvis.config.schema import ToolsSection
from jarvis.tools.disk_cleaner import DiskCleaner
from jarvis.tools.monitor import SystemMonitor
from jarvis.tools.service import ToolService

DESKTOP_TOOLS = {
    "open_app",
    "mouse_move",
    "mouse_click",
    "mouse_scroll",
    "key_press",
    "type_text",
}


class _Gate:
    """A ``ShellGate`` stand-in reading one fixed command-line level."""

    def __init__(self, mode: str) -> None:
        self._mode = mode

    def mode(self) -> str:
        return self._mode

    def audit(self, **_kwargs: Any) -> None:
        pass


class _Desk:
    def __init__(self, enabled: bool, throws: bool = False) -> None:
        self._enabled = enabled
        self._throws = throws

    def stats(self) -> dict[str, object]:
        if self._throws:
            raise RuntimeError("服务正在重启")
        return {"running": True, "enabled": self._enabled, "dry_run": False}

    def __getattr__(self, name: str) -> Any:
        return lambda *_a, **_k: None


def _service(
    tmp_path: Path,
    *,
    shell_mode: str = "off",
    desktop_on: bool = True,
    desktop_throws: bool = False,
    gate_factory: Any = None,
    **extra: Any,
) -> ToolService:
    stub = MagicMock()
    monitor = SystemMonitor(top_processes=5)
    service = ToolService(
        lambda: ToolsSection(True, True, True, True, (), 8000),
        monitor_factory=lambda: monitor,
        cleaner_factory=lambda: DiskCleaner(tmp_path / "audit.jsonl"),
        computer_factory=lambda: _Desk(desktop_on, desktop_throws),
        shell_gate_factory=gate_factory or (lambda: _Gate(shell_mode)),
        speaker=stub,
        reminders=stub,
        memory=stub,
        knowledge=stub,
        monitor=monitor,
        usage=stub,
        proposals=stub,
        settings=stub,
        voices=stub,
        conversations=stub,
        alerts=stub,
        **extra,
    )
    service.start()
    return service


def _advertised(service: ToolService) -> set[str]:
    return {str(tool["function"]["name"]) for tool in service.to_openai_tools()}  # type: ignore[index]


class TestCommandLineLevel:
    def test_off_does_not_advertise_run_powershell(self, tmp_path: Path) -> None:
        service = _service(tmp_path, shell_mode="off")
        assert "run_powershell" not in _advertised(service)
        assert "run_powershell" in {spec.name for spec in service.specs()}

    @pytest.mark.parametrize("mode", ["rehearsal", "user", "admin"])
    def test_any_real_level_brings_it_back(self, tmp_path: Path, mode: str) -> None:
        assert "run_powershell" in _advertised(_service(tmp_path, shell_mode=mode))

    def test_a_gate_that_cannot_be_read_leaves_the_tool_in(self, tmp_path: Path) -> None:
        """Failing *open* here: a capability that silently vanishes is worse than one
        that refuses with a reason the operator can act on."""

        def broken() -> Any:
            raise RuntimeError("偏好读不出来")

        assert "run_powershell" in _advertised(_service(tmp_path, gate_factory=broken))

    def test_no_gate_at_all_means_no_powershell(self, tmp_path: Path) -> None:
        assert "run_powershell" not in _advertised(_service(tmp_path, gate_factory=lambda: None))


class TestDesktopControlLevel:
    def test_closed_tier_hides_the_six_but_not_the_reader(self, tmp_path: Path) -> None:
        advertised = _advertised(_service(tmp_path, desktop_on=False))
        assert advertised.isdisjoint(DESKTOP_TOOLS)
        assert "computer_status" in advertised, "她得有一条能说出为什么那六个没了"

    def test_an_open_tier_advertises_them(self, tmp_path: Path) -> None:
        advertised = _advertised(_service(tmp_path, desktop_on=True))
        assert advertised >= DESKTOP_TOOLS

    def test_a_service_that_throws_while_restarting_keeps_them(self, tmp_path: Path) -> None:
        assert _advertised(_service(tmp_path, desktop_throws=True)) >= DESKTOP_TOOLS


class TestNothingElseIsFiltered:
    def test_the_rest_of_the_surface_is_untouched(self, tmp_path: Path) -> None:
        """The filter must not quietly start deciding other things."""
        service = _service(tmp_path, shell_mode="off", desktop_on=False)
        hidden = {spec.name for spec in service.specs()} - _advertised(service)
        assert hidden == DESKTOP_TOOLS | {"run_powershell"}

    def test_the_two_new_r88_tools_survive_both_switches_being_off(self, tmp_path: Path) -> None:
        """查版本和联网是只读的，不该被任何档位牵连。"""
        advertised = _advertised(_service(tmp_path, shell_mode="off", desktop_on=False))
        assert {"runtime_versions", "search_web", "fetch_url", "system_report"} <= advertised


class TestSurfacesReachTheRegistry:
    """A surface accepted by ``ToolService`` must reach the builder.

    ``phone=`` was taken in ``__init__``, stored, and never forwarded to
    ``build_builtin_tools`` -- so the read-the-phone-screen tool registered nothing while
    the panel and the composition root both believed it existed. Every static check in
    this repo was green, because each one tested a different half of that seam.
    """

    def test_a_phone_reader_brings_its_tool_with_it(self, tmp_path: Path) -> None:
        reader = MagicMock()
        service = _service(tmp_path, mirror=MagicMock(), phone=reader)
        assert "phone_screen_read" in _advertised(service)

    def test_no_reader_drops_exactly_that_one_tool(self, tmp_path: Path) -> None:
        with_reader = _advertised(_service(tmp_path, mirror=MagicMock(), phone=MagicMock()))
        without = _advertised(_service(tmp_path, mirror=MagicMock()))
        assert with_reader - without == {"phone_screen_read"}
        assert "mirror_start" in without, "投屏那三条与读屏那条是两件事，别一起消失"
