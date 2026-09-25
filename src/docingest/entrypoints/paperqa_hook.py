"""Driving adapter: PaperQA2's ``settings.parsing.parse_pdf`` hook backed by docingest.

    from docingest.entrypoints.paperqa_hook import parse_pdf_to_pages
    settings.parsing.parse_pdf = parse_pdf_to_pages

PaperQA2's own ``Docs.aadd("paper.pdf")`` then routes every page through the
text-layer / OCR router, with the content-addressed cache.
"""

from __future__ import annotations

import os
from pathlib import Path

from ..application.ingest import IngestService
from ..bootstrap import Container
from ..config import load_config
from ..domain.errors import DocumentOpenError
from ..domain.text import split_pages

_service: IngestService | None = None


def _ingest() -> IngestService:
    global _service
    if _service is None:  # keep the OCR model loaded across calls
        _service = Container(load_config(), log=lambda _: None).ingest
    return _service


def parse_pdf_to_pages(
    path: str | os.PathLike,
    page_size_limit: int | None = None,
    page_range: int | tuple[int, int] | None = None,
    **kwargs,
):
    from paperqa.types import ParsedMetadata, ParsedText
    from paperqa.utils import ImpossibleParsingError

    service = _ingest()
    try:
        stored = service.ingest(Path(path))
    except DocumentOpenError as exc:  # corrupt / encrypted: PaperQA skips, doesn't retry
        raise ImpossibleParsingError(f"docingest could not open PDF {path}: {exc}") from exc
    manifest = stored.manifest
    pages = split_pages(service.store.markdown(stored))
    content = {}
    for rec in manifest.pages:
        n = rec.index + 1
        if isinstance(page_range, int) and n != page_range:
            continue
        if isinstance(page_range, tuple) and not page_range[0] <= n <= page_range[1]:
            continue
        text = pages.get(n, "")
        if page_size_limit and len(text) > page_size_limit:
            raise ImpossibleParsingError(  # same contract as PaperQA2's own readers
                f"The text in page {n} ({rec.method.value}) of {manifest.n_pages} was"
                f" {len(text)} chars long, which exceeds the {page_size_limit} char limit"
                f" for the PDF at path {path}."
            )
        content[str(n)] = text + "\n"  # page break survives PaperQA's concatenation
    methods = sorted({r.method.value for r in manifest.pages})
    return ParsedText(
        content=content,
        metadata=ParsedMetadata(
            parsing_libraries=[f"docingest ({', '.join(methods)})"],
            total_parsed_text_length=sum(len(t) for t in content.values()),
        ),
    )
