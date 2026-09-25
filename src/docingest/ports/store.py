"""Port: persist normalized documents (content-addressed) and enumerate the corpus."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from ..domain.models import DocumentManifest


@dataclass(frozen=True)
class StoredDocument:
    manifest: DocumentManifest
    location: str  # adapter-specific address (a directory, a URI, a key)
    canonical: bool  # complete default run (True) or a partial / forced-OCR variant


class DocumentStore(Protocol):
    def lookup(
        self, doc_id: str, config_hash: str, *, max_pages: int | None, ocr_all: bool
    ) -> StoredDocument | None:
        """A cached result valid for these run options, if any."""
        ...

    def save(self, manifest: DocumentManifest, markdown: str) -> StoredDocument: ...

    def markdown(self, doc: StoredDocument) -> str: ...

    def corpus(self) -> tuple[list[StoredDocument], list[str]]:
        """One entry per document (canonical, else most complete variant) + warnings."""
        ...
