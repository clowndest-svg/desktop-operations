"""Splitting a document into retrievable chunks.

The chunking strategy is "pack sentences up to the size limit, overlap the
seam". Two things it deliberately does *not* do:

* It does not cut on a fixed character grid. A 600-character window lands in the
  middle of a sentence about half the time, and a chunk that begins mid-clause
  embeds poorly and reads worse when it is shown as a citation.
* It does not overlap whole chunks. Overlapping entire chunks doubles the index
  and makes a top-5 result list contain the same passage twice. Overlapping a
  fixed number of trailing *characters* keeps the seam recoverable without the
  duplication.

Sentence boundaries are recognised for Chinese (。！？；) and for Latin
(``.!?;``) punctuation. Chinese text has no spaces, so the Latin rule alone
would treat a whole paragraph as one sentence.
"""

from __future__ import annotations

import logging
import re
from typing import Final

from jarvis.knowledge.types import Chunk, ChunkingOptions

logger = logging.getLogger("jarvis.knowledge.chunker")

_SENTENCE_END: Final[re.Pattern[str]] = re.compile(r"(?<=[。！？；!?;])\s*|(?<=\.)\s+|\n{2,}")
"""Split points, in three flavours.

* After a CJK / full-width mark or a Latin exclamation mark, question mark or
  semicolon — no whitespace needed, because Chinese text has none.
* After a Latin **period followed by whitespace**. The lookahead is what keeps
  ``3.14`` and ``example.com`` intact; splitting on every dot would shred
  numbers, file names and version strings into pieces that embed badly.
* On a blank line, which is the author telling us where the thought ends.

The lookbehind keeps the punctuation with the sentence it ends — dropping it
would change the meaning of a quoted question.
"""


def split_units(text: str) -> list[str]:
    """Break ``text`` into sentence-ish units, preserving order."""
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    units = [unit.strip() for unit in _SENTENCE_END.split(normalized)]
    return [unit for unit in units if unit]


def _hard_split(unit: str, size: int) -> list[str]:
    """Cut a single oversized unit into ``size``-character pieces.

    Needed because one line of minified JSON or a base64 blob is a single
    "sentence" thousands of characters long; without this the packer would emit
    a chunk far past the configured size and blow the embedding window.
    """
    return [unit[index : index + size] for index in range(0, len(unit), size)]


def chunk_text(text: str, options: ChunkingOptions) -> list[Chunk]:
    """Split ``text`` into overlapping chunks.

    Args:
        text: The document's extracted text.
        options: Size, overlap and the metadata carried onto each chunk.

    Returns:
        Chunks in document order. Empty when ``text`` has no non-whitespace
        content — an empty document should produce no chunks rather than one
        empty chunk that matches everything.

    Raises:
        ValueError: if ``overlap`` is not smaller than ``size``. Accepting it
            would make the packer never advance and loop forever.
    """
    size = options.size
    overlap = options.overlap
    if size <= 0:
        raise ValueError("chunk size 必须为正数")
    if overlap >= size:
        raise ValueError("chunk overlap 必须小于 chunk size")

    units: list[str] = []
    for unit in split_units(text):
        if len(unit) > size:
            units.extend(_hard_split(unit, size))
        else:
            units.append(unit)
    if not units:
        return []

    pieces: list[str] = []
    current = ""
    for unit in units:
        if current and len(current) + len(unit) + 1 > size:
            pieces.append(current)
            tail = current[-overlap:] if overlap else ""
            current = f"{tail}\n{unit}" if tail else unit
        elif current:
            current = f"{current}\n{unit}"
        else:
            current = unit
    if current.strip():
        pieces.append(current)

    chunks: list[Chunk] = []
    cursor = 0
    for ordinal, piece in enumerate(pieces):
        stripped = piece.strip()
        if not stripped:
            continue
        # Offsets are approximate by design: overlap means the piece may start
        # before where the previous one ended, so a search for the exact
        # substring can fail on a seam. They are clamped to the text's bounds
        # so they are always usable as slice indices — a citation highlighter
        # that can raise IndexError is worse than one that highlights a little
        # too much.
        start = text.find(stripped[: min(40, len(stripped))], cursor)
        if start < 0:
            start = min(cursor, len(text))
        cursor = min(start + len(stripped), len(text))
        chunks.append(
            Chunk(
                chunk_id=f"{options.metadata.get('doc_id', 'doc')}#{ordinal}",
                doc_id=str(options.metadata.get("doc_id", "")),
                ordinal=ordinal,
                text=stripped,
                char_start=start,
                char_end=cursor,
            )
        )
    return chunks


__all__ = ["chunk_text", "split_units"]
