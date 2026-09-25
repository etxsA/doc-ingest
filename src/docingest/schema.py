"""Canonical intermediate representation for every ingested document.

Whatever the input (born-digital PDF, scanned PDF, image, office file, text),
the pipeline emits the same two artifacts:

* ``document.md``   – normalized Markdown, pages separated by page markers
* ``manifest.json`` – a :class:`DocumentManifest` with full provenance

Downstream consumers (PaperQA2, a vector DB, an agent) only ever see these.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum

from pydantic import BaseModel, Field


class SourceKind(StrEnum):
    PDF = "pdf"
    IMAGE = "image"
    OFFICE = "office"  # docx / pptx / xlsx / html via Docling
    TEXT = "text"  # md / txt passthrough


class PageMethod(StrEnum):
    TEXT_LAYER = "text_layer"  # embedded PDF text, no OCR needed
    VLM_OCR = "vlm_ocr"  # rasterized page -> vision-language model
    DOCLING = "docling"  # office/html converter
    PASSTHROUGH = "passthrough"


class PageProbe(BaseModel):
    """Signals used to decide whether a PDF page needs OCR."""

    n_chars: int = 0
    n_images: int = 0
    image_coverage: float = 0.0  # fraction of page area covered by image objects
    garbage_ratio: float = 0.0  # share of replacement / control / (cid:x) glyphs
    alpha_ratio: float = 0.0  # share of letters among non-space chars
    needs_ocr: bool = False
    reasons: list[str] = Field(default_factory=list)


class PageRecord(BaseModel):
    index: int  # 0-based
    method: PageMethod
    n_chars: int
    seconds: float
    probe: PageProbe | None = None
    model: str | None = None
    model_revision: str | None = None
    gen_tokens: int | None = None
    finish_reason: str | None = None


class ModelRef(BaseModel):
    repo_id: str
    revision: str


class DocumentManifest(BaseModel):
    doc_id: str  # sha256 of the raw bytes (content-addressed)
    source_path: str
    source_name: str
    source_kind: SourceKind
    mime: str
    size_bytes: int
    n_pages: int
    pages: list[PageRecord]
    pipeline_version: str
    config_hash: str  # hash of the effective config -> cache key / reproducibility
    ocr_model: ModelRef | None = None
    title: str | None = None
    created_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds")
    )
    total_seconds: float = 0.0

    @property
    def ocr_pages(self) -> int:
        return sum(p.method == PageMethod.VLM_OCR for p in self.pages)
