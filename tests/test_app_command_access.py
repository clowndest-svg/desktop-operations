"""The command line as a level a person sets, and a ledger of what ran.

Two things are pinned here that the rest of the design could quietly lose:

* the *default* — a fresh install must not be able to run anything, and must not
  advertise that it can;
* the *refusal* — a policy hit is an answer the model reads, not an exception, so it
  has to name what to do about it.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, cast

import pytest

from jarvis.app.command_access import (
    CommandAccess,
    CommandRecord,
    mode_for_tier,
    tier_for_mode,
)
from jarvis.app.preferences import SHELL_TIER, Preferences


def as_dict(value: object) -> dict[str, Any]:
    """Narrow a bridge-shaped ``dict[str, object]`` value for the assertions below."""
    assert isinstance(value, dict)
    return cast("dict[str, Any]", value)


def _access(tmp_path: Path) -> CommandAccess:
    return CommandAccess(Preferences(tmp_path / "preferences.json"), tmp_path / "commands.jsonl")


class TestLevels:
    def test_a_fresh_install_is_off(self, tmp_path: Path) -> None:
        access = _access(tmp_path)
        assert access.mode() == "off"
        assert access.current()["label"] == "关闭"

    @pytest.mark.parametrize(
        ("tier", "mode"),
        [(0, "off"), (1, "rehearsal"), (2, "user"), (3, "admin")],
    )
    def test_tiers_round_trip(self, tier: int, mode: str) -> None:
        assert mode_for_tier(tier) == mode
        assert tier_for_mode(mode) == tier

    def test_an_unknown_tier_is_off_not_the_highest_one(self) -> None:
        assert mode_for_tier(99) == "off"
        assert mode_for_tier("admin") == "off"

    def test_set_mode_persists_and_lists_the_new_one(self, tmp_path: Path) -> None:
        access = _access(tmp_path)
        answer = access.set_mode(2)
        assert answer["error"] == ""
        assert as_dict(answer["current"])["mode"] == "user"
        assert Preferences(tmp_path / "preferences.json").number(SHELL_TIER) == 2

    @pytest.mark.parametrize("value", [7, "two", None, True, []])
    def test_a_level_that_does_not_exist_is_refused_and_changes_nothing(
        self, tmp_path: Path, value: object
    ) -> None:
        access = _access(tmp_path)
        answer = access.set_mode(value)
        assert answer["error"]
        # The error must survive the payload: a spread placed after it silently wins.
        assert as_dict(answer["current"])["mode"] == "off"
        assert access.mode() == "off"

    def test_the_popup_gets_every_level_marked_and_ordered(self, tmp_path: Path) -> None:
        listing = _access(tmp_path).levels()
        rows = as_dict(listing)["levels"]
        assert isinstance(rows, list)
        modes = [as_dict(entry)["mode"] for entry in rows]
        assert modes == ["off", "rehearsal", "user", "admin"]
        assert [as_dict(entry)["current"] for entry in rows] == [True, False, False, False]


class TestAudit:
    def test_a_run_is_written_before_and_after(self, tmp_path: Path) -> None:
        access = _access(tmp_path)
        access.audit(mode="user", script="Get-Volume", exit_code=None, seconds=None, note="开始")
        access.audit(mode="user", script="Get-Volume", exit_code=0, seconds=1.5, note="完成")
        rows = (tmp_path / "commands.jsonl").read_text(encoding="utf-8").strip().splitlines()
        assert len(rows) == 2
        assert json.loads(rows[1])["exit_code"] == 0

    def test_recent_is_newest_first_and_survives_a_restart(self, tmp_path: Path) -> None:
        access = _access(tmp_path)
        access.record(CommandRecord(at=1.0, mode="user", script="dir"))
        access.record(CommandRecord(at=2.0, mode="admin", script="sfc /scannow", exit_code=0))
        assert [entry["script"] for entry in access.recent()] == ["sfc /scannow", "dir"]

        reopened = _access(tmp_path)
        reopened.start()
        assert [entry["script"] for entry in reopened.recent()] == ["sfc /scannow", "dir"]
        assert reopened.recent()[0]["label"] == "管理员"

    def test_a_corrupt_line_is_skipped_rather_than_losing_the_rest(self, tmp_path: Path) -> None:
        Path(tmp_path / "commands.jsonl").write_text(
            '{"at": 1, "mode": "user", "script": "ok"}\nnot json\n{"at": 2}\n',
            encoding="utf-8",
        )
        access = _access(tmp_path)
        access.start()
        # newest first, so the line without a script comes out in front.
        assert [entry["script"] for entry in access.recent()] == ["", "ok"]

    def test_an_exit_code_of_none_stays_none(self, tmp_path: Path) -> None:
        """A cancelled UAC prompt is not exit code 0, and 0 is not "unknown"."""
        access = _access(tmp_path)
        access.record(CommandRecord(at=1.0, mode="admin", script="whoami", exit_code=None))
        assert access.recent()[0]["exit_code"] is None


class _Gate:
    """A CommandAccess stand-in that counts what the tool reported."""

    def __init__(self, mode: str) -> None:
        self._mode = mode
        self.audits: list[dict[str, Any]] = []

    def mode(self) -> str:
        return self._mode

    def audit(self, **fields: Any) -> None:
        self.audits.append(fields)


class _Completed:
    def __init__(self, returncode: int = 0, stdout: bytes = b"", stderr: bytes = b"") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _with_gate(monkeypatch: pytest.MonkeyPatch, mode: str) -> tuple[Any, _Gate]:
    from jarvis.tools.builtins import shell_tools

    gate = _Gate(mode)
    tools = {spec.name: call for spec, call in shell_tools.build_with_gate(lambda: gate)}
    return tools["run_powershell"], gate


class TestTool:
    def test_off_refuses_and_says_where_to_turn_it_on(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import subprocess

        def _boom(*args: object, **kwargs: object) -> None:
            raise AssertionError("off must never reach subprocess")

        monkeypatch.setattr(subprocess, "run", _boom)
        run, _gate = _with_gate(monkeypatch, "off")
        answer = run({"script": "Get-Volume"})
        assert "关闭" in answer and "控制" in answer

    def test_rehearsal_shows_the_command_line_without_running_it(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import subprocess

        monkeypatch.setattr(subprocess, "run", _forbidden)
        run, _gate = _with_gate(monkeypatch, "rehearsal")
        answer = run({"script": "Get-Process | Stop-Process"})
        assert "演练" in answer
        assert "-EncodedCommand" in answer
        assert "Stop-Process" not in answer  # the script travels encoded, never in plain

    def test_user_runs_powershell_with_an_explicit_argv(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import subprocess

        seen: list[list[str]] = []

        def fake_run(argv: list[str], **kwargs: object) -> _Completed:
            seen.append(argv)
            return _Completed(returncode=0, stdout="C: 已用 71%".encode())

        monkeypatch.setattr(subprocess, "run", fake_run)
        run, gate = _with_gate(monkeypatch, "user")
        answer = run({"script": "Get-Volume"})
        assert seen[0][0] == "powershell.exe"
        assert "-NoProfile" in seen[0] and "-NonInteractive" in seen[0]
        assert "-Command" not in seen[0]  # only -EncodedCommand, so nothing can re-parse
        assert "退出码：0" in answer and "71%" in answer
        assert [entry["note"] for entry in gate.audits] == ["开始", "完成"]
        assert gate.audits[1]["exit_code"] == 0

    def test_a_script_with_quotes_is_data_not_syntax(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import base64
        import subprocess

        captured: dict[str, list[str]] = {}

        def fake_run(argv: list[str], **kwargs: object) -> _Completed:
            captured["argv"] = argv
            return _Completed()

        monkeypatch.setattr(subprocess, "run", fake_run)
        run, _gate = _with_gate(monkeypatch, "user")
        nasty = '"; Remove-Item C:\\ -Recurse; "'
        run({"script": nasty})
        decoded = base64.b64decode(captured["argv"][-1]).decode("utf-16-le")
        assert decoded.strip() == nasty  # it arrives as one argument, whole

    def test_admin_launches_through_runas_and_reads_the_result_file(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        import base64
        import subprocess

        calls: list[list[str]] = []

        def fake_run(argv: list[str], **kwargs: object) -> _Completed:
            calls.append(argv)
            # Two layers to unwrap: the outer command is Start-Process, and inside
            # its argument list sits the inner -EncodedCommand that the elevated
            # child actually runs. That inner script is where the result path is.
            outer = base64.b64decode(argv[-1]).decode("utf-16-le")
            nested = re.search(r"'([A-Za-z0-9+/=]{40,})'", outer)
            assert nested is not None, outer
            inner = base64.b64decode(nested.group(1)).decode("utf-16-le")
            located = re.search(r"\$out = '([^']+)'", inner)
            assert located is not None, inner
            Path(located.group(1)).write_text(
                "Volume  HealthStatus\n__exit__:0\n", encoding="utf-8"
            )
            return _Completed(returncode=0)

        monkeypatch.setattr(subprocess, "run", fake_run)
        run, gate = _with_gate(monkeypatch, "admin")
        answer = run({"script": "Get-Volume"})
        decoded = base64.b64decode(calls[0][-1]).decode("utf-16-le")
        assert "-Verb RunAs" in decoded and "-Wait" in decoded
        assert "HealthStatus" in answer
        assert "退出码：0" in answer
        assert "结果文件" in answer  # says how it knows, and how little
        assert gate.audits[-1]["mode"] == "admin"

    def test_a_declined_uac_prompt_is_reported_as_such(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import subprocess

        def fake_run(argv: list[str], **kwargs: object) -> _Completed:
            return _Completed(returncode=1, stderr="操作已被用户取消".encode())

        monkeypatch.setattr(subprocess, "run", fake_run)
        run, _gate = _with_gate(monkeypatch, "admin")
        answer = run({"script": "Get-Volume"})
        assert "提权窗口没有打开" in answer

    def test_a_timeout_is_a_readable_failure_and_is_audited(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import subprocess

        from jarvis.core.exceptions import ToolExecutionError

        def fake_run(argv: list[str], **kwargs: object) -> _Completed:
            raise subprocess.TimeoutExpired(cmd=argv, timeout=5)

        monkeypatch.setattr(subprocess, "run", fake_run)
        run, gate = _with_gate(monkeypatch, "user")
        with pytest.raises(ToolExecutionError, match="超过"):
            run({"script": "Start-Sleep 600", "timeout_seconds": 5})
        assert gate.audits[-1]["note"] == "超时"

    def test_the_tool_takes_no_confirmation_parameter(self) -> None:
        """``confirmed`` must stay out of reach of the model, as everywhere else."""
        from jarvis.tools.builtins.shell_tools import build_with_gate

        specs = [spec for spec, _call in build_with_gate(lambda: None)]
        powershell = next(spec for spec in specs if spec.name == "run_powershell")
        properties = as_dict(powershell.parameters["properties"])
        assert "confirmed" not in properties and "confirm" not in properties
        assert set(properties) == {"script", "timeout_seconds"}

    def test_the_shell_service_still_gates_its_own_tool(self) -> None:
        """Adding the level did not quietly unlock ``run_shell``."""
        from jarvis.tools.builtins.shell_tools import build, build_with_gate

        assert [spec.name for spec, _call in build()] == ["run_shell"]
        assert [spec.name for spec, _call in build_with_gate(lambda: None)] == [
            "run_shell",
            "run_powershell",
        ]


def _forbidden(*args: object, **kwargs: object) -> None:
    raise AssertionError("this level must not reach subprocess")
