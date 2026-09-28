"""Architecture enforcement: the layering rules in ``docs/architecture.md`` are code.

The document has always said "依赖只能自上而下". It was honoured by convention, and
convention leaked: ``jarvis/ui/state_bridge.py`` imported ``jarvis.orchestration.types``
to get the ``PipelineEvent`` type, which is exactly the rule 6 violation this test
exists to catch (it is why ``PipelineEvent`` now lives in ``jarvis/core/events.py``).

Enforced here:

* **rule 1** - a package may only import packages its allowed-dependency row lists.
* **rule 2 (registration)** - every subpackage of ``jarvis`` must appear in the table,
  so a new package cannot arrive without somebody deciding where it sits.
* **rule 3** - ``core`` is stdlib-only: no ``jarvis`` imports at all.
* **rule 6** - ``ui`` sees ``core``, ``config`` and ``app`` and nothing else.

Deliberately NOT enforced: the second half of rule 2 ("同层协作只依赖对方
``__init__.py``"). Seventeen existing edges reach into a sibling's module
(``asr`` -> ``jarvis.vad.types``, ``wakeword`` -> ``jarvis.audio.source``, and most
of the rest into ``jarvis.config.schema``), all of them downward in spirit and
harmless to direction. Turning that clause on would be a rename-and-re-export
refactor with no bug behind it, so it stays a documented convention.
"""

from __future__ import annotations

import ast
import pathlib
import re

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
PACKAGE_DIR = REPO_ROOT / "jarvis"

# The composition root wires everything together and is exempt from the table:
# importing across layers is its job. ``jarvis/__init__.py`` is NOT exempt -- it must
# stay a version string, so it is checked like any other module.
COMPOSITION_ROOT = "jarvis/__main__.py"

# Mirrors the "允许依赖" column of docs/architecture.md section 2.
ALLOWED: dict[str, frozenset[str]] = {
    "core": frozenset(),
    "config": frozenset({"core"}),
    "logging": frozenset({"core", "config"}),
    "llm": frozenset({"core", "config", "logging", "prompt"}),
    "audio": frozenset({"core"}),
    "wakeword": frozenset({"core", "config", "audio"}),
    "vad": frozenset({"core", "config", "audio"}),
    "asr": frozenset({"core", "config", "vad"}),
    "tts": frozenset({"core", "config"}),
    "database": frozenset({"core", "config"}),
    "vector": frozenset({"core", "config"}),
    "ocr": frozenset({"core", "config"}),
    "browser": frozenset({"core", "config"}),
    "prompt": frozenset({"core", "config"}),
    "memory": frozenset({"core", "config", "database", "vector", "llm"}),
    "knowledge": frozenset({"core", "config", "database", "vector", "llm"}),
    "tools": frozenset({"core", "config"}),
    "vision": frozenset({"core", "config", "ocr", "llm"}),
    "computer": frozenset({"core", "config", "vision", "ocr"}),
    "mcp": frozenset({"core", "config", "tools"}),
    "plugins": frozenset({"core", "config", "tools"}),
    "agent": frozenset(
        {
            "core",
            "config",
            "llm",
            "prompt",
            "planner",
            "memory",
            "knowledge",
            "tools",
        }
    ),
    "orchestration": frozenset(
        {
            "core",
            "config",
            "llm",
            "asr",
            "tts",
            "vad",
            "wakeword",
            "audio",
            "agent",
        }
    ),
    "planner": frozenset({"core", "config", "llm", "prompt"}),
    "workflow": frozenset({"core", "config", "scheduler", "tools", "database"}),
    "scheduler": frozenset({"core", "config", "database"}),
    # L4: the only layer allowed to reach down into everything below it.
    "app": frozenset(
        {
            "core",
            "config",
            "logging",
            "llm",
            "audio",
            "wakeword",
            "vad",
            "asr",
            "tts",
            "database",
            "vector",
            "ocr",
            "browser",
            "prompt",
            "memory",
            "knowledge",
            "tools",
            "vision",
            "computer",
            "mcp",
            "plugins",
            "agent",
            "orchestration",
            "planner",
            "workflow",
            "scheduler",
        }
    ),
    # L5: rule 6, spelled out so the violation that started this test stays fixed.
    "ui": frozenset({"core", "config", "app"}),
    # Side-channel product: talks to the model directly, never to the app layer.
    "qqbot": frozenset({"core", "config", "llm"}),
}


def _relative(path: pathlib.Path) -> str:
    return path.relative_to(REPO_ROOT).as_posix()


def _package_of(path: pathlib.Path) -> str:
    """Which first-party package owns this file.

    Root modules get their own pseudo-packages: ``__main__`` is the composition root
    and is exempt, ``__init__`` must stay import-free so importing ``jarvis`` costs
    nothing.
    """
    parts = path.relative_to(PACKAGE_DIR).parts
    if len(parts) == 1:
        return parts[0][: -len(".py")]
    return parts[0]


def _imported_jarvis_packages(path: pathlib.Path) -> set[str]:
    """First-party package names a file imports, at any depth, lazily included."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    targets: set[str] = set()
    for node in ast.walk(tree):
        modules: list[str] = []
        if isinstance(node, ast.Import):
            modules = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            modules = [node.module]
        for module in modules:
            if module == "jarvis" or not module.startswith("jarvis."):
                continue
            targets.add(module.split(".")[1])
    return targets


def _files() -> list[pathlib.Path]:
    return [path for path in sorted(PACKAGE_DIR.rglob("*.py")) if "__pycache__" not in path.parts]


def test_every_package_is_registered_in_the_layer_table() -> None:
    on_disk = {
        part.name
        for part in PACKAGE_DIR.iterdir()
        if part.is_dir() and part.name not in {"__pycache__"}
    }
    unregistered = sorted(on_disk - set(ALLOWED))
    assert not unregistered, (
        f"packages missing from ALLOWED (decide where they sit, "
        f"and update docs/architecture.md too): {unregistered}"
    )
    stale = sorted(set(ALLOWED) - on_disk)
    assert not stale, f"ALLOWED lists packages that no longer exist: {stale}"


def test_no_layer_violations() -> None:
    """Rule 1 + rule 6: every edge appears in the owning package's allowed row."""
    offenders: list[str] = []
    for path in _files():
        source = _package_of(path)
        if source == "__main__":
            continue  # the composition root may wire anything together
        allowed = ALLOWED.get(source, frozenset() if source == "__init__" else None)
        if allowed is None:
            offenders.append(f"{_relative(path)}: unregistered package {source!r}")
            continue
        for target in sorted(_imported_jarvis_packages(path)):
            if target == source:
                continue
            if target not in allowed:
                offenders.append(
                    f"{_relative(path)}: {source} -> {target} "
                    f"(allowed: {', '.join(sorted(allowed)) or 'nothing'})"
                )
    assert not offenders, "layering violations:\n" + "\n".join(offenders)


def test_core_depends_on_nothing_outside_itself() -> None:
    """Rule 3: the shared kernel may only import the stdlib and its own modules.

    ``core/__init__.py`` re-exports from ``core.exceptions`` and friends, so the
    check is "nothing beyond core", not "nothing at all".
    """
    for path in sorted((PACKAGE_DIR / "core").rglob("*.py")):
        foreign = _imported_jarvis_packages(path) - {"core"}
        assert not foreign, f"{_relative(path)} imports {sorted(foreign)} but core is stdlib-only"


def test_ui_only_talks_to_app_core_and_config() -> None:
    """Rule 6, stated positively: the HUD may not reach into L1/L2/L3.

    ``state_bridge.py`` used to import ``jarvis.orchestration.types`` for one data
    class; that is how this assertion came to exist.
    """
    for path in sorted((PACKAGE_DIR / "ui").rglob("*.py")):
        for target in sorted(_imported_jarvis_packages(path) - {"ui"}):
            assert target in ALLOWED["ui"], f"{_relative(path)} imports jarvis.{target}"


def test_app_never_imports_the_presentation_layer() -> None:
    """The dependency arrow points down: ``app`` must not know ``ui`` exists."""
    for path in sorted((PACKAGE_DIR / "app").rglob("*.py")):
        assert "ui" not in _imported_jarvis_packages(path), _relative(path)


def test_architecture_doc_table_matches_the_enforced_table() -> None:
    """``docs/architecture.md`` section 2 is the contract; this keeps it honest.

    The document claims to be machine-checked, so the dependency column is compared
    against :data:`ALLOWED` cell by cell. A row that says "core, config, llm" while
    the code allows more (or less) is a documentation bug that would mislead the next
    person to add an import.
    """
    text = (REPO_ROOT / "docs" / "architecture.md").read_text(encoding="utf-8")
    documented: dict[str, str] = {}
    for line in text.splitlines():
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) < 4 or not cells[0].startswith("`"):
            continue
        documented[cells[0].strip("`")] = cells[3]

    assert set(documented) >= set(ALLOWED) - {"__main__"}, "every package needs a doc row"
    for package, allowed in ALLOWED.items():
        cell = documented.get(package)
        if cell is None:
            continue
        if cell in {"仅标准库", "所有下层"}:
            # Stated qualitatively on purpose: core imports nothing foreign (asserted
            # separately), and app may reach anything below it.
            continue
        listed = {token.strip() for token in re.split(r"[,/、]", cell) if token.strip()}
        assert listed == set(
            allowed
        ), f"{package}: doc says {sorted(listed)}, code allows {sorted(allowed)}"


def test_composition_root_is_still_the_only_place_that_wires_ui() -> None:
    """If this fails, the exemption above is being abused and must be re-scoped."""
    files = [p for p in _files() if _package_of(p) == "__main__"]
    assert [_relative(p) for p in files] == [COMPOSITION_ROOT]
    imported = _imported_jarvis_packages(files[0])
    assert {"ui", "app", "orchestration"} <= imported
