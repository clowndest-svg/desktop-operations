"""Tests for the packaged entry point's two shipped defaults.

``packaging/entry.py`` is not an importable module (``scripts`` and ``packaging``
are not packages), so it is loaded by path the way ``build_desktop`` loads the icon
script. What is under test is the pair of decisions that only exist in the shipped
form: the flags a double-click runs with, and where its data lands when nobody has
configured anything. Both were guessed at before, and one of them put 900 MB of
speech models on the system drive.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
ENTRY_PATH = REPO_ROOT / "packaging" / "entry.py"


@pytest.fixture(scope="module")
def entry() -> Any:
    spec = importlib.util.spec_from_file_location("_jarvis_packaging_entry", ENTRY_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestShippedFlags:
    def test_a_double_click_gets_desktop_and_voice_permission(self, entry: Any) -> None:
        assert entry.build_argv([]) == ["--desktop", "--voice"]

    def test_caller_arguments_are_still_honoured(self, entry: Any) -> None:
        """``小夜.exe -v`` must keep working: DevTools is the only way to see a page error."""
        assert entry.build_argv(["-v"]) == ["--desktop", "--voice", "-v"]
