"""Packaging honesty gates: nothing may be imported unless metadata declares it.

This nearly shipped twice in one day, in the same shape: ``pywebview`` and
``psutil`` were used by the desktop HUD but appeared in ``pyproject.toml`` only in
mypy's ``ignore_missing_imports`` list, so a clean install following the README
would have hit ``ModuleNotFoundError`` the first time anyone double-clicked. Worse,
the UI fell back to *mock data* on that failure, turning a hard crash into a
dashboard that quietly displays made-up numbers.

Two checks:

1. **declared** - every third-party import in ``jarvis/**`` maps to a distribution
   listed in ``pyproject.toml`` (base dependencies or any extra).
2. **desktop path** - the modules ``python -m jarvis --desktop`` loads before the
   window opens are satisfied by base + ``[desktop]`` alone, so the HUD never
   depends on an extra the user was not told to install.

Check 2's file list mirrors ``jarvis/__main__.py:_run_desktop`` and
``jarvis/ui/desktop.py``. It is a *per-file* scan, not a transitive closure:
engines that lazy-import heavy SDKs (``funasr``, ``torch``) live in their own
modules and are covered by check 1 plus their extras.
"""

from __future__ import annotations

import ast
import os
import pathlib
import re
import sys
import tomllib
from typing import Any

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
PACKAGE_DIR = REPO_ROOT / "jarvis"
PYPROJECT = REPO_ROOT / "pyproject.toml"

# Distribution name (PEP 503-normalised) -> importable top-level module names.
# Anything imported but absent from the inverse of this map fails check 1, which is
# the point: the map is what has to be kept in step with pyproject.
DIST_TO_IMPORTS: dict[str, tuple[str, ...]] = {
    "pyyaml": ("yaml",),
    "langgraph": ("langgraph",),
    "qq-botpy": ("botpy",),
    "pywebview": ("webview",),
    "psutil": ("psutil",),
    "pystray": ("pystray",),
    "sounddevice": ("sounddevice",),
    "openwakeword": ("openwakeword",),
    "silero-vad": ("silero_vad",),
    "funasr": ("funasr",),
    "edge-tts": ("edge_tts",),
    "soundfile": ("soundfile",),
    "pyinstaller": ("PyInstaller",),
    "apscheduler": ("apscheduler",),
    "rapidocr-onnxruntime": ("rapidocr_onnxruntime",),
    "mss": ("mss",),
    "pillow": ("PIL",),
    "playwright": ("playwright",),
    "pyautogui": ("pyautogui",),
    "pypdf": ("pypdf",),
    "python-docx": ("docx",),
}

# Imported but never declared, each with the reason it may stay that way. Adding an
# entry here is a decision, not a fix: prefer declaring the package in its extra.
UNDECLARED_ALLOWED: dict[str, str] = {
    "numpy": "transitive of the voice extra (funasr/torch); pinned by them, not here",
    "torch": "transitive of silero-vad + funasr; a second ceiling here would fight them",
    "pvporcupine": "opt-in alternative engine, commented out of the voice extra on purpose",
    "cosyvoice": "no clean PyPI wheel; installed manually per its official guide",
    # ``from System.Drawing import Color`` in jarvis/ui/pet.py reaches a CLR namespace,
    # not a Python package: the bridge that makes those names importable is pythonnet,
    # which pywebview's Windows backend requires and pins. There is no distribution
    # named ``System`` to declare, and pinning pythonnet a second time here would only
    # fight pywebview's own ceiling.
    "System": "CLR namespaces handed to us by pythonnet, a hard dependency of pywebview on Windows",
}

# Loaded by the desktop HUD before its window appears. Files later phases add are
# listed now so this test keeps its teeth as the phases land.
DESKTOP_EAGER_PATH: tuple[str, ...] = (
    "jarvis/__main__.py",
    "jarvis/ui/desktop.py",
    "jarvis/ui/state_bridge.py",
    "jarvis/ui/pump.py",
    # The tray, the hide-instead-of-quit policy and the second-launch guard all run
    # before the window appears, so they are on the same "base + [desktop] only"
    # hook as the shell itself.
    "jarvis/ui/tray.py",
    "jarvis/ui/lifecycle.py",
    "jarvis/ui/instance.py",
    "jarvis/ui/pet.py",
    "jarvis/ui/audio_bridge.py",
    "jarvis/app/application.py",
    "jarvis/app/system_service.py",
    "jarvis/app/disk_service.py",
    "jarvis/app/voice_service.py",
    "jarvis/app/chat_service.py",
    "jarvis/tools/monitor.py",
    "jarvis/tools/disk_cleaner.py",
    "jarvis/config/loader.py",
    "jarvis/core/events.py",
)

# Requirement groups nobody imports at runtime: command-line tooling. They are
# excluded from the import-name check because nothing in ``jarvis/**`` may use them.
TOOLING_GROUPS: frozenset[str] = frozenset(
    {"optional-dependencies.dev", "optional-dependencies.build"}
)

# Present today and expected to stay: if one disappears the list above is a lie.
DESKTOP_EAGER_MUST_EXIST: tuple[str, ...] = (
    "jarvis/ui/desktop.py",
    "jarvis/app/system_service.py",
    "jarvis/tools/monitor.py",
)


def normalise(name: str) -> str:
    """PEP 503 name normalisation: lowercase, separators collapsed to ``-``."""
    return re.sub(r"[-_.]+", "-", name).lower()


def _requirement_name(requirement: str) -> str:
    """Pull the distribution name out of a PEP 508 requirement string."""
    match = re.match(r"\s*([A-Za-z0-9][A-Za-z0-9._-]*)", requirement)
    if match is None:  # pragma: no cover - guards against exotic requirement lines
        raise AssertionError(f"cannot read a package name from {requirement!r}")
    return normalise(match.group(1))


def _metadata() -> dict[str, Any]:
    return tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))


def _groups() -> dict[str, tuple[str, ...]]:
    """Every distribution the metadata declares, keyed by requirement group."""
    project = _metadata()["project"]
    groups: dict[str, tuple[str, ...]] = {"dependencies": tuple(project.get("dependencies", ()))}
    for extra, reqs in project.get("optional-dependencies", {}).items():
        groups[f"optional-dependencies.{extra}"] = tuple(reqs)
    return groups


def _runtime_groups() -> dict[str, tuple[str, ...]]:
    """Requirement groups that describe importable runtime dependencies."""
    return {name: reqs for name, reqs in _groups().items() if name not in TOOLING_GROUPS}


def _declared_distributions(groups: dict[str, tuple[str, ...]]) -> set[str]:
    return {_requirement_name(req) for reqs in groups.values() for req in reqs}


def _imported_modules(path: pathlib.Path) -> set[str]:
    """Top-level module names imported anywhere in a file, functions included.

    Lazy imports are the reason this walks the whole tree: ``import webview`` sits
    inside ``desktop.run()``, so a module-level-only scan would have missed exactly
    the bug this test exists to catch.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            found.add(node.module.split(".")[0])
    return {
        name
        for name in found
        if name not in sys.stdlib_module_names and name not in {"jarvis", "__future__"}
    }


def _package_files() -> list[pathlib.Path]:
    return sorted(PACKAGE_DIR.rglob("*.py"))


def test_desktop_extra_is_what_the_readme_installs() -> None:
    """The README's one-liner must install something that can open the window."""
    desktop = {_requirement_name(r) for r in _groups()["optional-dependencies.desktop"]}
    assert {"pywebview", "psutil"} <= desktop
    # It used to hold Qt, which the README still advertised as the desktop UI.
    assert "pyside6" not in desktop
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    assert "[desktop]" in readme


def test_every_imported_package_is_declared() -> None:
    """Check 1: imports and metadata agree.

    Tooling groups are excluded on purpose: a library module that reached for
    ``pytest`` should fail here, not be quietly excused by the dev extra.
    """
    declared = _declared_distributions(_runtime_groups())
    mapped = {name.lower() for names in DIST_TO_IMPORTS.values() for name in names}
    mapped |= declared
    offenders: list[str] = []
    for path in _package_files():
        for module in sorted(_imported_modules(path)):
            if module.lower() in mapped or module in UNDECLARED_ALLOWED:
                continue
            offenders.append(f"{module} <- {path.relative_to(REPO_ROOT).as_posix()}")
    assert (
        not offenders
    ), "undeclared imports; add each to the right extra in pyproject.toml:\n" + "\n".join(
        sorted(offenders)
    )


def test_declared_map_covers_every_declared_distribution() -> None:
    """The import-name table must not rot when an extra gains a package."""
    unmapped = sorted(_declared_distributions(_runtime_groups()) - set(DIST_TO_IMPORTS))
    assert not unmapped, f"add these to DIST_TO_IMPORTS with their import names: {unmapped}"


def test_undeclared_exceptions_are_still_load_bearing() -> None:
    """A stale exception would hide the next real bug, so it must still be used."""
    imported: set[str] = set()
    for path in _package_files():
        imported |= {name.lower() for name in _imported_modules(path)}
    # Compared case-insensitively: a CLR namespace is capitalised (`System`), and this
    # check's job is "is the exception still used", not "did you type the case right".
    stale = sorted(key for key in UNDECLARED_ALLOWED if key.lower() not in imported)
    assert not stale, f"nothing imports these any more, drop them: {stale}"


def test_desktop_path_needs_only_base_and_desktop_extras() -> None:
    """Check 2: ``pip install -e '.[desktop]'`` really is enough to open the HUD."""
    project = _metadata()["project"]
    allowed = {_requirement_name(r) for r in project.get("dependencies", ())}
    allowed |= {_requirement_name(r) for r in project["optional-dependencies"]["desktop"]}
    importable = {name.lower() for dist in allowed for name in DIST_TO_IMPORTS.get(dist, (dist,))}

    offenders: list[str] = []
    for relative in DESKTOP_EAGER_PATH:
        path = REPO_ROOT / relative
        if not path.is_file():  # a later phase has not landed yet
            continue
        for module in sorted(_imported_modules(path)):
            # The same exceptions check 1 grants: a CLR namespace reached through
            # pythonnet is not something `.[desktop]` could install any differently,
            # because pywebview is what brings pythonnet.
            if module in UNDECLARED_ALLOWED:
                continue
            if module.lower() not in importable:
                offenders.append(f"{relative} imports {module!r}")
    assert not offenders, "desktop start-up reaches outside base + [desktop]:\n" + "\n".join(
        offenders
    )


def test_desktop_eager_path_files_still_exist() -> None:
    missing = [name for name in DESKTOP_EAGER_MUST_EXIST if not (REPO_ROOT / name).is_file()]
    assert not missing, f"desktop start-up files vanished, update this test: {missing}"


def test_web_bundle_is_declared_and_present() -> None:
    """Check 3: a wheel built from this tree must actually contain the HUD.

    ``jarvis/ui/web`` is gitignored, so this can only assert what is true for
    whoever builds: the ``package-data`` glob is there, it matches files on disk,
    and ``index.html`` references nothing that is missing. Building a wheel that
    installs a blank window is the failure this guards.
    """
    data = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    declared = data["tool"]["setuptools"]["package-data"]
    assert "web/**/*" in declared.get("jarvis.ui", []), "package-data must ship the HUD bundle"

    from jarvis.ui.desktop import WEB_DIR, bundle_hint, index_path

    assert bundle_hint() == "", bundle_hint()
    matched = [
        p for pattern in declared["jarvis.ui"] for p in WEB_DIR.parent.glob(pattern) if p.is_file()
    ]
    assert index_path() in matched, "the glob must cover index.html itself"
    # ``**`` recursion is the part setuptools versions have disagreed about, and the
    # hashed JS/CSS live one level down. Verified end to end by building a wheel and
    # finding jarvis/ui/web/assets/index-*.js inside it.
    assert any("assets" in p.parts for p in matched), "the glob must reach web/assets/"


def test_build_desktop_script_verifies_the_bundle() -> None:
    """The build script's --check path is the same judgement the test makes."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "build_desktop", REPO_ROOT / "scripts" / "build_desktop.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.verify() == 0


def test_missing_monitor_dependency_degrades_instead_of_killing_startup() -> None:
    """A machine without psutil should lose telemetry, not lose the application."""
    from jarvis.app.system_service import SystemService
    from jarvis.core.exceptions import ToolError

    def boom() -> object:
        raise ToolError("system monitoring requires the 'psutil' package")

    service = SystemService(boom)  # type: ignore[arg-type]
    service.start()
    assert service.running is False
    report = service.report()
    assert "遥测读取失败" in report.error and "psutil" in report.error, report.error


def test_pyinstaller_spec_ships_the_web_bundle() -> None:
    """A frozen app has no ``package-data``: the spec must carry the HUD itself.

    The wheel path is covered by the ``package-data`` assertion above; PyInstaller
    ignores that entirely, so ``jarvis/ui/web`` and ``defaults.yaml`` have to appear
    in ``datas`` or the exe starts and shows nothing but a build hint.
    """
    spec = (REPO_ROOT / "packaging" / "jarvis.spec").read_text(encoding="utf-8")
    # Comments explain why a line is gone; they must not be what a test reads as
    # "still there". Prose is checked by a human, code by this test.
    code = "\n".join(line for line in spec.splitlines() if not line.lstrip().startswith("#"))

    assert (
        '"jarvis.ui.web"' in spec or '"jarvis", "ui", "web"' in spec
    ), "the spec must bundle the built HUD into jarvis/ui/web"
    assert "defaults.yaml" in spec, "the spec must bundle the built-in configuration"
    assert os.path.isfile(REPO_ROOT / "packaging" / "entry.py"), "the frozen entry point is missing"
    # funasr resolves model classes through an import-time registry; without every
    # submodule collected the frozen build fails with 'NoneType' object is not callable.
    assert 'collect_submodules("funasr")' in code, "funasr submodules must be collected"
    # Collecting the code is not enough: funasr fills the registry by walking its own
    # package directory, which a frozen tree does not have. The sources must ship too.
    assert 'find_spec("funasr")' in code, "the spec must ship funasr's source tree"
    # Measured, not assumed: this call looks right and returns zero entries.
    assert 'includes=["**/*.py"]' not in code, "collect_data_files does not ship .py sources"
    assert os.path.isfile(
        REPO_ROOT / "packaging" / "hooks" / "runtime_hook_funasr.py"
    ), "the frozen funasr diagnostic hook is missing"
