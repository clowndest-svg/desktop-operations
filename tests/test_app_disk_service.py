"""The disk service is the only door the HUD has onto deletion.

Two properties are worth pinning here because neither is visible from L2:

* every field an approval needs must travel to the page and back intact -- a
  dropped ``kind`` would turn "delete these 1,646 temp files" into "rmtree this
  folder";
* garbage from the page is refused before the tool ever sees it.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any, cast

import pytest

from jarvis.app.disk_service import DiskPlan, DiskService
from jarvis.tools.disk_cleaner import KIND_LOOSE_FILES, DiskCleaner, JunkItem

_FILE_COUNT = 12


def _rows(plan: DiskPlan) -> list[dict[str, Any]]:
    return [row for group in plan.groups for row in cast("list[dict[str, Any]]", group["items"])]


@pytest.fixture()
def root(tmp_path: Path) -> Path:
    directory = tmp_path / "temp"
    directory.mkdir()
    older = time.time() - 60.0
    for index in range(_FILE_COUNT):
        path = directory / f"tmp{index:04d}.tmp"
        path.write_bytes(b"x" * 32)
        os.utime(path, (older, older))
    return directory


@pytest.fixture()
def service(tmp_path: Path, root: Path) -> DiskService:
    return DiskService(
        lambda: DiskCleaner(
            audit_log=tmp_path / "audit" / "deletions.jsonl",
            sources={"临时文件": (root,)},
            protected_dirs=(tmp_path / "Jarvis",),
        )
    )


class TestLifecycle:
    def test_plan_before_start_reports_instead_of_crashing(self, tmp_path: Path) -> None:
        service = DiskService(lambda: DiskCleaner(audit_log=tmp_path / "a.jsonl", sources={}))

        plan = service.plan()

        assert plan.groups == ()
        assert plan.error

    def test_clean_after_stop_reports_instead_of_deleting(self, service: DiskService) -> None:
        service.start()
        service.stop()

        outcome = service.clean([{"path": "C:\\\\temp", "kind": KIND_LOOSE_FILES}], confirmed=True)

        assert outcome.deleted == ()
        assert outcome.error


class TestPlanPayload:
    def test_every_field_an_approval_needs_travels_to_the_page(self, service: DiskService) -> None:
        service.start()

        row = _rows(service.plan())[0]

        assert set(row) >= {
            "path",
            "size_bytes",
            "category",
            "is_directory",
            "modified_at",
            "kind",
            "member_count",
            "sample",
            "scanned_at",
        }
        assert row["kind"] == KIND_LOOSE_FILES
        assert row["member_count"] == _FILE_COUNT
        assert isinstance(row["sample"], list)

    def test_the_page_can_send_back_what_it_was_shown(
        self, service: DiskService, root: Path
    ) -> None:
        """Round-tripping through JSON must not quietly downgrade an aggregate."""
        service.start()
        row = _rows(service.plan())[0]

        outcome = service.clean([row], confirmed=True)

        assert outcome.error == ""
        assert list(outcome.deleted) == [str(root)]
        assert root.is_dir(), "the folder itself is never part of the approval"
        assert list(root.iterdir()) == []

    def test_the_audit_line_lands_in_its_own_directory(
        self, service: DiskService, root: Path, tmp_path: Path
    ) -> None:
        service.start()
        row = _rows(service.plan())[0]

        outcome = service.clean([row], confirmed=True)

        assert Path(outcome.log_path) == tmp_path / "audit" / "deletions.jsonl"
        assert Path(outcome.log_path).is_file()


class TestMalformedApprovals:
    def test_entries_that_are_not_items_are_refused_before_the_tool(
        self, service: DiskService, root: Path
    ) -> None:
        service.start()

        outcome = service.clean(["C:\\\\Users\\\\me\\\\Documents", {"no": "path"}], confirmed=True)

        assert list(outcome.deleted) == []
        assert len(outcome.failed) == 2
        assert all("条目" in reason for _path, reason in outcome.failed)
        assert len(list(root.iterdir())) == _FILE_COUNT

    def test_an_unconfirmed_request_becomes_an_error_not_an_exception(
        self, service: DiskService, root: Path
    ) -> None:
        service.start()
        row = _rows(service.plan())[0]

        outcome = service.clean([row], confirmed=False)

        assert outcome.error
        assert len(list(root.iterdir())) == _FILE_COUNT


class TestJunkItemRebuild:
    def test_missing_descriptive_fields_default_instead_of_failing(self) -> None:
        item = JunkItem.from_wire({"path": "C:\\\\temp\\\\a.tmp", "scanned_at": 1.5})

        assert item is not None
        assert item.category == ""
        assert item.member_count == 1
        assert item.aggregates_loose_files is False


class TestDigest:
    """The text the 协助分析 button hands to the model.

    Pinned because it is the only place a scan becomes something a model can read,
    and a digest that silently says nothing is indistinguishable from a model that
    had nothing to say.
    """

    @staticmethod
    def _two_groups(tmp_path: Path, root: Path) -> DiskService:
        cache = tmp_path / "cache"
        cache.mkdir()
        (cache / "big.bin").write_bytes(b"y" * 4096)
        old = time.time() - 60.0
        os.utime(cache / "big.bin", (old, old))
        return DiskService(
            lambda: DiskCleaner(
                audit_log=tmp_path / "audit.jsonl",
                sources={"浏览器缓存": (cache,), "临时文件": (root,)},
                protected_dirs=(),
            )
        )

    def test_nothing_to_say_before_the_first_scan(self, service: DiskService) -> None:
        assert service.digest() == ""
        assert service.last_plan is None

    def test_a_failed_scan_produces_no_digest(self, tmp_path: Path) -> None:
        service = DiskService(lambda: DiskCleaner(audit_log=tmp_path / "a.jsonl", sources={}))

        assert service.plan().error
        assert service.digest() == ""

    def test_digest_names_every_category_and_the_largest_rows(
        self, tmp_path: Path, root: Path
    ) -> None:
        service = self._two_groups(tmp_path, root)
        service.start()
        service.plan()

        text = service.digest()

        assert "浏览器缓存" in text
        assert "临时文件" in text
        assert "最大的" in text
        # 4 KB against 32-byte leftovers: the ordering is the point of the digest, so
        # a row list that came out alphabetical would still "contain" everything.
        assert text.index("big.bin") < text.index("tmp0000")

    def test_digest_counts_protected_skips_rather_than_hiding_them(
        self, tmp_path: Path, root: Path
    ) -> None:
        service = DiskService(
            lambda: DiskCleaner(
                audit_log=tmp_path / "audit.jsonl",
                sources={"临时文件": (root,)},
                protected_dirs=(root,),
            )
        )
        service.start()
        plan = service.plan()

        assert plan.skipped_protected > 0
        assert str(plan.skipped_protected) in service.digest()

    def test_stop_forgets_the_scan_it_was_describing(self, service: DiskService) -> None:
        service.start()
        service.plan()
        assert service.digest()

        service.stop()

        assert service.digest() == ""
