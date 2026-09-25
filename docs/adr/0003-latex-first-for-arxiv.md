# ADR 0003: Prefer LaTeX sources over PDFs for arXiv

**Status:** accepted, 2026-09-24

## Context
Most arXiv papers publish their LaTeX source at `/src/<id>`. The PDF is a lossy rendering of it:
- math becomes glyph soup in the text layer;
- ligatures and hyphenation break words;
- two-column layouts scramble the reading order.

OCR of the rendered page recovers some math, but costs about 10 s per page and still makes mistakes.

## Decision
- The arXiv crawler downloads the **source first** and falls back to the PDF. The preference order is set by `[arxiv].prefer`.
- The `latex` converter port has one implementation today, `pandoc`, which runs in several steps:
  1. Safely extract the archive (tar with `filter='data'` and a size cap).
  2. Detect the main file (`00README.json`, then `\documentclass` together with `\begin{document}`, then name heuristics).
  3. Flatten `\input` and `\include` in Python, with cycle protection, encoding fallback and comment stripping. pandoc hangs forever on a file that `\input`s itself.
  4. Run pandoc 3.9 with a wall-clock timeout, using a Markdown profile that keeps `$…$` math, tables and `[@cite]` keys.
  5. Split the result into one segment per section.
- If pandoc fails, a pylatexenc plain-text fallback runs with the math kept verbatim, so ingestion never stops at a bad source.
- Bibliographic metadata comes from the arXiv API (CC0). The license comes from OAI-PMH (`oaipmh.arxiv.org`, moved in 2025). Both are stored in the manifest and used for PaperQA2 citations.
- The crawler follows the arXiv terms of use: one request every 3 s, a single connection, and a User-Agent with an optional contact address. Full texts are stored for personal and research use only, and each one's license is recorded because e-prints may not be redistributed without permission.
