# ADR 0002: Per-page OCR routing and a content-addressed cache

**Status:** accepted, 2026-09-24

## Context
Research PDFs are often mixed: born-digital pages next to scanned appendices, or figures that are pages of scanned text. OCR with a vision-language model costs about 10 s per page on a laptop. A text layer costs about 1 ms.

## Decision
- **Route each PDF page on its own** with a pure policy (`domain.routing.decide`). A page goes to OCR when any of these holds:
  - fewer than 50 embedded characters (Marker, olmOCR);
  - images cover at least 60% of the page and it has fewer than 400 characters (Marker);
  - more than 10% of glyphs are broken (Marker `detect_bad_ocr`);
  - fewer than 50% of characters are letters (olmOCR filter).
- Image coverage is measured in page space, including nested Form XObjects and rotated or offset page boxes.
- **Record the reason for every decision** in the manifest.
- **Address outputs by content** (the sha256 of the input). The cache key is the pipeline version plus the fingerprints of the adapters that produce that kind of input.
- Partial runs (`--max-pages`) and forced-OCR runs are stored as variants and never replace a complete result.

## Consequences
- OCR cost is paid only where it adds information.
- Re-running is idempotent and cheap. Model or prompt changes invalidate exactly the affected documents.
