"""Tests for the disk cleaner's safety guarantees.

These use a real filesystem (``tmp_path``) rather than mocks: the whole point of
this module is that certain paths must survive, and that claim is only worth
anything if it is checked against actual files.

Every test passes an explicit ``sources=`` map so it never scans, and never
could scan, the developer's real temp or cache directories.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from jarvis.core.exceptions import DangerousOperationRejectedError
from jarvis.tools.disk_cleaner import DiskCleaner, JunkItem


@pytest.fixture()
def tree(tmp_path: Path) -> dict[str, Path]:
    """A small fake junk layout: two categories plus a protected interloper."""
    temp = tmp_path / "temp"
    temp.mkdir()
    (temp / "loose.log").write_bytes(b"x" * 500)
    cache = temp / "buildcache"
    (cache / "nested").mkdir(parents=True)
    (cache / "a.bin").write_bytes(b"y" * 1000)
    (cache / "nested" / "b.bin").write_bytes(b"z" * 2000)

    # Sits inside a scanned root but must never be offered or deleted.
    windows = temp / "Windows"
    windows.mkdir()
    (windows / "kernel.sys").write_bytes(b"k" * 100)

    other = tmp_path / "pipcache"
    other.mkdir()
    (other / "wheel.whl").write_bytes(b"w" * 300)
    return {"temp": temp, "other": other, "windows": windows, "cache_dir": cache}


@pytest.fixture()
def cleaner(tmp_path: Path, tree: dict[str, Path]) -> DiskCleaner:
    return DiskCleaner(
        audit_log=tmp_path / "audit.jsonl",
        sources={"临时文件": (tree["temp"],), "包管理缓存": (tree["other"],)},
    )


def _exact(*paths: str | Path) -> tuple[JunkItem, ...]:
    """Approvals for single entries, exactly as a scan would have listed them."""
    return tuple(
        JunkItem(
            path=str(path),
            size_bytes=0,
            category="临时文件",
            is_directory=Path(path).is_dir(),
            modified_at=0.0,
        )
        for path in paths
    )


def _paths(report: object) -> set[str]:
    return {item.path for group in report.groups for item in group.items}  # type: ignore[attr-defined]


class TestScan:
    def test_lists_one_level_per_category(
        self, cleaner: DiskCleaner, tree: dict[str, Path]
    ) -> None:
        report = cleaner.scan()

        assert {group.category for group in report.groups} == {"临时文件", "包管理缓存"}
        listed = _paths(report)
        # Children of the root, not every nested file.
        assert str(tree["cache_dir"]) in listed
        assert str(tree["temp"] / "loose.log") in listed
        assert str(tree["other"] / "wheel.whl") in listed
        assert len(listed) == 3  # 2 under temp (Windows excluded) + 1 under the cache root

    def test_directories_report_recursive_size(
        self, cleaner: DiskCleaner, tree: dict[str, Path]
    ) -> None:
        report = cleaner.scan()
        sizes = {item.path: item.size_bytes for group in report.groups for item in group.items}

        assert sizes[str(tree["cache_dir"])] == 3000  # 1000 + 2000, nested counted
        assert sizes[str(tree["temp"] / "loose.log")] == 500

    def test_protected_path_is_never_listed(
        self, cleaner: DiskCleaner, tree: dict[str, Path]
    ) -> None:
        report = cleaner.scan()

        assert str(tree["windows"]) not in _paths(report)
        assert report.skipped_protected >= 1

    def test_scan_deletes_nothing(self, cleaner: DiskCleaner, tree: dict[str, Path]) -> None:
        cleaner.scan()

        assert tree["cache_dir"].is_dir()
        assert (tree["temp"] / "loose.log").exists()

    def test_total_matches_group_sum(self, cleaner: DiskCleaner) -> None:
        report = cleaner.scan()

        assert report.total_bytes == sum(group.total_bytes for group in report.groups)


class TestDeleteRequiresConfirmation:
    def test_missing_confirmation_raises(self, cleaner: DiskCleaner, tree: dict[str, Path]) -> None:
        target = tree["temp"] / "loose.log"

        with pytest.raises(DangerousOperationRejectedError, match="without explicit confirmation"):
            cleaner.delete(_exact(target), confirmed=False)

        assert target.exists()

    def test_positional_confirmed_is_impossible(
        self, cleaner: DiskCleaner, tree: dict[str, Path]
    ) -> None:
        # ``confirmed`` is keyword-only so no caller can delete by accident.
        with pytest.raises(TypeError):
            cleaner.delete(_exact(tree["temp"] / "loose.log"), True)  # type: ignore[call-arg]


class TestProtectedPathsSurvive:
    def test_explicitly_requested_protected_path_is_refused(
        self, cleaner: DiskCleaner, tree: dict[str, Path]
    ) -> None:
        victim = tree["windows"] / "kernel.sys"

        result = cleaner.delete(_exact(victim), confirmed=True)

        assert victim.exists(), "a protected file was deleted"
        assert result.deleted == ()
        assert result.freed_bytes == 0
        assert result.failed == ((str(victim), "protected path"),)

    def test_caller_supplied_protected_dir_cannot_be_deleted(
        self, tmp_path: Path, tree: dict[str, Path]
    ) -> None:
        """JARVIS' own data dir is protected by an argument, not by a name pattern.

        It lives under ``%LOCALAPPDATA%`` like any other cache, so the line between
        "the assistant's models and audit trail" and "somebody's leftover junk" is
        one constructor argument wide -- which is why it is asserted, not assumed.
        """
        jarvis_home = tmp_path / "Jarvis"
        model = jarvis_home / "models" / "sensevoice" / "model.pt"
        model.parent.mkdir(parents=True)
        model.write_bytes(b"x" * 8)
        cleaner = DiskCleaner(
            jarvis_home / "audit" / "deletions.jsonl",
            sources={"临时文件": (tmp_path / "temp",)},
            protected_dirs=(jarvis_home,),
        )
        result = cleaner.delete(_exact(model), confirmed=True)
        assert result.deleted == ()
        assert any("protected" in reason for _path, reason in result.failed), result.failed
        assert model.is_file()
        assert not (jarvis_home / "audit").exists(), "a refused delete must not log"

    def test_forward_slash_spelling_cannot_slip_past_the_blocklist(
        self, cleaner: DiskCleaner, tree: dict[str, Path]
    ) -> None:
        mixed = str(tree["windows"] / "kernel.sys").replace("\\", "/")

        result = cleaner.delete(_exact(mixed), confirmed=True)

        assert (tree["windows"] / "kernel.sys").exists()
        assert result.failed[0][1] == "protected path"


class TestDeletion:
    def test_selected_items_are_removed_and_bytes_counted(
        self, cleaner: DiskCleaner, tree: dict[str, Path]
    ) -> None:
        log = tree["temp"] / "loose.log"
        cache_dir = tree["cache_dir"]

        result = cleaner.delete(_exact(log, cache_dir), confirmed=True)

        assert not log.exists()
        assert not cache_dir.exists()
        assert result.freed_bytes == 3500
        assert result.failed == ()
        # Untouched: the sibling we did not select.
        assert (tree["other"] / "wheel.whl").exists()

    def test_missing_path_is_reported_not_raised(
        self, cleaner: DiskCleaner, tree: dict[str, Path]
    ) -> None:
        ghost = tree["temp"] / "already-gone"

        result = cleaner.delete(_exact(ghost), confirmed=True)

        assert result.deleted == ()
        assert "FileNotFoundError" in result.failed[0][1]

    def test_noop_delete_writes_no_audit_line(self, cleaner: DiskCleaner, tmp_path: Path) -> None:
        cleaner.delete((), confirmed=True)

        assert not (tmp_path / "audit.jsonl").exists()


class TestAuditLog:
    def test_each_action_appends_one_json_line(
        self, cleaner: DiskCleaner, tree: dict[str, Path], tmp_path: Path
    ) -> None:
        cleaner.delete(_exact(tree["temp"] / "loose.log"), confirmed=True)
        cleaner.delete(_exact(tree["other"] / "wheel.whl"), confirmed=True)

        lines = (tmp_path / "audit.jsonl").read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 2
        first = json.loads(lines[0])
        assert first["count"] == 1
        assert first["freed_bytes"] == 500
        assert first["paths"] == [str(tree["temp"] / "loose.log")]
        assert "at" in first

    def test_audit_log_is_created_on_demand(self, tmp_path: Path, tree: dict[str, Path]) -> None:
        log = tmp_path / "deep" / "nested" / "audit.jsonl"
        cleaner = DiskCleaner(audit_log=log, sources={"临时文件": (tree["temp"],)})

        cleaner.delete(_exact(tree["temp"] / "loose.log"), confirmed=True)

        assert log.is_file()
