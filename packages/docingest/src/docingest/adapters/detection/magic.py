"""TypeDetector adapter: magic bytes first (extensions lie), suffix as fallback."""

from __future__ import annotations

import codecs
import gzip
import re
import tarfile
import zlib
from pathlib import Path

from ...domain.errors import UnsupportedInputError
from ...domain.models import SourceKind

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".webp", ".bmp", ".gif"}
OFFICE_SUFFIXES = {".docx", ".pptx", ".xlsx", ".html", ".htm", ".xhtml"}
TEXT_SUFFIXES = {".md", ".markdown", ".txt"}
LATEX_SUFFIXES = {".tex", ".ltx"}
# Text formats in which a leading "%PDF-..." is ordinary content (a TeX comment line).
TEXTLIKE_SUFFIXES = TEXT_SUFFIXES | LATEX_SUFFIXES

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

_OFFICE_MIME = {
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".html": "text/html",
    ".htm": "text/html",
    ".xhtml": "application/xhtml+xml",
}


def _is_tar(block: bytes) -> bool:
    """A valid first tar header: ustar or a pre-POSIX (v7) tar without the magic.

    Checks the header's checksum, as ``tarfile`` does (the arXiv crawler classifies
    with ``tarfile``), but on this one block only: ``tarfile.open`` would read a
    pax / GNU long-name member whole into memory before any size limit applies.
    """
    try:
        tarfile.TarInfo.frombuf(block[: tarfile.BLOCKSIZE], tarfile.ENCODING, "surrogateescape")
    except tarfile.HeaderError:
        return False
    return True


def _is_text(head: bytes) -> bool:
    """UTF-8 without NULs; a multi-byte character cut at the end of ``head`` is fine."""
    try:
        codecs.getincrementaldecoder("utf-8")().decode(head)
    except UnicodeDecodeError:
        return False
    return b"\0" not in head


def _looks_html(head: bytes) -> bool:
    return head.lstrip()[:15].lower().startswith((b"<!doctype html", b"<html"))


class MagicBytesDetector:
    def detect(self, path: Path) -> tuple[SourceKind, str]:  # noqa: PLR0911
        with path.open("rb") as f:
            head = f.read(1024)
        suffix = path.suffix.lower()
        # A .tex / .txt / .md may begin with a "%PDF-A compliant ..." comment line; it is
        # a PDF only if the bytes are not text (a real PDF's second line is binary).
        if head.startswith(b"%PDF-") and (suffix not in TEXTLIKE_SUFFIXES or not _is_text(head)):
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
        """Same rule as the arXiv crawler's ``classify``, so whatever it saved as a
        ``.tex.gz`` also ingests: a tar is a source archive, a PDF / PostScript / HTML
        payload is not a source, anything else is a single gzipped .tex. TeX has no
        reliable signature: comment headers run for kilobytes, and plain-TeX e-prints
        (``\\input harvmac``) carry no ``\\documentclass`` at all.
        """
        try:
            with gzip.open(path, "rb") as f:
                inner = f.read(1024)
        except (OSError, EOFError, zlib.error) as e:  # truncated streams raise EOFError
            raise UnsupportedInputError(f"corrupt gzip file {path.name}: {e}") from e
        if _is_tar(inner):
            return SourceKind.LATEX, "application/gzip"  # tar.gz source archive
        if inner.startswith(b"%PDF-"):
            raise UnsupportedInputError(f"{path.name} is a gzipped PDF; gunzip it first")
        if inner.startswith(b"%!PS") or _looks_html(inner):
            raise UnsupportedInputError(f"gzip file {path.name} holds PostScript / HTML, not TeX")
        return SourceKind.LATEX, "application/gzip"  # single gzipped .tex
