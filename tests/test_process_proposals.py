"""P0-3: the model may ask to end a process, and that is where her authority ends.

``propose_kill_process`` writes to the panel's queue. The act stays on the existing
two-door path -- tick, then 确认 -- where the protection list and the pid's real name are
re-read next to the kill. These tests are mostly about refusals, because the failure
this guards against is not "she couldn't close notepad" but "she closed something the
operator never agreed to".
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, cast

import pytest

from jarvis.app.process_service import MAX_PROPOSALS, ProcessService
from jarvis.config.schema import ToolsSection
from jarvis.tools.builtins import process_tools
from jarvis.tools.registry import ToolRegistry
from jarvis.tools.types import RiskLevel
from tests.test_tools_process_control import FakeProcess, _audited, _controller


def _tools_section() -> ToolsSection:
    return ToolsSection.from_mapping(
        {
            "enabled": True,
            "confirm_dangerous": True,
            "allow_write": False,
            "allow_shell": False,
            "file_roots": [],
            "max_result_chars": 4000,
        }
    )


def _service(tmp_path: Path, processes: dict[int, FakeProcess]) -> ProcessService:
    return ProcessService(lambda: _controller(tmp_path, processes, own_pids=[]))


def _registry(queue: ProcessService) -> ToolRegistry:
    registry = ToolRegistry(_tools_section)
    for spec, handler in process_tools.build(proposals=cast(Any, queue)):
        registry.register(spec, handler)
    return registry


@pytest.fixture
def notepad(tmp_path: Path) -> dict[str, Any]:
    alive = FakeProcess(4100, "notepad.exe")
    queue = _service(tmp_path, {4100: alive, 700: FakeProcess(700, "explorer.exe")})
    return {"process": alive, "queue": queue, "registry": _registry(queue), "tmp": tmp_path}


class TestRegistration:
    def test_no_window_means_no_proposal_tool(self) -> None:
        """``--ask`` has nobody to press 确认. A tool that can only ever produce an
        unanswered request is the ``run_shell`` mistake: advertise a door that is not
        there and the model sends the user to it."""
        assert process_tools.build() == []
        assert process_tools.build(proposals=None) == []

    def test_the_tool_is_caution_never_dangerous(self, notepad: dict[str, Any]) -> None:
        """It writes a row on screen. Marking it DANGEROUS would demand a confirmation
        flag the model cannot set and would make the queue unreachable rather than
        safer -- the human gate is the panel, not the risk enum."""
        spec = notepad["registry"].get("propose_kill_process")
        assert spec is not None and spec.spec.risk is RiskLevel.CAUTION

    def test_a_missing_name_is_refused_before_the_queue_sees_it(
        self, notepad: dict[str, Any]
    ) -> None:
        result = notepad["registry"].invoke("propose_kill_process", {"pid": 4100})
        assert not result.ok and "name" in result.error


class TestProposalRefusals:
    def test_a_protected_name_is_refused_at_proposal_not_after_the_act(
        self, tmp_path: Path
    ) -> None:
        """Rejected on the way in, with the reason handed back, so she can tell the
        operator why rather than reporting a request that vanished."""
        queue = _service(tmp_path, {4: FakeProcess(4, "lsass.exe")})
        result = _registry(queue).invoke(
            "propose_kill_process", {"pid": 4, "name": "lsass.exe", "reason": "占用高"}
        )
        assert result.ok and "没有提交" in result.output and "关键" in result.output
        assert queue.proposals() == []

    def test_explorer_is_not_on_the_protection_list_and_that_is_deliberate(
        self, notepad: dict[str, Any]
    ) -> None:
        """Killing the shell is a real recovery move an operator may want, so it stays
        tickable in the panel; what protects it here is that a person has to press 确认.
        Recorded because the plan said this one would be refused at proposal -- it is
        not, and the human gate is the reason that is acceptable."""
        result = notepad["registry"].invoke(
            "propose_kill_process", {"pid": 700, "name": "explorer.exe", "reason": "卡了"}
        )
        assert result.ok and [row["pid"] for row in notepad["queue"].proposals()] == [700]

    def test_the_protection_list_covers_the_names_that_would_end_the_session(
        self, notepad: dict[str, Any]
    ) -> None:
        for name in ("lsass.exe", "svchost.exe", "dwm.exe", "小夜.exe", "msedgewebview2.exe"):
            verdict = notepad["queue"].propose(999, name, "试一下")
            assert verdict["ok"] is False, name

    def test_a_pid_running_something_else_is_refused_with_what_it_actually_is(
        self, notepad: dict[str, Any]
    ) -> None:
        verdict = notepad["queue"].propose(4100, "chrome.exe", "关掉浏览器")
        assert verdict["ok"] is False
        assert "notepad.exe" in str(verdict["error"])
        assert notepad["queue"].proposals() == []

    def test_a_process_that_is_not_there_is_refused(self, notepad: dict[str, Any]) -> None:
        verdict = notepad["queue"].propose(99999, "ghost.exe")
        assert verdict["ok"] is False and "没有 PID" in str(verdict["error"])

    def test_an_empty_name_is_refused_rather_than_matched_against_anything(
        self, notepad: dict[str, Any]
    ) -> None:
        assert notepad["queue"].propose(4100, "  ")["ok"] is False


class TestTheQueueIsNotAnExecution:
    def test_an_accepted_proposal_ends_nothing_and_says_wait_for_the_button(
        self, notepad: dict[str, Any]
    ) -> None:
        result = notepad["registry"].invoke(
            "propose_kill_process", {"pid": 4100, "name": "notepad.exe", "reason": "写完了"}
        )
        assert result.ok, result.error
        assert not notepad["process"].terminated and not notepad["process"].killed
        assert "等人按" in result.output and "才会真的停止" in result.output
        rows = notepad["queue"].proposals()
        assert [row["pid"] for row in rows] == [4100]
        assert rows[0]["reason"] == "写完了"
        # Nothing has been attempted yet, so nothing belongs in the audit file.
        assert _audited(notepad["tmp"] / "kills.jsonl") == []

    def test_the_queue_is_bounded_because_it_is_a_prompt_not_an_inbox(self, tmp_path: Path) -> None:
        alive = {1000 + index: FakeProcess(1000 + index, f"p{index}.exe") for index in range(12)}
        queue = _service(tmp_path, alive)
        for pid in alive:
            assert queue.propose(pid, f"p{pid - 1000}.exe")["ok"] is True
        assert len(queue.proposals()) == MAX_PROPOSALS

    def test_a_proposal_somebody_ignored_stops_being_offered(self, notepad: dict[str, Any]) -> None:
        notepad["queue"].propose(4100, "notepad.exe")
        assert notepad["queue"].dismiss(4100) is True
        assert notepad["queue"].proposals() == []
        assert notepad["queue"].dismiss(4100) is False

    def test_an_aged_out_proposal_disappears_on_its_own(self, notepad: dict[str, Any]) -> None:
        """The pid it names may be somebody else's by now, so the strip must not keep
        offering it -- the re-check at the act would refuse, but a dead row is noise."""
        queue: Any = notepad["queue"]
        queue.propose(4100, "notepad.exe")
        queue._proposals[0] = queue._proposals[0].__class__(
            pid=4100, name="notepad.exe", reason="", asked_at=time.time() - 700
        )
        assert queue.proposals() == []


class TestThroughTheBridge:
    """What the panel actually calls. A proposal the page cannot read is not a door."""

    def test_no_process_service_answers_in_a_shape_the_page_can_draw(self, tmp_path: Path) -> None:
        from tests.test_ui_desktop import _bridge

        bridge = _bridge(tmp_path)
        assert bridge.process_proposals() == {"entries": [], "error": "进程服务不可用"}
        assert bridge.process_dismiss(1)["ok"] is False

    def test_a_proposal_made_by_the_model_shows_up_for_the_person_to_answer(
        self, notepad: dict[str, Any]
    ) -> None:
        from tests.test_ui_desktop import _bridge

        queue: ProcessService = notepad["queue"]
        bridge = _bridge(notepad["tmp"], process=queue)
        notepad["registry"].invoke(
            "propose_kill_process", {"pid": 4100, "name": "notepad.exe", "reason": "写完了"}
        )

        board = bridge.process_proposals()
        assert board["error"] == ""
        entries = cast("list[dict[str, Any]]", board["entries"])
        assert [entry["pid"] for entry in entries] == [4100]
        assert entries[0]["reason"] == "写完了"

        assert bridge.process_dismiss(4100)["ok"] is True
        assert bridge.process_proposals()["entries"] == []

    def test_a_pid_that_arrives_as_a_string_from_javascript_is_refused_not_guessed(
        self, notepad: dict[str, Any]
    ) -> None:
        from tests.test_ui_desktop import _bridge

        bridge = _bridge(notepad["tmp"], process=notepad["queue"])
        assert bridge.process_dismiss("四千一百")["ok"] is False
        assert bridge.process_dismiss(4100)["ok"] is False  # nothing queued yet


class TestTheHumanDoorIsStillTheOnlyDoor:
    def test_confirming_in_the_panel_is_what_actually_ends_it(
        self, notepad: dict[str, Any]
    ) -> None:
        notepad["registry"].invoke(
            "propose_kill_process", {"pid": 4100, "name": "notepad.exe", "reason": "写完了"}
        )
        report = notepad["queue"].kill([{"pid": 4100, "name": "notepad.exe"}], confirmed=True)

        assert report["ended"] == 1 and notepad["process"].terminated
        assert notepad["queue"].proposals() == []
        rows = _audited(notepad["tmp"] / "kills.jsonl")
        assert len(rows) == 1 and rows[0]["ok"] is True and rows[0]["pid"] == 4100

    def test_the_same_proposal_refuses_a_person_who_only_clicked_once(
        self, notepad: dict[str, Any]
    ) -> None:
        notepad["queue"].propose(4100, "notepad.exe")
        report = notepad["queue"].kill([{"pid": 4100, "name": "notepad.exe"}], confirmed=False)
        assert report["error"] and not notepad["process"].terminated

    def test_a_recycled_pid_is_still_refused_at_the_moment_of_the_act(self, tmp_path: Path) -> None:
        """The proposal said ``notepad.exe``, then notepad exited and Windows handed 4100
        to something else. The queue is not proof of anything; the re-read is."""
        alive = FakeProcess(4100, "notepad.exe")
        queue = _service(tmp_path, {4100: alive})
        assert queue.propose(4100, "notepad.exe")["ok"] is True
        alive._name = "lsass.exe"  # the number now belongs to something else entirely

        report = queue.kill([{"pid": 4100, "name": "notepad.exe"}], confirmed=True)
        assert report["ended"] == 0
        assert not alive.terminated
        assert "复用" in report["results"][0]["note"]
