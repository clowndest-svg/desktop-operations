"""Document loading and text extraction.

Two decisions worth explaining, because both are the difference between a
knowledge base that works on a Chinese Windows machine and one that produces
mojibake:

**Encoding is guessed, with a Chinese-first fallback.** ``utf-8`` first, then
``gb18030`` — the superset of GBK/GB2312 that every Simplified-Chinese Windows
editor has produced for twenty years. A ``.txt`` written in Notepad on a
Chinese system is not UTF-8, and refusing it (or silently replacing every byte
with ``?``) would make the feature look broken for exactly the users it is for.

**Binary formats are optional dependencies, lazily imported.** PDF and DOCX need
third-party parsers; a text-only install must not acquire them. The failure
message names the extra to install rather than raising ``ImportError``.
"""

from __future__ import annotations

import hashlib
import importlib
import logging
from pathlib import Path
from typing import Any, Final

from jarvis.core.exceptions import KnowledgeError
from jarvis.knowledge.types import Document

logger = logging.getLogger("jarvis.knowledge.loader")

TEXT_SUFFIXES: Final[frozenset[str]] = frozenset(
    {
        ".txt",
        ".md",
        ".markdown",
        ".rst",
        ".log",
        ".csv",
        ".tsv",
        ".json",
        ".yaml",
        ".yml",
        ".toml",
        ".ini",
        ".cfg",
        ".conf",
        ".py",
        ".js",
        ".ts",
        ".tsx",
        ".jsx",
        ".java",
        ".cs",
        ".go",
        ".rs",
        ".c",
        ".h",
        ".cpp",
        ".hpp",
        ".sql",
        ".sh",
        ".bat",
        ".ps1",
        ".html",
        ".htm",
        ".xml",
    }
)
"""Extensions read as plain text. Deliberately broad — for a personal knowledge
base, "index my notes and my scripts" is the common case, and every one of these
is decodable as text."""

PDF_SUFFIXES: Final[frozenset[str]] = frozenset({".pdf"})
DOCX_SUFFIXES: Final[frozenset[str]] = frozenset({".docx"})

SUPPORTED_SUFFIXES: frozenset[str] = TEXT_SUFFIXES | PDF_SUFFIXES | DOCX_SUFFIXES
"""Everything :func:`load_document` accepts."""

_ENCODINGS: Final[tuple[str, ...]] = ("utf-8", "gb18030")
"""Tried in order. ``gb18030`` is a superset of GBK and GB2312, so one entry
covers every Simplified-Chinese legacy encoding rather than needing a list."""

MAX_TEXT_BYTES: Final[int] = 64 * 1024 * 1024
"""Refuse anything larger than this before reading it into memory."""


def document_id(path: Path) -> str:
    """Stable id for a source path.

    A hash of the *resolved absolute path*, not of the content: re-ingesting an
    edited file must replace the old version rather than accumulate a second
    copy, and a content hash would treat the edit as a new document.
    """
    digest = hashlib.blake2b(str(path).encode("utf-8"), digest_size=10).hexdigest()
    return f"doc-{digest}"


def media_type_for(suffix: str) -> str:
    """MIME-ish tag for a suffix; ``text/plain`` for anything unrecognised."""
    lowered = suffix.lower()
    if lowered in {".md", ".markdown"}:
        return "text/markdown"
    if lowered in {".json"}:
        return "application/json"
    if lowered in {".yaml", ".yml"}:
        return "application/yaml"
    if lowered in {".csv", ".tsv"}:
        return "text/csv"
    if lowered in {".html", ".htm"}:
        return "text/html"
    if lowered in PDF_SUFFIXES:
        return "application/pdf"
    if lowered in DOCX_SUFFIXES:
        return "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    return "text/plain"


def decode_text(raw: bytes, *, source: str) -> str:
    """Decode bytes to text, preferring UTF-8 and falling back to GB18030.

    Raises:
        KnowledgeError: if neither codec accepts the bytes. Latin-1 would accept
            anything and turn a corrupt file into plausible-looking noise, which
            is worse than an honest refusal.
    """
    for encoding in _ENCODINGS:
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise KnowledgeError(
        f"无法解码文本文件（既不是 UTF-8 也不是 GB18030）：{source}",
        details={"source": source, "encodings": list(_ENCODINGS)},
    )


def load_document(path: Path, *, max_file_mb: int) -> Document:
    """Read ``path`` and extract its text.

    Raises:
        KnowledgeError: on a missing file, an unsupported type, an oversized
            file, an undecodable body, or a missing optional parser.
    """
    resolved = path.expanduser()
    if not resolved.is_file():
        raise KnowledgeError(f"文件不存在：{resolved}")
    suffix = resolved.suffix.lower()
    if suffix not in SUPPORTED_SUFFIXES:
        raise KnowledgeError(
            f"不支持的文件类型：{suffix or '(无扩展名)'}",
            details={"path": str(resolved), "supported": sorted(SUPPORTED_SUFFIXES)},
        )
    size = resolved.stat().st_size
    limit = max_file_mb * 1024 * 1024
    if size > limit:
        raise KnowledgeError(
            f"文件太大：{size / 1024 / 1024:.1f} MB，上限 {max_file_mb} MB",
            details={"path": str(resolved), "size_bytes": size},
        )
    if size > MAX_TEXT_BYTES:
        raise KnowledgeError(f"文件超过 {MAX_TEXT_BYTES // 1024 // 1024} MB 的内部上限")

    if suffix in PDF_SUFFIXES:
        text = _read_pdf(resolved)
    elif suffix in DOCX_SUFFIXES:
        text = _read_docx(resolved)
    else:
        try:
            raw = resolved.read_bytes()
        except OSError as exc:
            raise KnowledgeError(f"无法读取文件：{exc}", details={"path": str(resolved)}) from exc
        text = decode_text(raw, source=str(resolved))

    return Document(
        doc_id=document_id(resolved.resolve()),
        source=str(resolved.resolve()),
        title=resolved.name,
        media_type=media_type_for(suffix),
        text=text,
        size_bytes=size,
    )


def _missing_parser(module: str, package: str, suffix: str) -> KnowledgeError:
    return KnowledgeError(
        f"解析 {suffix} 需要 {package}，当前环境没有安装。"
        f"请执行：pip install jarvis-assistant[docs]",
        details={"module": module, "package": package, "suffix": suffix},
    )


def _import_optional(module: str, package: str, suffix: str) -> Any:
    try:
        return importlib.import_module(module)
    except ImportError as exc:
        raise _missing_parser(module, package, suffix) from exc


def _read_pdf(path: Path) -> str:
    """Extract text from a PDF via ``pypdf`` (optional)."""
    pypdf = _import_optional("pypdf", "pypdf", ".pdf")
    try:
        reader = pypdf.PdfReader(str(path))
        pages = [page.extract_text() or "" for page in reader.pages]
    except Exception as exc:
        raise KnowledgeError(f"解析 PDF 失败：{exc}", details={"path": str(path)}) from exc
    return "\n\n".join(page for page in pages if page.strip())


def _read_docx(path: Path) -> str:
    """Extract text from a DOCX via ``python-docx`` (optional)."""
    docx = _import_optional("docx", "python-docx", ".docx")
    try:
        document = docx.Document(str(path))
    except Exception as exc:
        raise KnowledgeError(f"解析 DOCX 失败：{exc}", details={"path": str(path)}) from exc
    blocks = [paragraph.text for paragraph in document.paragraphs if paragraph.text.strip()]
    for table in document.tables:
        for row in table.rows:
            cells = [cell.text.strip() for cell in row.cells if cell.text.strip()]
            if cells:
                blocks.append(" | ".join(cells))
    return "\n".join(blocks)


__all__ = [
    "DOCX_SUFFIXES",
    "MAX_TEXT_BYTES",
    "PDF_SUFFIXES",
    "SUPPORTED_SUFFIXES",
    "TEXT_SUFFIXES",
    "decode_text",
    "document_id",
    "load_document",
    "media_type_for",
]
