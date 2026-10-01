"""Ending a process: the guards, the identity re-check, and the record.

The interesting cases are all refusals. A kill that works needs no test suite; the
ones that must never happen -- the assistant killing its own window, a recycled PID
turning into ``lsass.exe``, a click that was never confirmed -- are what these pin.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from jarvis.app.process_service import ProcessService
from jarvis.tools.process_control import (
    KillOutcome,
    ProcessControlError,
    ProcessController,
    ProcessTarget,
)


class FakeProcess:
    def __init__(
        self,
        pid: int,
        name: str,
        *,
        access_denied: bool = False,
        never_exits: bool = False,
    ) -> None:
        self.pid = pid
        self._name = name
        self.access_denied = access_denied
        self.never_exits = never_exits
        self.terminated = False
        self.killed = False

    def name(self) -> str:
        return self._name

    def terminate(self) -> None:
        if self.access_denied:
            raise AccessDenied("openprocess")
        self.terminated = True

    def wait(self, timeout: float | None = None) -> None:
        del timeout
        if self.never_exits:
            raise TimeoutError("still running")

    def kill(self) -> None:
        self.killed = True


class AccessDenied(Exception):  # noqa: N818 - the name IS the contract being faked
    """Stands in for ``psutil.AccessDenied``."""


class NoSuchProcess(Exception):  # noqa: N818 - the name IS the contract being faked
    """Stands in for ``psutil.NoSuchProcess``."""


class FakePsutil:
    """A process table the test controls, plus the two exception classes."""

    AccessDenied = AccessDenied
    NoSuchProcess = NoSuchProcess

    def __init__(self, processes: dict[int, FakeProcess]) -> None:
        self.processes = processes

    def Process(self, pid: int) -> FakeProcess:  # noqa: N802 - psutil's spelling
        try:
            return self.processes[pid]
        except KeyError:
            raise NoSuchProcess(pid) from None


def _controller(
    tmp_path: Path,
    processes: dict[int, FakeProcess],
    *,
    own_pids: list[int] | None = None,
) -> ProcessController:
    return ProcessController(
        psutil_module=FakePsutil(processes),
        audit_log=tmp_path / "kills.jsonl",
        own_pids=own_pids if own_pids is not None else [],
    )


def _audited(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


class TestTargets:
    def test_a_wire_row_needs_both_a_pid_and_a_name(self) -> None:
        assert ProcessTarget.from_wire({"pid": 12, "name": "notepad.exe"}) == ProcessTarget(
            12, "notepad.exe"
        )
        assert ProcessTarget.from_wire({"pid": 12}) is None
        assert ProcessTarget.from_wire({"name": "x"}) is None
        assert ProcessTarget.from_wire({"pid": True, "name": "x"}) is None
        assert ProcessTarget.from_wire({"pid": -1, "name": "x"}) is None
        assert ProcessTarget.from_wire("notepad.exe") is None
        assert ProcessTarget.from_wire({"pid": 5, "name": "   "}) is None


class TestRefusals:
    def test_an_unconfirmed_request_raises_and_touches_nothing(self, tmp_path: Path) -> None:
        gone = FakeProcess(11, "a.exe")
        controller = _controller(tmp_path, {11: gone})
        with pytest.raises(ProcessControlError, match="二次确认"):
            controller.kill([ProcessTarget(11, "a.exe")], confirmed=False)
        assert not gone.terminated
        assert _audited(tmp_path / "kills.jsonl") == []

    @pytest.mark.parametrize("name", ["lsass.exe", "svchost.exe", "dwm.exe", "Memory Compression"])
    def test_system_critical_names_are_refused_by_name(self, tmp_path: Path, name: str) -> None:
        alive = FakeProcess(400, name)
        controller = _controller(tmp_path, {400: alive})
        outcomes = controller.kill([ProcessTarget(400, name)], confirmed=True)
        assert outcomes[0].ok is False and "关键" in outcomes[0].note
        assert not alive.terminated
        rows = _audited(tmp_path / "kills.jsonl")
        assert len(rows) == 1 and rows[0]["ok"] is False

    def test_the_assistant_cannot_kill_its_own_window(self, tmp_path: Path) -> None:
        for name in ("小夜.exe", "msedgewebview2.exe", "pythonw.exe"):
            alive = FakeProcess(500, name)
            controller = _controller(tmp_path, {500: alive})
            outcomes = controller.kill([ProcessTarget(500, name)], confirmed=True)
            assert outcomes[0].ok is False, name

    def test_a_pid_in_its_own_tree_is_refused_even_under_another_name(self, tmp_path: Path) -> None:
        alive = FakeProcess(777, "worker.exe")
        controller = _controller(tmp_path, {777: alive}, own_pids=[777])
        outcomes = controller.kill([ProcessTarget(777, "worker.exe")], confirmed=True)
        assert outcomes[0].ok is False and "进程树" in outcomes[0].note


class TestIdentity:
    def test_a_recycled_pid_is_refused_rather_than_killed(self, tmp_path: Path) -> None:
        """The row said ``updater.exe``; the number now belongs to something else.

        This is the whole reason a target carries a name: between drawing the list
        and clicking 确认, Windows can hand the same PID to any process at all.
        """
        now = FakeProcess(900, "lsass.exe")
        controller = _controller(tmp_path, {900: now})
        outcomes = controller.kill([ProcessTarget(900, "updater.exe")], confirmed=True)
        assert outcomes[0].ok is False
        assert "复用" in outcomes[0].note and "lsass.exe" in outcomes[0].note
        assert not now.terminated

    def test_the_name_check_ignores_case(self, tmp_path: Path) -> None:
        alive = FakeProcess(901, "NotePad.EXE")
        controller = _controller(tmp_path, {901: alive})
        outcomes = controller.kill([ProcessTarget(901, "notepad.exe")], confirmed=True)
        assert outcomes[0].ok is True

    def test_a_process_that_already_left_is_reported_not_failed(self, tmp_path: Path) -> None:
        controller = _controller(tmp_path, {})
        outcomes = controller.kill([ProcessTarget(902, "gone.exe")], confirmed=True)
        assert outcomes[0].ok is False and outcomes[0].note == "进程已经不在了"


class TestKilling:
    def test_a_plain_kill_terminates_and_records(self, tmp_path: Path) -> None:
        alive = FakeProcess(1000, "stuck.exe")
        controller = _controller(tmp_path, {1000: alive})
        outcomes = controller.kill([ProcessTarget(1000, "stuck.exe")], confirmed=True)
        assert outcomes == [KillOutcome(1000, "stuck.exe", True, "已结束")]
        assert alive.terminated and not alive.killed
        rows = _audited(tmp_path / "kills.jsonl")
        assert rows[0]["ok"] is True and rows[0]["pid"] == 1000

    def test_a_process_that_ignores_terminate_is_forced(self, tmp_path: Path) -> None:
        alive = FakeProcess(1001, "stubborn.exe", never_exits=True)
        controller = _controller(tmp_path, {1001: alive})
        outcomes = controller.kill([ProcessTarget(1001, "stubborn.exe")], confirmed=True)
        assert outcomes[0].ok is True
        assert alive.terminated and alive.killed

    def test_a_denied_kill_says_who_would_have_to_do_it(self, tmp_path: Path) -> None:
        alive = FakeProcess(1002, "other-user.exe", access_denied=True)
        controller = _controller(tmp_path, {1002: alive})
        outcomes = controller.kill([ProcessTarget(1002, "other-user.exe")], confirmed=True)
        assert outcomes[0].ok is False and "权限不够" in outcomes[0].note
        assert not alive.killed

    def test_every_attempt_is_audited_including_the_refused_ones(self, tmp_path: Path) -> None:
        alive = FakeProcess(1003, "app.exe")
        controller = _controller(tmp_path, {1003: alive})
        controller.kill(
            [ProcessTarget(1003, "app.exe"), ProcessTarget(1004, "lsass.exe")],
            confirmed=True,
        )
        rows = _audited(tmp_path / "kills.jsonl")
        assert [(row["pid"], row["ok"]) for row in rows] == [(1003, True), (1004, False)]


class TestService:
    def test_malformed_rows_are_dropped_before_the_tool_sees_them(self, tmp_path: Path) -> None:
        alive = FakeProcess(1100, "app.exe")
        controller = _controller(tmp_path, {1100: alive})
        service = ProcessService(lambda: controller)
        answer = service.kill(
            [{"pid": "nope"}, {"pid": 1100, "name": "app.exe"}],
            confirmed=True,
        )
        assert answer["ended"] == 1
        assert [entry["pid"] for entry in answer["results"]] == [1100]

    def test_nothing_usable_is_an_error_not_a_crash(self, tmp_path: Path) -> None:
        service = ProcessService(lambda: _controller(tmp_path, {}))
        answer = service.kill([{"nonsense": True}], confirmed=True)
        assert answer["results"] == [] and answer["error"]

    def test_an_unconfirmed_click_comes_back_as_text(self, tmp_path: Path) -> None:
        service = ProcessService(
            lambda: _controller(
                tmp_path,
                {},
            )
        )
        answer = service.kill([{"pid": 1, "name": "x.exe"}], confirmed=False)
        assert "二次确认" in answer["error"]

    def test_a_missing_service_says_so(self) -> None:
        service = ProcessService(lambda: None)
        answer = service.kill([{"pid": 1, "name": "x.exe"}], confirmed=True)
        assert answer["error"] == "进程服务未启动"
