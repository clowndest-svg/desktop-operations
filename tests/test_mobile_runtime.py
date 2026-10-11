"""Execute mobile TypeScript behaviour with fake network/device ports, not string assertions."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest


def test_mobile_runtime_contracts() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is needed for mobile runtime tests")
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [
            node,
            "--experimental-strip-types",
            "--experimental-vm-modules",
            "scripts/test_mobile_runtime.mjs",
        ],
        cwd=root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "behavioural checks passed" in result.stdout
