"""Page-aware chunking: the chunks ``docingest ask`` gives to PaperQA2, as a pure function.

PaperQA2's ``chunk_pdf`` is the reference (the PaperQA2 adapter still calls it and a test
checks that both give the same chunks; the one difference is a document without pages,
where ``chunk_pdf`` raises and this returns no chunks). It is repeated here because the
application layer may not import ``paperqa``, and the chunk index must cut papers exactly
like ``ask`` does.

``CHUNK_CHARS`` and ``OVERLAP`` are the defaults of ``[qa] chunk_chars`` and ``overlap``.
Callers pass the configured values to ``chunk_pages``, which has no defaults of its own,
so the chunks of an index and of ``ask`` cannot differ by a forgotten setting.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

CHUNKER_VERSION = 1  # bump when the algorithm changes: an index built with another one is stale
CHUNK_CHARS = 900
OVERLAP = 100
PAGE_BREAK = "\n\n"  # keeps the last word of a page from fusing with the first of the next
NAME_LENGTH = 16  # a chunk is cited as "<first 16 characters of doc_id> pages a-b"


@dataclass(frozen=True)
class Chunk:
    doc_id: str
    name: str  # as cited: "<doc_id[:16]> pages a-b" (always a range, "pages 3-3" for one page)
    text: str
    first_page: int  # 1-based, as in the page markers of document.md
    last_page: int
    is_reference: bool = False  # set by the caller from the section titles, not by the chunker
    start: int = 0  # offset of text in the page texts joined by PAGE_BREAK (end: start + len(text))


def chunk_pages(
    doc_id: str,
    pages: Mapping[int, str],
    *,
    chunk_chars: int,
    overlap: int,
) -> list[Chunk]:
    """Cut ``{page number: page text}`` (see ``text.split_pages``) into overlapping chunks.

    A chunk is cut every ``chunk_chars`` characters of the pages joined by ``PAGE_BREAK``;
    the next one starts ``overlap`` characters before the cut. Each chunk records the first
    and last page it touches and where it starts (``start``) in the joined text, which
    identifies it inside its paper. No pages, no chunks.
    """
    if not 0 <= overlap < chunk_chars:
        raise ValueError(f"need 0 <= overlap < chunk_chars, got {overlap} and {chunk_chars}")
    chunks: list[Chunk] = []
    buffer = ""  # the joined text from offset `base` on
    base = 0
    touched: list[int] = []

    def emit() -> None:
        first, last = touched[0], touched[-1]
        name = f"{doc_id[:NAME_LENGTH]} pages {first}-{last}"
        chunks.append(Chunk(doc_id, name, buffer[:chunk_chars], first, last, start=base))

    for number, text in pages.items():
        buffer += text + PAGE_BREAK
        touched.append(number)
        while len(buffer) > chunk_chars:
            emit()
            buffer = buffer[chunk_chars - overlap :]
            base += chunk_chars - overlap
            touched = [number]
    if touched:  # what is left after the last cut is always longer than the overlap
        emit()
    return chunks
