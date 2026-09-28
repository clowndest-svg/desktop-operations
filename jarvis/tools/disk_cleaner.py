"""Disk junk scanner and cleaner, behind an explicit confirmation gate.

Three properties are the whole point of this module; everything else is detail.

1. **Scanning is read-only.** ``scan()`` never mutates anything, so it is safe to
   run on a timer or to show a customer before they have agreed to anything.
2. **Protected paths cannot be overridden.** The blocklist is checked again at
   delete time, not only when the list was built, so a stale or hand-edited
   selection still cannot reach ``C:\\Windows``.
3. **Every deletion is logged.** A cleaner that cannot answer "what did you
   remove yesterday" is not something to ship to a stranger's machine.

Walks are bounded by depth and file count on purpose: this is a cache/temp
sweep, not a whole-disk index, and it has to return fast enough to drive a UI.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import time
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from jarvis.core.exceptions import DangerousOperationRejectedError

logger = logging.getLogger("jarvis.tools.disk_cleaner")

_MAX_DEPTH = 6
_MAX_ITEMS_PER_CATEGORY = 4000
_LOOSE_FILE_MIN = 10
"""Aggregate a directory level once this many loose files sit in it.

    Below the threshold every file is listed on its own: three leftovers in a
    folder are worth reading. Sixteen hundred in %TEMP% are only worth counting,
    and a sixteen-hundred-row checklist is how somebody ends up approving "select
    all" without reading anything.
    """

_MAX_EXPANDED_MEMBERS = 20_000
"""Hard ceiling on files removed through one aggregate approval."""

_STALE_SCAN_TOLERANCE = 1.0
"""Seconds an approval may lag the scan it refers to before it is refused."""

KIND_EXACT = "exact"
KIND_LOOSE_FILES = "loose_files"


# Where junk legitimately lives. Each entry is (category, environment-derived
# root); a missing root is skipped rather than treated as an error.
def _known_sources() -> dict[str, tuple[Path, ...]]:
    local = Path(os.environ.get("LOCALAPPDATA", "")) if os.name == "nt" else None
    sources: dict[str, tuple[Path, ...]] = {
        "临时文件": (Path(os.environ.get("TEMP", "/tmp")),),
        "包管理缓存": (),
        "浏览器缓存": (),
        "缩略图与图标缓存": (),
    }
    if local is None or not local.is_dir():
        return sources
    cache_candidates = (local / "pip" / "Cache", local.parent / "npm-cache", local / "npm-cache")
    sources["包管理缓存"] = tuple(path for path in cache_candidates if path.is_dir())
    sources["浏览器缓存"] = tuple(
        path
        for path in (
            local / "Google" / "Chrome" / "User Data" / "Default" / "Cache",
            local / "Microsoft" / "Edge" / "User Data" / "Default" / "Cache",
        )
        if path.is_dir()
    )
    sources["缩略图与图标缓存"] = tuple(
        path for path in (local / "Microsoft" / "Windows" / "Explorer",) if path.is_dir()
    )
    return sources


# Never deleted, never even listed. Checked with a case-insensitive prefix match
# because Windows paths are case-insensitive.
PROTECTED_MARKERS: tuple[str, ...] = (
    r"\windows",
    r"\programdata\microsoft\windows\powerpoint",
    r"\program files",
    r"\program files (x86)",
    r"\recovery",
    r"\boot",
    r"\system volume information",
    r"\appdata\local\microsoft\outlook",
    r"\onedrive",
)

# Personal folders: even a "cache" inside these is not ours to decide about.
PROTECTED_USER_DIRS: tuple[str, ...] = (
    "Documents",
    "Desktop",
    "Downloads",
    "Pictures",
    "Videos",
    "Music",
)


@dataclass(frozen=True, slots=True)
class JunkItem:
    """One approvable unit, with the facts a human needs to say yes to it.

    ``kind`` is what makes deletion safe: a ``loose_files`` entry names a *folder*
    but stands for the scattered files inside it, and only the cleaner knows how to
    expand it back into members. Handing the UI bare path strings would let a
    "delete these 1,646 temp files" approval arrive as "rmtree this directory".
    """

    path: str
    size_bytes: int
    category: str
    is_directory: bool
    modified_at: float
    kind: str = KIND_EXACT
    member_count: int = 1
    """How many files this entry stands for: 1 unless it is an aggregate."""

    sample: tuple[str, ...] = ()
    """Up to a few member names, so the row is recognisable without expanding it."""

    scanned_at: float = 0.0
    """When the scan that produced this entry finished; see ``_expand``."""

    @property
    def aggregates_loose_files(self) -> bool:
        return self.kind == KIND_LOOSE_FILES

    @staticmethod
    def from_wire(data: Mapping[str, object]) -> JunkItem | None:
        """Rebuild an item from a JSON object the page echoed back.

        Unknown keys are ignored and anything unrecognised returns ``None`` rather
        than guessing: a malformed approval is refused by the caller, which is the
        safe direction for a delete request.
        """
        if not isinstance(data, dict):
            return None
        path = data.get("path")
        if not isinstance(path, str) or not path:
            return None

        def _text(key: str, default: str = "") -> str:
            value = data.get(key)
            return value if isinstance(value, str) else default

        def _number(key: str, default: float = 0.0) -> float:
            value = data.get(key)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                return default
            return float(value)

        raw_kind = _text("kind", KIND_EXACT)
        kind = raw_kind if raw_kind in (KIND_EXACT, KIND_LOOSE_FILES) else KIND_EXACT
        scanned_at = _number("scanned_at")
        if kind == KIND_LOOSE_FILES and scanned_at <= 0.0:
            # An aggregate without its scan stamp cannot be expanded safely: there
            # would be no way to tell "the files I showed you" from "everything in
            # this folder, including what appeared overnight".
            return None
        raw_sample = data.get("sample")
        sample = (
            [entry for entry in raw_sample if isinstance(entry, str)]
            if isinstance(raw_sample, (list, tuple))
            else []
        )
        return JunkItem(
            path=path,
            size_bytes=int(_number("size_bytes")),
            category=_text("category"),
            is_directory=bool(data.get("is_directory", False)),
            modified_at=_number("modified_at"),
            kind=kind,
            member_count=int(_number("member_count", 1.0) or 1),
            sample=tuple(sample),
            scanned_at=scanned_at,
        )


@dataclass(frozen=True, slots=True)
class JunkGroup:
    category: str
    items: tuple[JunkItem, ...]
    total_bytes: int


@dataclass(frozen=True, slots=True)
class JunkReport:
    scanned_at: float
    groups: tuple[JunkGroup, ...]
    total_bytes: int
    skipped_protected: int
    truncated: bool
    """True when a category hit its item cap; the total is then a floor."""

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class CleanResult:
    deleted: tuple[str, ...]
    freed_bytes: int
    failed: tuple[tuple[str, str], ...]
    log_path: str


class DiskCleaner:
    """Scans known junk locations; deletes only an explicitly confirmed list."""

    def __init__(
        self,
        audit_log: Path,
        *,
        sources: dict[str, tuple[Path, ...]] | None = None,
        protected_dirs: tuple[Path, ...] = (),
        protected_files: tuple[Path, ...] = (),
    ) -> None:
        """Build a cleaner.

        ``protected_dirs`` / ``protected_files`` are for paths only the caller
        knows about — above all JARVIS' own data directory, which lives under
        ``%LOCALAPPDATA%`` and would otherwise be one classification away from
        being swept up as somebody's cache, along with the audit trail that would
        have proved it.
        """
        self._audit_log = audit_log
        self._sources = sources if sources is not None else _known_sources()
        self._last_scan_at: float | None = None
        """Finished time of the last scan, used to refuse stale approvals."""
        self._protected_dirs = self._user_protected_roots() + tuple(protected_dirs)
        self._protected_files = tuple(protected_files)

    def scan(self) -> JunkReport:
        """Walk every known source. Read-only."""
        started_at = time.time()
        groups: list[JunkGroup] = []
        skipped = 0
        truncated = False
        total = 0
        for category, roots in self._sources.items():
            items: list[JunkItem] = []
            for root in roots:
                found, skip_count, hit_cap = self._walk(category, root, started_at)
                items.extend(found)
                skipped += skip_count
                truncated = truncated or hit_cap
            if not items:
                continue
            size = sum(item.size_bytes for item in items)
            total += size
            groups.append(JunkGroup(category=category, items=tuple(items), total_bytes=size))
        self._last_scan_at = started_at
        return JunkReport(
            scanned_at=started_at,
            groups=tuple(groups),
            total_bytes=total,
            skipped_protected=skipped,
            truncated=truncated,
        )

    def delete(self, items: Sequence[JunkItem], *, confirmed: bool) -> CleanResult:
        """Delete exactly what ``items`` names, refusing anything protected.

        The approval unit is a :class:`JunkItem` rather than a path string because
        one of those items may stand for sixteen hundred files a human saw as one
        row. ``confirmed`` must come from a deliberate action in the UI and is a
        required keyword, never a default, so no caller can delete by forgetting it.
        """
        if not confirmed:
            raise DangerousOperationRejectedError(
                "refusing to delete without explicit confirmation",
                details={"requested": len(items)},
            )
        deleted: list[str] = []
        failed: list[tuple[str, str]] = []
        freed = 0
        removed: list[str] = []
        for item in items:
            if item.aggregates_loose_files:
                members, reason = self._expand(item)
                if reason:
                    failed.append((item.path, reason))
                    logger.error("refused aggregate delete for %s: %s", item.path, reason)
                    continue
                gained, gone, why = self._remove_all(members)
                freed += gained
                removed.extend(gone)
                if len(gone) != len(members):
                    failed.append((item.path, "; ".join(why)))
                if gone:
                    deleted.append(item.path)
                continue
            path = Path(item.path)
            if self._is_protected(path):
                # Not a failure to be retried: recorded separately so the audit
                # shows that something was asked for and refused.
                failed.append((item.path, "protected path"))
                logger.error("refused to delete protected path: %s", item.path)
                continue
            size = self._measure(path)
            try:
                if self._is_directory(path):
                    shutil.rmtree(path)
                else:
                    path.unlink()
            except OSError as exc:
                failed.append((item.path, f"{type(exc).__name__}: {exc.strerror or exc}"))
                logger.warning("delete failed for %s: %s", item.path, exc)
                continue
            deleted.append(item.path)
            removed.append(item.path)
            freed += size
        if removed:
            self._append_audit(deleted, removed, freed)
        return CleanResult(
            deleted=tuple(deleted),
            freed_bytes=freed,
            failed=tuple(failed),
            log_path=str(self._audit_log),
        )

    def _remove_all(self, paths: Sequence[str]) -> tuple[int, list[str], list[str]]:
        """Delete a batch of concrete files: (bytes freed, removed, failures)."""
        freed = 0
        gone: list[str] = []
        why: list[str] = []
        for raw in paths:
            path = Path(raw)
            size = self._measure(path)
            try:
                path.unlink()
            except FileNotFoundError:
                continue  # vanished between listing and deleting; nothing to report
            except OSError as exc:
                why.append(f"{path.name}: {type(exc).__name__}")
                logger.warning("delete failed for %s: %s", raw, exc)
                continue
            gone.append(raw)
            freed += size
        return freed, gone, why

    def _expand(self, item: JunkItem) -> tuple[list[str], str]:
        """The members a ``loose_files`` approval actually covers.

        Re-lists the folder instead of trusting the count and keeps only entries
        that are still there, are still just files, are still unprotected, and were
        already present when the human approved the list. Without that last filter a
        scan from this morning could delete a file created at three tonight, which
        nobody approved.
        """
        if item.scanned_at <= 0.0:
            return [], "聚合条目缺少扫描时间戳"
        if self._last_scan_at is None:
            return [], "尚未扫描，无法展开聚合条目"
        if abs(item.scanned_at - self._last_scan_at) > _STALE_SCAN_TOLERANCE:
            return [], "清单来自更早的扫描，请重新扫描"
        root = Path(item.path)
        if self._is_protected(root):
            return [], "protected path"
        try:
            children = list(root.iterdir())
        except OSError as exc:
            return [], f"{type(exc).__name__}: {exc.strerror or exc}"
        members: list[str] = []
        for child in children:
            if self._is_directory(child):
                continue  # the approval covered loose files, never a folder
            if self._is_protected(child):
                logger.error("skipping protected member inside an aggregate: %s", child)
                continue
            try:
                modified = child.stat().st_mtime
            except OSError:
                continue  # gone between the scan and now
            if modified > item.scanned_at:
                continue
            members.append(str(child))
            if len(members) >= _MAX_EXPANDED_MEMBERS:
                return [], f"聚合条目成员数超过上限 {_MAX_EXPANDED_MEMBERS}"
        return members, ""

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    @staticmethod
    def _user_protected_roots() -> tuple[Path, ...]:
        home = Path.home()
        return tuple(home / name for name in PROTECTED_USER_DIRS)

    def _is_protected(self, path: Path) -> bool:
        # Normalise separators first: the markers are written with "\" and both
        # "/" and "\" appear in real Windows paths, so a raw compare would let a
        # forward-slash path slip past the blocklist.
        resolved = str(path).lower().replace("/", "\\")
        if any(marker in resolved for marker in PROTECTED_MARKERS):
            return True
        for root in self._protected_dirs:
            prefix = str(root).lower().replace("/", "\\")
            if resolved == prefix or resolved.startswith(prefix + "\\"):
                return True
        normalised_files = {str(one).lower().replace("/", "\\") for one in self._protected_files}
        return resolved in normalised_files

    def _walk(
        self, category: str, root: Path, scanned_at: float
    ) -> tuple[list[JunkItem], int, bool]:
        """List one level under ``root``; each child is one approvable unit.

        Deliberately not recursive. A list of four thousand individual temp files
        gets a human hitting "select all" without knowing what they approved;
        "Chrome 缓存 — 412 MB" is a decision they can actually make.

        Directories keep one row each. A pile of loose files at the same level
        collapses into a single row naming the folder and counting them, because
        nobody reads sixteen hundred ``tmp8a3f.tmp`` entries either -- but the row
        still deletes only those files, never the folder (see :meth:`_expand`).
        """
        items: list[JunkItem] = []
        skipped = 0
        try:
            children = sorted(root.iterdir(), key=lambda child: child.name.lower())
        except OSError as exc:
            logger.info("cannot list %s: %s", root, exc)
            return items, 0, False
        directories: list[Path] = []
        loose: list[Path] = []
        for child in children:
            if self._is_protected(child):
                skipped += 1
                continue
            (directories if self._is_directory(child) else loose).append(child)
        for child in directories:
            item = self._describe(child, category, scanned_at)
            if item is not None:
                items.append(item)
        if len(loose) > _LOOSE_FILE_MIN:
            aggregate = self._aggregate(category, root, loose, scanned_at)
            if aggregate is not None:
                items.append(aggregate)
        else:
            for child in loose:
                item = self._describe(child, category, scanned_at)
                if item is not None:
                    items.append(item)
        # Biggest first, then capped. Sorting by name put a wall of 0-byte temp
        # files in front of the operator and buried the one row worth deleting;
        # truncating before the sort would then throw the large ones away.
        items.sort(key=lambda item: (-item.size_bytes, item.path.lower()))
        if len(items) > _MAX_ITEMS_PER_CATEGORY:
            return items[:_MAX_ITEMS_PER_CATEGORY], skipped, True
        return items, skipped, False

    @staticmethod
    def _is_directory(path: Path) -> bool:
        return path.is_dir() and not path.is_symlink()

    def _aggregate(
        self, category: str, root: Path, members: list[Path], scanned_at: float
    ) -> JunkItem | None:
        """One row standing for many sibling files, carrying enough to delete them."""
        sample = tuple(path.name for path in members[:5])
        try:
            modified = max(path.stat().st_mtime for path in members)
        except OSError:
            modified = 0.0
        return JunkItem(
            path=str(root),
            size_bytes=sum(self._measure(path) for path in members),
            category=category,
            # Not a directory entry on purpose: approving this must never rmtree root.
            is_directory=False,
            modified_at=modified,
            kind=KIND_LOOSE_FILES,
            member_count=len(members),
            sample=sample,
            scanned_at=scanned_at,
        )

    def _describe(self, path: Path, category: str, scanned_at: float) -> JunkItem | None:
        try:
            stat = path.lstat()
        except OSError:
            return None
        return JunkItem(
            path=str(path),
            size_bytes=self._measure(path),
            category=category,
            is_directory=self._is_directory(path),
            modified_at=stat.st_mtime,
            scanned_at=scanned_at,
        )

    @staticmethod
    def _measure(path: Path) -> int:
        try:
            if path.is_dir() and not path.is_symlink():
                return sum(
                    entry.stat().st_size
                    for entry in path.rglob("*")
                    if entry.is_file() and not entry.is_symlink()
                )
            return path.stat().st_size
        except OSError:
            return 0

    def _append_audit(self, approved: list[str], removed: list[str], freed: int) -> None:
        """One JSON line per action. Failure to log is loud, not silent.

        Both views are kept: ``approved`` is what the operator ticked (often an
        aggregate row naming a folder) and ``removed`` is what actually left the
        disk, member by member. Answering "what did you delete" with just the folder
        name would be answering a different question.
        """
        record = {
            "at": datetime.now(UTC).isoformat(timespec="seconds"),
            "freed_bytes": freed,
            "count": len(removed),
            "approved": approved,
            "paths": removed,
        }
        try:
            self._audit_log.parent.mkdir(parents=True, exist_ok=True)
            with self._audit_log.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError as exc:
            logger.error("could not write deletion audit to %s: %s", self._audit_log, exc)
