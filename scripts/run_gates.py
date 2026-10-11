"""One entry point for every gate, with the evidence a green run is supposed to carry.

Why this exists: the r93 review found that "``mypy --strict`` 0 错" had been written down for
``jarvis`` alone while the test tree carried 63 type errors, and that a PowerShell gate had
been judged by the last line it printed rather than by its exit code. Both are the same
mistake -- a claim without a scope. So this runs each check, records the tool version, the
command, the exit code and the duration, and refuses to call anything a pass that it did
not just observe.

Usage::

    python scripts/run_gates.py                 # code gates only (fast, no npm)
    python scripts/run_gates.py --frontend      # also type-check and build the page
    python scripts/run_gates.py --out E:\\gates\\r94.json
"""

from __future__ import annotations

import argparse
import json
import platform
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]


def _version(command: list[str]) -> str:
    """The tool's own first line, so a version drift is visible in the report."""
    try:
        done = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
            shell=False,
            cwd=ROOT,
        )
    except (OSError, subprocess.SubprocessError):
        return "不可用"
    return (
        ((done.stdout or "") + (done.stderr or "")).strip().splitlines()[:1][0]
        if done.stdout or done.stderr
        else ""
    )


def _run(name: str, command: list[str], *, timeout: int, cwd: Path | None = None) -> dict[str, Any]:
    started = time.time()
    print(f"· {name}: {' '.join(command)}", flush=True)
    done = subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        shell=False,
        cwd=str(cwd or ROOT),
    )
    tail = [
        line for line in ((done.stdout or "") + (done.stderr or "")).splitlines() if line.strip()
    ]
    record: dict[str, Any] = {
        "name": name,
        "command": command,
        "exit_code": done.returncode,
        "seconds": round(time.time() - started, 1),
        "summary": tail[-1] if tail else "",
    }
    mark = "通过" if done.returncode == 0 else f"失败（退出码 {done.returncode}）"
    print(f"  {mark}｜{record['summary'][:160]}", flush=True)
    if done.returncode != 0:
        for line in tail[-25:]:
            print(f"    {line}", flush=True)
    return record


def main() -> int:
    parser = argparse.ArgumentParser(description="Run every quality gate and record the evidence.")
    parser.add_argument("--frontend", action="store_true", help="also vue-tsc + vite build")
    parser.add_argument("--out", type=Path, help="where to write the JSON report")
    parser.add_argument("--quick", action="store_true", help="skip the full pytest run")
    args = parser.parse_args()

    python = sys.executable
    npm = "npm.CMD" if platform.system() == "Windows" else "npm"
    steps: list[tuple[str, list[str], int, Path | None]] = [
        ("black", [python, "-m", "black", "--check", "-q", "."], 300, None),
        ("ruff", [python, "-m", "ruff", "check", "-q", "."], 300, None),
        (
            "mypy(jarvis+tests)",
            [python, "-m", "mypy", "--strict", "--no-pretty", "jarvis", "tests"],
            1200,
            None,
        ),
    ]
    if not args.quick:
        steps.append(
            (
                "pytest",
                # pyproject already carries ``-q``; passing another makes it ``-qq``, which
                # drops the "N passed" line and leaves the report summarizing a row of dots.
                [python, "-m", "pytest", "--tb=line", "-p", "no:cacheprovider"],
                3600,
                None,
            )
        )
    if args.frontend:
        steps.append(("frontend", [npm, "run", "build"], 900, ROOT / "frontend"))

    report: dict[str, Any] = {
        "python": platform.python_version(),
        "platform": platform.platform(),
        "tools": {
            "black": _version([python, "-m", "black", "--version"]),
            "ruff": _version([python, "-m", "ruff", "--version"]),
            "mypy": _version([python, "-m", "mypy", "--version"]),
            "pytest": _version([python, "-m", "pytest", "--version"]),
        },
        "scope_note": "mypy 的范围是 jarvis 和 tests 两个目录；只跑 jarvis 不算全项目通过。",
        "steps": [],
    }
    failed: list[str] = []
    for name, command, timeout, cwd in steps:
        try:
            record = _run(name, command, timeout=timeout, cwd=cwd)
        except subprocess.TimeoutExpired:
            record = {
                "name": name,
                "command": command,
                "exit_code": -1,
                "seconds": timeout,
                "summary": "超时",
            }
        report["steps"].append(record)
        if record["exit_code"] != 0:
            failed.append(name)
    report["ok"] = not failed
    report["failed"] = failed

    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.out is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text + "\n", encoding="utf-8", newline="\n")
        print(f"门禁报告：{args.out}")
    print("全部通过" if not failed else f"这些没过：{'、'.join(failed)}")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
