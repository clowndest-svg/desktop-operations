"""The desktop-control tools: what the model can ask for, and what it cannot.

The interesting assertions here are not "a click reaches the controller" -- the
service already tests that. They are the ones about the *seam*: that a policy veto
comes back as readable text instead of an exception, that dry-run never says it did
something, and that ``confirmed`` is not a parameter any tool accepts. That last one
is the whole safety model; a single tool that took a ``confirmed`` argument would let
a prompt talk the assistant into clicking past the gate, and it would do so happily,
because passing ``true`` is what a model does when a boolean looks required.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from jarvis.computer.service import ComputerService
from jarvis.computer.types import PlannedAction
from jarvis.config.schema import ComputerSection
from jarvis.tools.builtins import build_builtin_tools
from jarvis.tools.builtins.computer_tools import build
from jarvis.tools.policy import ToolPolicy
from jarvis.tools.types import RiskLevel, ToolHandler, ToolSpec


def _section(**overrides: object) -> ComputerSection:
    data: dict[str, object] = {
        "enabled": True,
        "dry_run": True,
        "allow_mouse": True,
        "allow_keyboard": True,
        "confirm_dangerous": False,
    }
    data.update(overrides)
    return ComputerSection.from_mapping(data)


class _Recorder:
    """A controller that remembers, and never moves a real cursor."""

    name = "recorder"

    def __init__(self) -> None:
        self.actions: list[PlannedAction] = []

    def execute(self, action: PlannedAction) -> None:
        self.actions.append(action)


def _tools(**overrides: object) -> tuple[dict[str, ToolHandler], dict[str, ToolSpec], _Recorder]:
    recorder = _Recorder()
    service = ComputerService(lambda: _section(**overrides), controller=recorder)
    service.start()
    pairs = build(lambda: service)
    handlers = {spec.name: handler for spec, handler in pairs}
    specs = {spec.name: spec for spec, _ in pairs}
    return handlers, specs, recorder


def _call(handlers: dict[str, ToolHandler], name: str, **arguments: Any) -> str:
    return handlers[name](arguments)


class TestRegistration:
    def test_every_tool_has_a_spec_and_a_handler(self) -> None:
        handlers, specs, _ = _tools()

        assert set(handlers) == set(specs)
        assert {"computer_status", "mouse_move", "mouse_click", "mouse_scroll"} <= set(handlers)

    def test_no_tool_accepts_a_confirmation_flag(self) -> None:
        """The gate only exists if the caller cannot satisfy it themselves."""
        _handlers, specs, _ = _tools()

        for name, spec in specs.items():
            properties = spec.parameters.get("properties") or {}
            assert isinstance(properties, Mapping)
            assert "confirmed" not in properties, f"{name} would let a prompt clear the gate"
            assert "confirm" not in properties, f"{name} would let a prompt clear the gate"

    def test_the_registry_omits_desktop_tools_without_a_service(self) -> None:
        """Advertising a capability nothing can perform is how a model promises the
        operator something that will only ever be refused."""
        policy = ToolPolicy(_tools_policy_section)

        names = {
            spec.name
            for spec, _ in build_builtin_tools(
                policy,
                monitor_factory=_never_called("telemetry"),
                cleaner_factory=_never_called("the cleaner"),
            )
        }

        assert "mouse_click" not in names
        assert "system_report" in names


class TestRefusalsComeBackAsText:
    def test_a_disabled_service_explains_the_switch(self) -> None:
        handlers, _specs, recorder = _tools(enabled=False)

        answer = _call(handlers, "mouse_click", x=10, y=20)

        assert "computer.enabled" in answer
        assert recorder.actions == []

    def test_a_disabled_mouse_explains_its_own_switch(self) -> None:
        handlers, _specs, recorder = _tools(allow_mouse=False)

        answer = _call(handlers, "mouse_click", x=10, y=20)

        assert "allow_mouse" in answer
        assert recorder.actions == []

    def test_bad_coordinates_are_described_not_raised(self) -> None:
        handlers, _specs, _recorder = _tools()

        assert "参数不对" in _call(handlers, "mouse_move", x="left", y=2)

    def test_an_unknown_mouse_button_is_rejected_before_the_controller(self) -> None:
        handlers, _specs, recorder = _tools(dry_run=False)

        answer = _call(handlers, "mouse_click", x=1, y=1, button="side")

        assert "side" in answer
        assert recorder.actions == []

    def test_a_typing_dump_is_refused_on_length_alone(self) -> None:
        """Long text means the model is pasting context into a field, and every
        character of it would land somewhere real."""
        handlers, _specs, recorder = _tools(dry_run=False)

        answer = _call(handlers, "type_text", text="字" * 900)

        assert "400" in answer
        assert recorder.actions == []


class TestExecution:
    def test_dry_run_never_claims_the_cursor_moved(self) -> None:
        handlers, _specs, recorder = _tools(dry_run=True)

        answer = _call(handlers, "mouse_click", x=40, y=50)

        assert "未真实执行" in answer
        assert recorder.actions == [], "dry run must not reach the controller at all"

    def test_a_real_run_reports_what_happened(self) -> None:
        handlers, _specs, recorder = _tools(dry_run=False)

        answer = _call(handlers, "mouse_move", x=40, y=50)

        assert answer.startswith("已执行")
        assert recorder.actions[0].kind.value == "move"

    def test_key_press_is_the_workhorse_without_vision(self) -> None:
        handlers, _specs, recorder = _tools(dry_run=False)

        _call(handlers, "key_press", key="win")

        assert recorder.actions[0].params["key"] == "win"

    def test_status_reads_the_service_not_the_configuration(self) -> None:
        handlers, _specs, _recorder = _tools()

        answer = _call(handlers, "computer_status")

        assert "演练模式" in answer


def _never_called(label: str) -> Callable[[], Any]:
    """A factory the registry must not reach, because reaching it is the bug."""

    def build() -> Any:
        raise AssertionError(f"{label} is not part of this test")

    return build


def _tools_policy_section() -> Any:
    from jarvis.config.schema import ToolsSection

    # Built field by field: the policy now reads ``allow_shell`` to decide what to
    # advertise, and ``from_mapping`` leaves the keys it was not given as ``None``,
    # which the schema rightly refuses to treat as a boolean.
    return ToolsSection(
        enabled=True,
        confirm_dangerous=True,
        allow_write=False,
        allow_shell=False,
        file_roots=(),
        max_result_chars=10_000,
    )


class TestDeclaredRisk:
    """A click is not a read. The registry's own vocabulary has to say so.

    ``--tools`` prints this column to an operator deciding what to leave switched
    on, and ``SAFE`` on ``mouse_click`` is the kind of label that makes the listing
    worse than nothing.
    """

    def test_every_mutating_tool_declares_a_side_effect(self) -> None:
        _handlers, specs, _ = _tools()

        read_only = {"computer_status"}
        for name, spec in specs.items():
            if name in read_only:
                assert spec.risk is RiskLevel.SAFE, f"{name} reads; it should not be flagged"
                continue
            assert spec.risk is RiskLevel.CAUTION, f"{name} moves something real"

    def test_nothing_claims_to_need_a_confirmation_it_cannot_take(self) -> None:
        """DANGEROUS would route through the registry's ``confirmed`` argument, which
        no tool here may accept -- so the label would promise a gate that never fires
        and silently make the whole feature unreachable from a chat turn."""
        _handlers, specs, _ = _tools()

        assert all(spec.risk is not RiskLevel.DANGEROUS for spec in specs.values())
