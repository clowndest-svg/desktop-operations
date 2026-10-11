"""``runtime_versions``: the one tool that answers 「我电脑上装了 JDK 没有」.

Written because a local model, asked exactly that with only ``system_report`` on hand,
answered that it could check CPU, memory and disk but not Java -- which was the correct
reading of the tools it was given. These tests pin that the reading stays correct by
making the tool exist, and pin the safety shape of a tool that runs processes: fixed
argv, nothing from the model, bounded output.
"""

from __future__ import annotations

import shutil
import subprocess
from typing import Any

import pytest

from jarvis.tools.builtins import runtime_tools


class _Done:
    def __init__(self, out: bytes = b"", err: bytes = b"", code: int = 0) -> None:
        self.stdout, self.stderr, self.returncode = out, err, code


@pytest.fixture
def calls() -> list[list[str]]:
    return []


@pytest.fixture
def run_ok(monkeypatch: pytest.MonkeyPatch, calls: list[list[str]]) -> None:
    def fake_run(argv: Any, **kwargs: Any) -> _Done:
        calls.append(list(argv))
        assert kwargs.get("shell") is False, "shell=True 会把固定命令变成拼接命令"
        assert kwargs.get("timeout") == runtime_tools._TIMEOUT_SECONDS
        name = argv[0].lower()
        if "java" in name and "javac" not in name:
            return _Done(
                err=b'openjdk version "17.0.19" 2026-04-21\r\nTemurin build 17.0.19+10\r\n'
            )
        return _Done(out=b"1.2.3\nsecond line ignored\n")

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(shutil, "which", lambda exe: rf"C:\tools\{exe}.exe")


class TestHappyPath:
    def test_every_listed_runtime_gets_a_line(self, run_ok: None, calls: list[list[str]]) -> None:
        report = runtime_tools.build()[0][1]({})
        for label, _, _ in runtime_tools.COMMANDS:
            assert label in report
        assert len(calls) == len(runtime_tools.COMMANDS)

    def test_java_is_read_from_stderr_because_that_is_where_it_prints(self, run_ok: None) -> None:
        report = runtime_tools.build()[0][1]({})
        assert "openjdk version" in report and "17.0.19" in report

    def test_only_the_first_line_of_a_banner_survives(self, run_ok: None) -> None:
        report = runtime_tools.build()[0][1]({})
        assert "Temurin build" not in report
        assert "second line ignored" not in report

    def test_the_argv_is_exactly_the_built_in_table(
        self, run_ok: None, calls: list[list[str]]
    ) -> None:
        """No argument reaches the command line, so there is nothing to inject."""
        runtime_tools.build()[0][1]({})
        tails = {tuple(argv[1:]) for argv in calls}
        assert tails == {("-version",), ("--version",)}


class TestMissingAndBroken:
    def test_an_absent_runtime_is_reported_not_raised(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(shutil, "which", lambda _: None)
        report = runtime_tools.build()[0][1]({})
        assert report.count("未安装") == len(runtime_tools.COMMANDS)
        assert "Traceback" not in report

    def test_a_slow_probe_is_cut_off(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def slow(*_a: Any, **_k: Any) -> _Done:
            raise subprocess.TimeoutExpired(cmd=["java"], timeout=5)

        monkeypatch.setattr(shutil, "which", lambda exe: f"/x/{exe}")
        monkeypatch.setattr(subprocess, "run", slow)
        report = runtime_tools.build()[0][1]({})
        assert "没回答" in report

    def test_an_oserror_says_which_one(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def boom(*_a: Any, **_k: Any) -> _Done:
            raise OSError(13, "拒绝访问")

        monkeypatch.setattr(shutil, "which", lambda exe: f"/x/{exe}")
        monkeypatch.setattr(subprocess, "run", boom)
        report = runtime_tools.build()[0][1]({})
        assert "PermissionError" in report

    def test_a_binary_that_prints_nothing_is_reported_as_such(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(shutil, "which", lambda exe: f"/x/{exe}")
        monkeypatch.setattr(subprocess, "run", lambda *_a, **_k: _Done())
        report = runtime_tools.build()[0][1]({})
        assert "没吐出版本号" in report


class TestBoundaries:
    def test_arguments_are_ignored_so_the_model_cannot_shape_a_command(
        self, run_ok: None, calls: list[list[str]]
    ) -> None:
        runtime_tools.build()[0][1]({"command": "whoami", "args": ["-a"]})
        assert all(argv[1:] in (["-version"], ["--version"]) for argv in calls)

    def test_the_spec_declares_no_parameters(self) -> None:
        assert runtime_tools._SPEC.parameters["properties"] == {}

    def test_it_is_a_read_only_tool(self) -> None:
        """SAFE, not dangerous: it writes nothing and runs nothing the model picked."""
        assert runtime_tools._SPEC.permissions == frozenset()

    def test_no_paths_and_no_environment_values_reach_the_report(self, run_ok: None) -> None:
        """The version line is the answer; where the binary lives is not, and neither is

        the environment it inherited -- a report that echoed PATH would put the whole
        directory list into a prompt that then goes to a cloud provider.
        """
        import os

        report = runtime_tools.build()[0][1]({})
        assert "\\" not in report and ":/" not in report.replace("http:/", "")
        for value in os.environ.values():
            if len(value) > 6:
                assert value not in report

    def test_output_is_bounded(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(shutil, "which", lambda exe: f"/x/{exe}")
        monkeypatch.setattr(subprocess, "run", lambda *_a, **_k: _Done(out=b"A" * 90_000))
        report = runtime_tools.build()[0][1]({})
        assert len(report) < 1_200
