"""Content-addressed provenance and path checks shared by the release tools."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SOURCE_PATTERNS = (
    "jarvis/**/*.py",
    "jarvis/config/defaults.yaml",
    "frontend/src/**/*.ts",
    "frontend/src/**/*.vue",
    "frontend/src/**/*.css",
    "frontend/package*.json",
    "frontend/vite*.ts",
    "frontend/capacitor.config.ts",
    "frontend/index.html",
    "frontend/mobile/index.html",
    "frontend/android/app/src/main/**/*.java",
    "frontend/android/app/src/main/AndroidManifest.xml",
    "frontend/android/app/build.gradle",
    "packaging/*.py",
    "packaging/*.spec",
    "packaging/hooks/*.py",
    "pyproject.toml",
)


def digest_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def source_fingerprint(root: Path) -> str:
    paths = {
        path
        for pattern in SOURCE_PATTERNS
        for path in root.glob(pattern)
        if path.is_file() and "__pycache__" not in path.parts
    }
    digest = hashlib.sha256()
    for path in sorted(paths, key=lambda item: item.relative_to(root).as_posix()):
        digest.update(path.relative_to(root).as_posix().encode("utf-8") + b"\0")
        # Git may check out CRLF; line endings do not change these text programs.
        digest.update(path.read_bytes().replace(b"\r\n", b"\n"))
        digest.update(b"\0")
    return digest.hexdigest()


def round_from(*hints: object) -> str:
    """The ``rNN`` path segment inside the build hints PyInstaller was given.

    Lives here rather than in the spec because the spec is not importable and this is
    the one number every buyer's "is my copy old" answer is derived from. Last hint
    wins so an explicit ``--distpath`` outruns a leftover ``--workpath``.
    """
    found = ""
    for hint in hints:
        parts = str(hint or "").replace("/", "\\").split("\\")
        hits = [part for part in parts if re.fullmatch(r"r[0-9]+", part)]
        if hits:
            found = hits[-1]
    return found


def build_info(root: Path, round_name: str = "") -> dict[str, object]:
    """What the bundle has to know about itself.

    ``round_name`` is the artifact's identity (``r107``). It is passed in rather than
    looked up because the only place that knows it is the PyInstaller invocation's
    ``--distpath``. Empty means "a build nobody labelled", and ``jarvis.release_round``
    then returns "" so ``check_update`` refuses to compare instead of inventing 0.
    """
    try:
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True, check=True
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        head = "unknown"
    now = datetime.now(UTC).astimezone()
    return {
        "built_at": now.isoformat(timespec="seconds"),
        "git_head": head,
        "source_sha256": source_fingerprint(root),
        "round": valid_round(round_name) if round_name else "",
        "schema": 2,
        "edition": os.environ.get("XIAOYE_BUILD_EDITION", "author"),
    }


def valid_round(value: str) -> str:
    if re.fullmatch(r"r[0-9]+", value) is None:
        raise ValueError(f"invalid release round: {value!r}")
    return value


def scoped_path(root: Path, target: Path) -> Path:
    """Only strict descendants are valid release artifacts; never the root itself."""
    base = root.resolve()
    resolved = target.resolve()
    if resolved == base or not resolved.is_relative_to(base):
        raise ValueError(f"release path escapes its root: {target}")
    return resolved


def read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pending = path.with_name(path.name + ".tmp")
    pending.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    pending.replace(path)


MOBILE_PATTERNS = (
    "frontend/src/mobile/**/*",
    "frontend/src/mobile-main.ts",
    "frontend/src/theme.ts",
    "frontend/src/styles/hud.css",
    "frontend/mobile/index.html",
    "frontend/vite.mobile.config.ts",
    "frontend/capacitor.config.ts",
    "frontend/package*.json",
    "frontend/android/app/src/main/**/*.java",
    "frontend/android/app/src/main/AndroidManifest.xml",
    "frontend/android/app/src/main/res/**/*.xml",
    "frontend/android/app/build.gradle",
    "frontend/android/build.gradle",
    "frontend/android/variables.gradle",
)


def mobile_fingerprint(root: Path) -> str:
    paths = {path for pattern in MOBILE_PATTERNS for path in root.glob(pattern) if path.is_file()}
    digest = hashlib.sha256()
    for path in sorted(paths, key=lambda item: item.relative_to(root).as_posix()):
        digest.update(path.relative_to(root).as_posix().encode("utf-8") + b"\0")
        digest.update(path.read_bytes().replace(b"\r\n", b"\n") + b"\0")
    return digest.hexdigest()
