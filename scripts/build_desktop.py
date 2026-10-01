"""Build the desktop HUD bundle and prove it is self-consistent.

``jarvis/ui/web`` is not in git (hash-named assets churn on every build) but it IS
declared as ``package-data``, so whoever builds a wheel has to run this first. The
script fails loudly rather than producing a wheel that installs a window with
nothing in it: after Vite finishes it re-reads ``index.html`` and checks that every
asset the page references actually landed on disk.

Usage::

    python scripts/build_desktop.py            # npm install (if needed) + build
    python scripts/build_desktop.py --check    # only verify what is already there
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
FRONTEND_DIR = REPO_ROOT / "frontend"
ICON_PATH = REPO_ROOT / "packaging" / "app.ico"
sys.path.insert(0, str(REPO_ROOT))

from jarvis.ui.desktop import WEB_DIR, bundle_hint, index_path  # noqa: E402


def _make_icon_module() -> object:
    """Load ``scripts/make_icon.py`` by path (``scripts`` is not a package)."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "make_icon", REPO_ROOT / "scripts" / "make_icon.py"
    )
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        raise SystemExit("找不到 scripts/make_icon.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def write_icon() -> None:
    """(Re)generate the application icon.

    Regenerated on every build rather than committed as a blob: the icon is
    drawn from a dozen numbers in ``make_icon.py``, so the script is the source
    of truth and a stale .ico would silently disagree with it.
    """
    module = _make_icon_module()
    ICON_PATH.parent.mkdir(parents=True, exist_ok=True)
    ICON_PATH.write_bytes(module.build_ico())  # type: ignore[attr-defined]
    print(f"图标已生成：{ICON_PATH}")


def _npm() -> str:
    """The npm executable, resolved the way Windows needs it."""
    executable = shutil.which("npm")
    if executable is None:
        raise SystemExit(
            "找不到 npm。请安装 Node.js（建议放在 E:\\BianChengGongJu\\NodeJs），"
            "或直接用已有的 jarvis/ui/web 产物跑 --check。"
        )
    return executable


def _run(*command: str) -> None:
    print(f"$ {' '.join(command)}", flush=True)
    completed = subprocess.run(command, cwd=FRONTEND_DIR, check=False)
    if completed.returncode != 0:
        raise SystemExit(f"前端构建失败（退出码 {completed.returncode}）：{' '.join(command)}")


def build(*, force_install: bool) -> None:
    """Install dependencies when needed, then build into ``jarvis/ui/web``."""
    if force_install or not (FRONTEND_DIR / "node_modules" / ".package-lock.json").is_file():
        _npm_and_install()
    _run(_npm(), "run", "build")
    write_icon()


def _npm_and_install() -> None:
    """Prefer a lockfile-respecting ``ci`` and fall back to ``install``.

    ``npm ci`` wipes ``node_modules`` and needs the lockfile to match
    ``package.json``; a developer who just added a dependency would otherwise get a
    confusing hard failure here.
    """
    npm = _npm()
    lockfile = FRONTEND_DIR / "package-lock.json"
    if lockfile.is_file():
        _run(npm, "ci")
    else:
        _run(npm, "install")


def verify() -> int:
    """Check the bundle and say what is wrong. Returns a process exit code."""
    problem = bundle_hint()
    if problem:
        print(f"构建产物校验失败：{problem}", file=sys.stderr)
        return 1
    if not ICON_PATH.is_file():
        print(
            f"构建产物校验失败：缺少应用图标 {ICON_PATH}\n"
            "  重新生成：python scripts/make_icon.py",
            file=sys.stderr,
        )
        return 1
    files = sorted(p for p in WEB_DIR.rglob("*") if p.is_file())
    total = sum(p.stat().st_size for p in files)
    print(f"界面产物就绪：{index_path()}")
    print(f"  {len(files)} 个文件，{total / 1024 / 1024:.2f} MiB，位于 {WEB_DIR}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="构建 JARVIS 桌面 HUD 的前端产物")
    parser.add_argument(
        "--check",
        action="store_true",
        help="只校验已有产物，不跑 npm（打包前自查用）",
    )
    parser.add_argument(
        "--reinstall",
        action="store_true",
        help="无论 node_modules 是否存在都重装依赖（换机器 / lockfile 变了时用）",
    )
    args = parser.parse_args(argv)
    if not args.check:
        build(force_install=args.reinstall)
    return verify()


if __name__ == "__main__":
    sys.exit(main())
