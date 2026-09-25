"""Port: discover and fetch documents from a remote source (e.g. arXiv)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from ..domain.models import SourceMetadata


@dataclass(frozen=True)
class SourceRecord:
    key: str  # source-specific id, e.g. "1706.03762v7"
    metadata: SourceMetadata


@dataclass(frozen=True)
class FetchedSource:
    path: Path  # downloaded file (LaTeX archive, .tex, or PDF fallback)
    record: SourceRecord
    format: str  # "latex-archive" | "latex" | "pdf"


class SourceCrawler(Protocol):
    def search(self, query: str, limit: int) -> list[SourceRecord]: ...

    def fetch(self, record: SourceRecord, dest_dir: Path) -> FetchedSource:
        """Download the best available format; raise ``SourceUnavailableError`` if none."""
        ...
