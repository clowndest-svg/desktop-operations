"""Loose-file aggregation: what one checkbox may and may not cover.

A folder of a thousand ``tmp*.tmp`` files is listed as a single row, because
nobody reads a thousand-row checklist -- and a UI that invites "select all" is a
UI that deletes things nobody approved. Collapsing the list is only safe while the
delete stays inside what the list meant, so most of these tests are about what must
*survive* an aggregate approval:

* the folder itself (the row names it, but stands for its files),
* its subdirectories,
* files that appeared after the scan,
* entries carried over from a scan that is no longer current,
* protected siblings.

The JSON round-trip tests cover the other half of the contract: the page echoes
what it was shown, and an approval that arrives mangled must be refused rather than
guessed at.
"""

from __future__ import annotations

import dataclasses
import json
import os
import time
from pathlib import Path

import pytest

from jarvis.tools.disk_cleaner import (
    KIND_EXACT,
    KIND_LOOSE_FILES,
    DiskCleaner,
    JunkItem,
    JunkReport,
)


def _make_loose_files(root: Path, count: int, *, size: int = 64) -> list[Path]:
    """Fill ``root`` with leftovers, backdated so any later scan sees them as old."""
    root.mkdir(parents=True, exist_ok=True)
    older = time.time() - 60.0
    made: list[Path] = []
    for index in range(count):
        path = root / f"tmp{index:04d}.tmp"
        path.write_bytes(b"x" * size)
        os.utime(path, (older, older))
        made.append(path)
    return made


def _rows(report: JunkReport) -> list[JunkItem]:
    return [item for group in report.groups for item in group.items]


def _only_item(report: JunkReport) -> JunkItem:
    items = _rows(report)
    assert len(items) == 1, items
    return items[0]


def _aggregate(report: JunkReport) -> JunkItem:
    aggregates = [item for item in _rows(report) if item.kind == KIND_LOOSE_FILES]
    assert len(aggregates) == 1, _rows(report)
    return aggregates[0]


@pytest.fixture()
def root(tmp_path: Path) -> Path:
    return tmp_path / "temp"


@pytest.fixture()
def cleaner(tmp_path: Path, root: Path) -> DiskCleaner:
    return DiskCleaner(audit_log=tmp_path / "audit.jsonl", sources={"临时文件": (root,)})


class TestScanShape:
    def test_many_loose_files_collapse_into_one_row(self, cleaner: DiskCleaner, root: Path) -> None:
        _make_loose_files(root, 12)

        report = cleaner.scan()
        row = _only_item(report)

        assert row.kind == KIND_LOOSE_FILES
        assert row.path == str(root)
        assert row.member_count == 12
        assert row.size_bytes == 12 * 64
        assert len(row.sample) <= 5
        assert "tmp0000.tmp" in row.sample
        assert row.scanned_at == pytest.approx(report.scanned_at)

    def test_the_row_is_never_a_directory_entry(self, cleaner: DiskCleaner, root: Path) -> None:
        """Approving this row must not read as "delete the folder"."""
        _make_loose_files(root, 12)

        assert _only_item(cleaner.scan()).is_directory is False

    def test_the_biggest_junk_is_listed_first(self, tmp_path: Path, root: Path) -> None:
        """A wall of 0-byte files must not bury the one row worth deleting.

        The name-sorted list did exactly that on a real machine: the first screen
        was all ``tmp*.tmp`` at 0 B and the 3 GB entry sat hundreds of rows down.
        """
        root.mkdir(parents=True, exist_ok=True)
        big = root / "cache"
        big.mkdir()
        (big / "blob.bin").write_bytes(b"b" * 5000)
        for index in range(5):
            (root / f"tiny{index}.tmp").write_bytes(b"x")
        cleaner = DiskCleaner(audit_log=tmp_path / "audit.jsonl", sources={"临时文件": (root,)})

        items = _rows(cleaner.scan())

        assert items[0].path == str(big), "the 5 KB folder should lead, not a 1 B file"
        assert items[0].size_bytes == 5000
        # Below it, the equal-sized leftovers in name order: stable, so two scans
        # of an unchanged folder show the same list.
        assert [item.path for item in items[1:]] == [str(root / f"tiny{i}.tmp") for i in range(5)]

    def test_few_loose_files_stay_listed_individually(
        self, cleaner: DiskCleaner, root: Path
    ) -> None:
        """Three leftovers are worth reading one by one; three thousand are not."""
        files = _make_loose_files(root, 3)

        items = [item for group in cleaner.scan().groups for item in group.items]

        assert {item.path for item in items} == {str(path) for path in files}
        assert all(item.kind == KIND_EXACT for item in items)

    def test_subdirectories_keep_their_own_rows(self, cleaner: DiskCleaner, root: Path) -> None:
        _make_loose_files(root, 12)
        (root / "Chrome" / "Cache").mkdir(parents=True)

        report = cleaner.scan()
        items = [item for group in report.groups for item in group.items]

        assert len(items) == 2
        directory_row = next(item for item in items if item.is_directory)
        assert directory_row.kind == KIND_EXACT
        assert directory_row.path == str(root / "Chrome")

    def test_protected_sibling_is_neither_counted_nor_offered(
        self, cleaner: DiskCleaner, root: Path
    ) -> None:
        _make_loose_files(root, 12)
        guarded = root / "windows.dll"
        guarded.write_bytes(b"d" * 40)

        row = _only_item(cleaner.scan())

        assert row.member_count == 12, "a protected file slipped into the aggregate"
        assert guarded.name not in row.sample


class TestAggregateDelete:
    def test_it_removes_the_files_and_keeps_the_folder(
        self, cleaner: DiskCleaner, root: Path
    ) -> None:
        files = _make_loose_files(root, 12)
        keeper = root / "buildcache"
        keeper.mkdir()
        (keeper / "nested.bin").write_bytes(b"k" * 10)
        row = _aggregate(cleaner.scan())

        result = cleaner.delete((row,), confirmed=True)

        assert root.is_dir(), "the aggregate approved files, not the folder"
        assert keeper.is_dir() and (keeper / "nested.bin").is_file()
        assert all(not path.exists() for path in files)
        assert result.freed_bytes == 12 * 64
        assert result.deleted == (str(root),)
        assert result.failed == ()

    def test_a_file_created_after_the_scan_survives(self, cleaner: DiskCleaner, root: Path) -> None:
        """The approval covers the list a human read, not whatever landed since."""
        _make_loose_files(root, 12)
        row = _only_item(cleaner.scan())

        latecomer = root / "arrived-later.tmp"
        latecomer.write_bytes(b"new" * 10)
        future = row.scanned_at + 120.0
        os.utime(latecomer, (future, future))

        result = cleaner.delete((row,), confirmed=True)

        assert latecomer.is_file(), "a file nobody approved was deleted"
        assert result.freed_bytes == 12 * 64
        assert result.deleted == (str(root),)

    def test_an_approval_from_an_older_scan_is_refused(
        self, cleaner: DiskCleaner, root: Path
    ) -> None:
        """A stale checklist must not delete: that folder may mean something else now."""
        files = _make_loose_files(root, 12)
        stale = _only_item(cleaner.scan())
        cleaner.scan()  # a newer scan exists, so the old row is no longer the plan
        outdated = dataclasses.replace(stale, scanned_at=stale.scanned_at - 3600.0)

        result = cleaner.delete((outdated,), confirmed=True)

        assert all(path.is_file() for path in files)
        assert result.deleted == ()
        assert "重新扫描" in result.failed[0][1], result.failed

    def test_an_aggregate_without_a_scan_is_refused(self, cleaner: DiskCleaner, root: Path) -> None:
        files = _make_loose_files(root, 12)
        fabricated = JunkItem(
            path=str(root),
            size_bytes=12 * 64,
            category="临时文件",
            is_directory=False,
            modified_at=time.time(),
            kind=KIND_LOOSE_FILES,
            member_count=12,
            scanned_at=time.time(),
        )

        result = cleaner.delete((fabricated,), confirmed=True)

        assert all(path.is_file() for path in files)
        assert "尚未扫描" in result.failed[0][1], result.failed

    def test_a_protected_folder_is_never_expanded(self, tmp_path: Path) -> None:
        """Even if a protected root were misconfigured as a source, delete refuses."""
        guarded = tmp_path / "Documents"
        files = _make_loose_files(guarded, 12)
        cleaner = DiskCleaner(
            audit_log=tmp_path / "audit.jsonl",
            sources={"临时文件": (guarded,)},
            protected_dirs=(guarded,),
        )
        row = JunkItem(
            path=str(guarded),
            size_bytes=12 * 64,
            category="临时文件",
            is_directory=False,
            modified_at=0.0,
            kind=KIND_LOOSE_FILES,
            member_count=12,
            scanned_at=cleaner.scan().scanned_at,
        )

        result = cleaner.delete((row,), confirmed=True)

        assert all(path.is_file() for path in files)
        assert "protected" in result.failed[0][1], result.failed

    def test_expanded_members_are_audited_not_just_the_row(
        self, cleaner: DiskCleaner, root: Path, tmp_path: Path
    ) -> None:
        """ "What did you delete" cannot be answered with a folder name."""
        files = _make_loose_files(root, 12)
        row = _only_item(cleaner.scan())

        cleaner.delete((row,), confirmed=True)

        lines = (tmp_path / "audit.jsonl").read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 1
        record = json.loads(lines[0])
        assert record["count"] == 12
        assert record["approved"] == [str(root)]
        assert sorted(record["paths"]) == sorted(str(path) for path in files)


class TestApprovalRoundTrip:
    """The page echoes scanned entries back; nothing may be lost on the way."""

    def test_from_wire_rebuilds_an_aggregate_exactly(
        self, cleaner: DiskCleaner, root: Path
    ) -> None:
        _make_loose_files(root, 12)
        row = _only_item(cleaner.scan())
        payload = {
            "path": row.path,
            "size_bytes": row.size_bytes,
            "category": row.category,
            "is_directory": row.is_directory,
            "modified_at": row.modified_at,
            "kind": row.kind,
            "member_count": row.member_count,
            "sample": list(row.sample),
            "scanned_at": row.scanned_at,
        }

        assert JunkItem.from_wire(payload) == row

    @pytest.mark.parametrize(
        ("payload", "why"),
        [
            ({}, "no path"),
            ({"path": ""}, "empty path"),
            ({"path": 123}, "path is not a string"),
            ({"path": "C:\\\\temp", "kind": KIND_LOOSE_FILES}, "aggregate without a stamp"),
        ],
    )
    def test_from_wire_refuses_entries_it_cannot_trust(
        self, payload: dict[str, object], why: str
    ) -> None:
        assert JunkItem.from_wire(payload) is None, why

    def test_an_unknown_kind_narrows_to_a_single_path(self) -> None:
        """An unrecognised kind must not unlock anything.

        Falling back to ``exact`` means the worst a forged entry can do is name one
        path -- the narrowest thing this tool is willing to delete.
        """
        item = JunkItem.from_wire({"path": "C:\\\\temp\\\\x.tmp", "kind": "rmtree-everything"})

        assert item is not None
        assert item.kind == KIND_EXACT
        assert item.aggregates_loose_files is False

    def test_non_string_sample_entries_are_dropped(self) -> None:
        item = JunkItem.from_wire(
            {"path": "C:\\\\temp", "kind": KIND_LOOSE_FILES, "scanned_at": 1.0, "sample": ["a", 3]}
        )

        assert item is not None and item.sample == ("a",)
