"""Port: read PDF pages (measurements, embedded text, rasterization)."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from PIL.Image import Image

from ..domain.models import PageSignals


class PdfPage(Protocol):
    def signals(self) -> tuple[PageSignals, str]:
        """Measurements for the OCR routing policy, plus the raw embedded text."""
        ...

    def render(self, dpi: int) -> Image: ...


class PdfDocument(Protocol):
    title: str | None

    def __len__(self) -> int: ...

    def page(self, index: int) -> PdfPage: ...

    def close(self) -> None: ...


class PdfReader(Protocol):
    fingerprint: str

    def open(self, path: Path) -> PdfDocument:
        """Raise ``DocumentOpenError`` for corrupt / encrypted / truncated files."""
        ...
