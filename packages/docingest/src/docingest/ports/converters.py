"""Port: convert a whole document (office, LaTeX, text) into Markdown segments."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, runtime_checkable

from ..domain.models import PageMethod, SourceMetadata


@dataclass(frozen=True)
class Segment:
    """One logical unit of a converted document (a section, a slide, the whole file)."""

    text: str
    title: str | None = None


@dataclass(frozen=True)
class Conversion:
    segments: list[Segment]
    method: PageMethod
    engine: str  # e.g. "pandoc 3.8", "docling 2.130"
    title: str | None = None
    metadata: SourceMetadata | None = None
    warnings: list[str] = field(default_factory=list)
    # True when a fallback ran because of the environment (timeout, crash, missing tool),
    # not because the input is unconvertible: the result must not be cached as canonical.
    degraded: bool = False


@runtime_checkable
class DocumentConverter(Protocol):
    fingerprint: str

    def convert(self, path: Path) -> Conversion:
        """Raise ``ConversionError`` when the document cannot be converted."""
        ...
