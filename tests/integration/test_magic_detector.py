import gzip
import io
import tarfile

import pytest
from builders import LONG, text_pdf
from PIL import Image
from PIL.PngImagePlugin import PngInfo

from docingest.adapters.detection.magic import MagicBytesDetector
from docingest.domain.errors import UnsupportedInputError
from docingest.domain.models import SourceKind

detect = MagicBytesDetector().detect


def test_magic_bytes_beat_extensions(tmp_path):
    fake = tmp_path / "actually_png.pdf"
    Image.new("RGB", (10, 10)).save(fake, "PNG")
    assert detect(fake) == (SourceKind.IMAGE, "image/png")
    md = tmp_path / "notes.md"
    md.write_text("# hi")
    assert detect(md)[0] == SourceKind.TEXT
    (tmp_path / "x.xyz").write_bytes(b"\x00\x01")
    with pytest.raises(UnsupportedInputError):
        detect(tmp_path / "x.xyz")


def test_text_starting_with_bm_is_not_a_bitmap(tmp_path):
    md = tmp_path / "BMW_notes.md"
    md.write_text("BMW engine notes: the block is aluminium.")
    assert detect(md)[0] == SourceKind.TEXT
    bmp = tmp_path / "real.bmp"
    Image.new("RGB", (4, 4)).save(bmp, "BMP")
    assert detect(bmp) == (SourceKind.IMAGE, "image/bmp")


def test_pdf_header_after_leading_junk(tmp_path):
    src = text_pdf(tmp_path / "a.pdf", LONG)
    junk = tmp_path / "junk.bin"
    junk.write_bytes(b"\x00garbage-before-header\n" + src.read_bytes())
    assert detect(junk)[0] == SourceKind.PDF


def test_text_mentioning_pdf_header_is_not_a_pdf(tmp_path):
    md = tmp_path / "notes.md"
    md.write_text("Every PDF file starts with the header `%PDF-1.7`.")
    assert detect(md)[0] == SourceKind.TEXT
    html = tmp_path / "page.html"
    html.write_text("<p>The magic bytes are %PDF-1.4</p>")
    assert detect(html)[0] == SourceKind.OFFICE
    info = PngInfo()
    info.add_text("Comment", "converted from %PDF-1.4 source")
    png = tmp_path / "scan.png"
    Image.new("RGB", (8, 8)).save(png, pnginfo=info)
    assert detect(png)[0] == SourceKind.IMAGE


def test_latex_inputs(tmp_path):
    tex = tmp_path / "main.tex"
    tex.write_text("\\documentclass{article}\\begin{document}Hi\\end{document}")
    assert detect(tex)[0] == SourceKind.LATEX
    single = tmp_path / "2401.00001"  # arXiv e-print: gzipped single .tex, no extension
    single.write_bytes(gzip.compress(tex.read_bytes()))
    assert detect(single)[0] == SourceKind.LATEX
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        tar.add(tex, arcname="main.tex")
    archive = tmp_path / "2401.00002.tar.gz"
    archive.write_bytes(buf.getvalue())
    assert detect(archive) == (SourceKind.LATEX, "application/gzip")
    other = tmp_path / "data.gz"
    other.write_bytes(gzip.compress(b"just some numbers 1 2 3"))
    with pytest.raises(UnsupportedInputError):
        detect(other)
