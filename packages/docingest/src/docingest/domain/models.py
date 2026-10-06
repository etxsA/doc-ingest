"""Canonical intermediate representation for every ingested document.

Whatever the input (born-digital PDF, scanned PDF, image, office file, LaTeX source,
text), the pipeline emits the same two artifacts:

* ``document.md``   - normalized Markdown, segments separated by page markers
* ``manifest.json`` - a :class:`DocumentManifest` with full provenance

Downstream consumers (PaperQA2, a vector DB, an agent) only ever see these.
Pure domain: no I/O, no third-party imports besides pydantic.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, Field


class SourceKind(StrEnum):
    PDF = "pdf"
    IMAGE = "image"
    OFFICE = "office"  # docx / pptx / xlsx / html
    LATEX = "latex"  # .tex file or an arXiv source archive (.tar.gz / .gz)
    TEXT = "text"  # md / txt passthrough


class PageMethod(StrEnum):
    TEXT_LAYER = "text_layer"  # embedded PDF text, no OCR needed
    VLM_OCR = "vlm_ocr"  # rasterized page -> vision-language model
    DOCLING = "docling"  # office/html converter
    LATEX = "latex"  # LaTeX source -> Markdown (one segment per section)
    LATEX_PLAINTEXT = "latex_plaintext"  # fallback when the LaTeX converter fails
    PASSTHROUGH = "passthrough"


class PageSignals(BaseModel):
    """Raw measurements of a PDF page, produced by a PDF adapter."""

    n_chars: int = 0
    n_images: int = 0
    image_coverage: float = 0.0  # fraction of page area covered by image objects
    garbage_ratio: float = 0.0  # share of replacement / control / (cid:x) glyphs
    alpha_ratio: float = 0.0  # share of letters among non-space chars


class PageProbe(PageSignals):
    """Signals plus the routing decision taken on them."""

    needs_ocr: bool = False
    reasons: list[str] = Field(default_factory=list)


class ModelRef(BaseModel):
    repo_id: str
    revision: str


class SourceMetadata(BaseModel):
    """Bibliographic metadata, e.g. from the arXiv API. Everything optional."""

    title: str | None = None
    authors: list[str] = Field(default_factory=list)
    year: int | None = None
    abstract: str | None = None
    doi: str | None = None
    arxiv_id: str | None = None  # without version, e.g. "1706.03762"
    version: str | None = None  # e.g. "v7"
    url: str | None = None
    license: str | None = None
    categories: list[str] = Field(default_factory=list)

    def citation(self, fallback: str) -> str:
        """Short human-readable citation for QA answers."""
        who = self.authors[0].split()[-1] if self.authors else None
        if who and len(self.authors) > 1:
            who += " et al."
        parts = [p for p in (who, f"({self.year})" if self.year else None) if p]
        head = " ".join(parts)
        title = self.title or fallback
        ref = f"arXiv:{self.arxiv_id}{self.version or ''}" if self.arxiv_id else None
        return ". ".join(p for p in (head, title, ref) if p)


class PageRecord(BaseModel):
    index: int  # 0-based
    method: PageMethod
    n_chars: int
    seconds: float
    engine: str | None = None  # adapter that produced the text, e.g. "pdfium", "pandoc 3.8"
    title: str | None = None  # section title for LaTeX / office segments
    probe: PageProbe | None = None
    model: str | None = None
    model_revision: str | None = None
    gen_tokens: int | None = None
    finish_reason: str | None = None


class DocumentManifest(BaseModel):
    doc_id: str  # sha256 of the raw bytes (content-addressed)
    source_path: str
    source_name: str
    source_kind: SourceKind
    mime: str
    size_bytes: int
    n_pages: int  # segments processed in this run
    source_pages: int  # segments in the source; n_pages < source_pages -> partial run
    max_pages: int | None = None  # run options, recorded for provenance
    ocr_all: bool = False
    pages: list[PageRecord]
    pipeline_version: str
    config_hash: str  # hash of pipeline version + policy + adapter fingerprints
    ocr_model: ModelRef | None = None
    title: str | None = None
    metadata: SourceMetadata | None = None
    created_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat(timespec="seconds"))
    total_seconds: float = 0.0

    @property
    def ocr_pages(self) -> int:
        return sum(p.method == PageMethod.VLM_OCR for p in self.pages)

    @property
    def complete(self) -> bool:
        return self.n_pages == self.source_pages

    def citation(self) -> str:
        if self.metadata:
            return self.metadata.citation(self.title or self.source_name)
        return f"{self.title} ({self.source_name})" if self.title else self.source_name
