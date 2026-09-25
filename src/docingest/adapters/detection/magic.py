"""TypeDetector adapter: magic bytes first (extensions lie), suffix as fallback."""

from __future__ import annotations

import gzip
import re
from pathlib import Path

from ...domain.errors import UnsupportedInputError
from ...domain.models import SourceKind

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".webp", ".bmp", ".gif"}
OFFICE_SUFFIXES = {".docx", ".pptx", ".xlsx", ".html", ".htm", ".xhtml"}
TEXT_SUFFIXES = {".md", ".markdown", ".txt"}
LATEX_SUFFIXES = {".tex", ".ltx"}

# Unambiguous signatures. BMP ("BM") is too weak on its own and is checked separately.
_MAGIC: list[tuple[bytes, SourceKind, str]] = [
    (b"\x89PNG\r\n\x1a\n", SourceKind.IMAGE, "image/png"),
    (b"\xff\xd8\xff", SourceKind.IMAGE, "image/jpeg"),
    (b"II*\x00", SourceKind.IMAGE, "image/tiff"),
    (b"MM\x00*", SourceKind.IMAGE, "image/tiff"),
    (b"GIF8", SourceKind.IMAGE, "image/gif"),
]
_BMP_DIB_SIZES = {12, 40, 52, 56, 64, 108, 124}
_PDF_HEADER = re.compile(rb"%PDF-\d\.\d")
_TEX_HINT = re.compile(rb"\\(documentclass|documentstyle|begin\{document\}|section|input\{)")

_OFFICE_MIME = {
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".html": "text/html",
    ".htm": "text/html",
    ".xhtml": "application/xhtml+xml",
}


def _is_tar(block: bytes) -> bool:
    return len(block) > 262 and block[257:262] == b"ustar"


class MagicBytesDetector:
    def detect(self, path: Path) -> tuple[SourceKind, str]:
        with path.open("rb") as f:
            head = f.read(1024)
        suffix = path.suffix.lower()
        if head.startswith(b"%PDF-"):
            return SourceKind.PDF, "application/pdf"
        for magic, kind, mime in _MAGIC:
            if head.startswith(magic):
                return kind, mime
        if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
            return SourceKind.IMAGE, "image/webp"
        if (
            head[:2] == b"BM"
            and len(head) >= 18
            and head[6:10] == b"\0\0\0\0"
            and int.from_bytes(head[14:18], "little") in _BMP_DIB_SIZES
        ):
            return SourceKind.IMAGE, "image/bmp"
        if head[:2] == b"\x1f\x8b":  # arXiv e-prints: gzipped tar, or a single gzipped .tex
            return self._gzip(path)
        if _is_tar(head):
            return SourceKind.LATEX, "application/x-tar"
        # The PDF spec tolerates junk before the header (pdfium too), but text that merely
        # *mentions* "%PDF-1.7" must not become a PDF: only for .pdf / unknown suffixes.
        known = OFFICE_SUFFIXES | TEXT_SUFFIXES | IMAGE_SUFFIXES | LATEX_SUFFIXES
        if suffix not in known and _PDF_HEADER.search(head):
            return SourceKind.PDF, "application/pdf"
        if suffix in LATEX_SUFFIXES:
            return SourceKind.LATEX, "application/x-tex"
        if suffix in OFFICE_SUFFIXES:
            return SourceKind.OFFICE, _OFFICE_MIME[suffix]
        if suffix in TEXT_SUFFIXES:
            return SourceKind.TEXT, "text/markdown" if suffix != ".txt" else "text/plain"
        raise UnsupportedInputError(f"Unsupported input type: {path.name}")

    @staticmethod
    def _gzip(path: Path) -> tuple[SourceKind, str]:
        try:
            with gzip.open(path, "rb") as f:
                block = f.read(4096)
        except OSError as e:
            raise UnsupportedInputError(f"corrupt gzip file {path.name}: {e}") from e
        if _is_tar(block):
            return SourceKind.LATEX, "application/gzip"  # tar.gz source archive
        if block.startswith(b"%PDF-"):
            raise UnsupportedInputError(f"{path.name} is a gzipped PDF; gunzip it first")
        if _TEX_HINT.search(block):
            return SourceKind.LATEX, "application/gzip"  # single gzipped .tex
        raise UnsupportedInputError(f"gzip file {path.name} is neither a tar nor a TeX source")
