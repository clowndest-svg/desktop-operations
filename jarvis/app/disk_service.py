"""Disk service: the application-layer door onto the confirmed-delete tool.

The UI never calls the L2 cleaner directly (architecture rule 6), and never
decides on its own what is safe. It asks for a plan, shows it, and sends back
the exact paths a human ticked — with confirmation re-checked at the tool.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from jarvis.core.exceptions import JarvisError
from jarvis.tools.disk_cleaner import JunkItem

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Callable, Sequence

    from jarvis.tools.disk_cleaner import DiskCleaner

logger = logging.getLogger("jarvis.app.disk_service")


def _item_to_dict(item: JunkItem) -> dict[str, object]:
    """One approvable entry, in the shape the page renders and echoes back.

    Every field the tool needs to honour the approval travels with it -- ``kind``
    and ``scanned_at`` included -- so what the human ticked is what gets deleted.
    """
    return {
        "path": item.path,
        "size_bytes": item.size_bytes,
        "category": item.category,
        "is_directory": item.is_directory,
        "modified_at": item.modified_at,
        "kind": item.kind,
        "member_count": item.member_count,
        "sample": list(item.sample),
        "scanned_at": item.scanned_at,
    }


@dataclass(frozen=True, slots=True)
class DiskPlan:
    """What a scan found, in a shape the page can render directly."""

    groups: tuple[dict[str, object], ...]
    total_bytes: int
    skipped_protected: int
    truncated: bool
    error: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "groups": list(self.groups),
            "total_bytes": self.total_bytes,
            "skipped_protected": self.skipped_protected,
            "truncated": self.truncated,
            "error": self.error,
        }


@dataclass(frozen=True, slots=True)
class DiskOutcome:
    deleted: Sequence[str]
    freed_bytes: int
    failed: Sequence[tuple[str, str]]
    log_path: str
    error: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "deleted": list(self.deleted),
            "freed_bytes": self.freed_bytes,
            "failed": [list(pair) for pair in self.failed],
            "log_path": self.log_path,
            "error": self.error,
        }


class DiskService:
    """Owns the cleaner and keeps its results JSON-ready for the desktop shell."""

    name = "disk"

    def __init__(self, cleaner_factory: Callable[[], DiskCleaner]) -> None:
        self._cleaner_factory = cleaner_factory
        self._cleaner: DiskCleaner | None = None

    def start(self) -> None:
        if self._cleaner is not None:
            return
        self._cleaner = self._cleaner_factory()
        logger.info("disk service ready")

    def stop(self) -> None:
        self._cleaner = None

    def plan(self) -> DiskPlan:
        """Scan for junk. Read-only; safe to call whenever the page asks."""
        cleaner = self._cleaner
        if cleaner is None:
            return DiskPlan((), 0, 0, False, "磁盘服务未启动")
        try:
            report = cleaner.scan()
        except JarvisError as exc:
            logger.error("disk scan failed: %s", exc)
            return DiskPlan((), 0, 0, False, str(exc))
        groups = tuple(
            {
                "category": group.category,
                "total_bytes": group.total_bytes,
                "items": [_item_to_dict(item) for item in group.items],
            }
            for group in report.groups
        )
        return DiskPlan(
            groups=groups,
            total_bytes=report.total_bytes,
            skipped_protected=report.skipped_protected,
            truncated=report.truncated,
        )

    def clean(self, items: list[object], *, confirmed: bool) -> DiskOutcome:
        """Delete exactly what the operator ticked, if they confirmed.

        The page sends back the entries it was shown, as JSON objects. Anything it
        sends that is not one of those entries is dropped here, before the tool ever
        sees it: a delete request that arrived intact is worth more than a message
        saying "I tried to delete something".
        """
        cleaner = self._cleaner
        if cleaner is None:
            return DiskOutcome((), 0, (), "", "磁盘服务未启动")
        approved: list[JunkItem] = []
        rejected: list[tuple[str, str]] = []
        for raw in items:
            mapping = raw if isinstance(raw, dict) else {}
            item = JunkItem.from_wire(mapping)
            if item is None:
                rejected.append((str(raw)[:120], "不是本次扫描列出的条目"))
                continue
            approved.append(item)
        if rejected:
            logger.error("disk cleanup rejected %d malformed entry(ies)", len(rejected))
        try:
            result = cleaner.delete(tuple(approved), confirmed=confirmed)
        except JarvisError as exc:
            logger.error("disk cleanup refused: %s", exc)
            return DiskOutcome((), 0, (), "", str(exc))
        logger.info(
            "disk cleanup removed %d item(s), freed %d bytes",
            len(result.deleted),
            result.freed_bytes,
        )
        return DiskOutcome(
            deleted=result.deleted,
            freed_bytes=result.freed_bytes,
            failed=tuple(rejected) + result.failed,
            log_path=result.log_path,
        )
