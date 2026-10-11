"""Release self-check never starts the GUI or uses a real model."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from jarvis.__main__ import main
from jarvis.app.diagnostics import runtime_report


def test_self_check_precedes_desktop_start(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import jarvis.__main__ as entry

    def forbidden(*args: Any, **kwargs: Any) -> int:
        raise AssertionError("must not start the real desktop")

    monkeypatch.setattr(entry, "_run_desktop", forbidden)
    report = tmp_path / "self-check.json"
    result = main(["--desktop", "--voice", "--self-check-json", str(report)])
    data = json.loads(report.read_text(encoding="utf-8"))
    assert result in (0, 1)
    assert data["network_used"] is False
    assert data["microphone_opened"] is False
    assert data["user_data_opened"] is False
    assert next(row for row in data["checks"] if row["name"] == "chat_no_network")["ok"]


def test_missing_bundle_is_a_failed_check(tmp_path: Path) -> None:
    data = runtime_report(tmp_path, tmp_path / "models")
    assert not data["ok"]
    assert not next(row for row in data["checks"] if row["name"] == "desktop_bundle")["ok"]


def test_bundle_cannot_reference_files_outside_its_root(tmp_path: Path) -> None:
    web = tmp_path / "ui" / "web"
    web.mkdir(parents=True)
    (web / "index.html").write_text('<script src="../secret.js"></script>', encoding="utf-8")
    (web.parent / "secret.js").write_text("test", encoding="utf-8")
    data = runtime_report(tmp_path, tmp_path / "models")
    assert not next(row for row in data["checks"] if row["name"] == "desktop_bundle")["ok"]


@pytest.mark.parametrize(
    "arguments",
    [
        ["--self-check"],
        ["--self-check-json", "report.json"],
        ["--self-check-json=report.json"],
        ["--version"],
    ],
)
def test_frozen_hooks_do_not_create_user_data_in_read_only_modes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, arguments: list[str]
) -> None:
    import inspect
    import runpy
    import sys

    root = Path(__file__).resolve().parents[1]
    data = tmp_path / "private-data"
    local = tmp_path / "LocalAppData"
    monkeypatch.setenv("JARVIS_HOME", str(data))
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    monkeypatch.setattr(sys, "argv", [str(tmp_path / "小夜.exe"), *arguments])
    monkeypatch.setattr(inspect, "getsource", inspect.getsource)
    monkeypatch.setattr(inspect, "getsourcelines", inspect.getsourcelines)
    runpy.run_path(str(root / "packaging/hooks/runtime_hook_data_root.py"))
    runpy.run_path(str(root / "packaging/hooks/runtime_hook_funasr.py"))
    assert not data.exists()
    assert not local.exists()
    assert not (tmp_path / "数据").exists()
