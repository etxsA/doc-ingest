"""DOCX / PPTX / XLSX / HTML -> Markdown via Docling (optional ``office`` extra)."""

from __future__ import annotations

from pathlib import Path


def convert_office(path: Path) -> str:
    try:
        from docling.document_converter import DocumentConverter
    except ImportError as e:  # pragma: no cover - depends on installed extras
        raise RuntimeError(
            "Office/HTML inputs need the 'office' extra: uv sync --extra office"
        ) from e
    return DocumentConverter().convert(str(path)).document.export_to_markdown()
