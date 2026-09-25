"""Input-type detection and per-page "does this need OCR?" probing."""

from __future__ import annotations

import re
from pathlib import Path

import pypdfium2 as pdfium
import pypdfium2.raw as pdfium_c

from .config import ProbeConfig
from .schema import PageProbe, SourceKind

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".webp", ".bmp", ".gif"}
OFFICE_SUFFIXES = {".docx", ".pptx", ".xlsx", ".html", ".htm", ".xhtml"}
TEXT_SUFFIXES = {".md", ".markdown", ".txt"}

_MAGIC: list[tuple[bytes, SourceKind, str]] = [
    (b"%PDF-", SourceKind.PDF, "application/pdf"),
    (b"\x89PNG\r\n\x1a\n", SourceKind.IMAGE, "image/png"),
    (b"\xff\xd8\xff", SourceKind.IMAGE, "image/jpeg"),
    (b"II*\x00", SourceKind.IMAGE, "image/tiff"),
    (b"MM\x00*", SourceKind.IMAGE, "image/tiff"),
    (b"GIF8", SourceKind.IMAGE, "image/gif"),
    (b"BM", SourceKind.IMAGE, "image/bmp"),
]

_OFFICE_MIME = {
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".html": "text/html",
    ".htm": "text/html",
    ".xhtml": "application/xhtml+xml",
}


def detect_kind(path: Path) -> tuple[SourceKind, str]:
    """Sniff magic bytes first (extensions lie), fall back to the suffix."""
    head = path.read_bytes()[:16]
    for magic, kind, mime in _MAGIC:
        if head.startswith(magic):
            return kind, mime
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return SourceKind.IMAGE, "image/webp"
    suffix = path.suffix.lower()
    if suffix in OFFICE_SUFFIXES:
        return SourceKind.OFFICE, _OFFICE_MIME[suffix]
    if suffix in TEXT_SUFFIXES:
        return SourceKind.TEXT, "text/markdown" if suffix != ".txt" else "text/plain"
    raise ValueError(f"Unsupported input type: {path.name}")


_CID = re.compile(r"\(cid:\d+\)")


def garbage_ratio(text: str) -> float:
    """Share of characters that indicate a broken text layer."""
    stripped = "".join(text.split())
    if not stripped:
        return 0.0
    bad = stripped.count("�") + sum(
        1 for c in stripped if ord(c) < 32 or 0xE000 <= ord(c) <= 0xF8FF  # control / private-use
    )
    bad += sum(len(m) for m in _CID.findall(text))
    return bad / len(stripped)


def alpha_ratio(text: str) -> float:
    """Share of letters among non-space chars (olmOCR flags < 0.5 as bad text)."""
    stripped = "".join(text.split())
    return sum(c.isalpha() for c in stripped) / len(stripped) if stripped else 0.0


def probe_pdf_page(page: pdfium.PdfPage, cfg: ProbeConfig) -> tuple[PageProbe, str]:
    """Return OCR decision signals plus the page's embedded text."""
    textpage = page.get_textpage()
    text = textpage.get_text_bounded()
    n_chars = len("".join(text.split()))

    width, height = page.get_size()
    page_area = max(width * height, 1.0)
    image_area = 0.0
    n_images = 0
    for obj in page.get_objects(filter=[pdfium_c.FPDF_PAGEOBJ_IMAGE], max_depth=2):
        left, bottom, right, top = obj.get_bounds()
        image_area += max(right - left, 0) * max(top - bottom, 0)
        n_images += 1
    coverage = min(image_area / page_area, 1.0)
    garbage = garbage_ratio(text)
    alpha = alpha_ratio(text)

    reasons: list[str] = []
    if n_chars < cfg.min_chars:
        reasons.append(f"only {n_chars} embedded chars (<{cfg.min_chars})")
    if coverage >= cfg.image_coverage and n_chars < cfg.image_coverage_max_chars:
        reasons.append(f"image covers {coverage:.0%} of page with little text")
    if garbage > cfg.max_garbage_ratio:
        reasons.append(f"garbled text layer ({garbage:.0%} bad glyphs)")
    if n_chars >= cfg.min_chars and alpha < cfg.min_alpha_ratio:
        reasons.append(f"low alphabetic ratio ({alpha:.0%})")

    probe = PageProbe(
        n_chars=n_chars,
        n_images=n_images,
        image_coverage=round(coverage, 3),
        garbage_ratio=round(garbage, 3),
        alpha_ratio=round(alpha, 3),
        needs_ocr=bool(reasons),
        reasons=reasons,
    )
    return probe, text
