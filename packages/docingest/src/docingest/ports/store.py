"""Port: persist normalized documents (content-addressed) and enumerate the corpus."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from ..domain.models import DocumentManifest


@dataclass(frozen=True)
class StoredDocument:
    manifest: DocumentManifest
    location: str  # adapter-specific address (a directory, a URI, a key)
    canonical: bool  # complete default run (True) or a partial / forced-OCR / degraded variant


@runtime_checkable
class DocumentStore(Protocol):
    def lookup(
        self, doc_id: str, config_hash: str, *, max_pages: int | None, ocr_all: bool
    ) -> StoredDocument | None:
        """A cached result valid for these run options, if any. Never a degraded one."""
        ...

    def save(
        self, manifest: DocumentManifest, markdown: str, *, degraded: bool = False
    ) -> StoredDocument:
        """Persist a result; saving the same run again replaces it in place.

        ``degraded``: produced by an environment-caused fallback (a converter timeout or
        crash), not by the configured pipeline. It is kept so the corpus is not missing
        the document, but it is never canonical, never replaces a stored result and is
        never returned by :meth:`lookup`, so the next run retries the real conversion.
        """
        ...

    def markdown(self, doc: StoredDocument) -> str: ...

    def corpus(self) -> tuple[list[StoredDocument], list[str]]:
        """One entry per document (canonical, else most complete variant) + warnings."""
        ...
