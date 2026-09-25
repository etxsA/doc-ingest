"""DocumentConverter adapter for DOCX / PPTX / XLSX / HTML via Docling (``office`` extra)."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from ...domain.errors import ConversionError
from ...domain.models import PageMethod
from ...ports import Conversion, Segment


def _docling_version() -> str:
    try:
        return version("docling")
    except PackageNotFoundError:
        return "missing"


class DoclingConverter:
    fingerprint = f"docling {_docling_version()}"

    def convert(self, path: Path) -> Conversion:
        try:
            from docling.document_converter import DocumentConverter
        except ImportError as e:  # pragma: no cover - depends on installed extras
            raise ConversionError(
                "Office/HTML inputs need the 'office' extra: uv sync --extra office"
            ) from e
        try:
            md = DocumentConverter().convert(str(path)).document.export_to_markdown()
        except Exception as e:
            raise ConversionError(f"docling failed on {path.name}: {e}") from e
        return Conversion(segments=[Segment(md)], method=PageMethod.DOCLING, engine=self.fingerprint)
