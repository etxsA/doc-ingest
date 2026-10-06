"""Tiny, dependency-free document builders for tests."""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

LONG = "Attention is all you need and the transformer architecture works well. " * 3


def pdf(
    path: Path,
    content: bytes,
    resources: bytes = b"",
    extra: list[bytes] | tuple[bytes, ...] = (),
    page_box: bytes = b"/MediaBox [0 0 612 792]",
) -> Path:
    """Minimal PDF writer: one page, Helvetica as /F1, optional extra objects (6+)."""
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R " + page_box + b" "
        b"/Resources << /Font << /F1 4 0 R >> " + resources + b" >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream",
        *extra,
    ]
    out, offsets = bytearray(b"%PDF-1.4\n"), []
    for i, body in enumerate(objs, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    out += b"".join(b"%010d 00000 n \n" % o for o in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref)
    path.write_bytes(bytes(out))
    return path


def text_pdf(path: Path, *lines: str) -> Path:
    ops = b"BT /F1 11 Tf 72 700 Td 14 TL " + b" ".join(b"(%s) '" % line.encode() for line in lines)
    return pdf(path, ops + b" ET")


def image_pdf(path: Path) -> Path:
    img = Image.new("RGB", (1275, 1650), "white")
    ImageDraw.Draw(img).text((100, 100), "Scanned words live only in pixels", fill="black")
    img.save(path, "PDF", resolution=150)
    return path


def blank_pdf(path: Path, pages: int) -> Path:
    import pypdfium2 as pdfium

    doc = pdfium.PdfDocument.new()
    for _ in range(pages):
        doc.new_page(612, 792)
    doc.save(path)
    return path
