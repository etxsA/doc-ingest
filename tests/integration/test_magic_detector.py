import gzip
import io
import tarfile
from pathlib import Path

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
    for payload in (b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n", b"%!PS-Adobe-3.0\n", b"<!DOCTYPE html>"):
        other = tmp_path / "not_tex.gz"  # gzipped PDF / PostScript / HTML: not a source
        other.write_bytes(gzip.compress(payload))
        with pytest.raises(UnsupportedInputError):
            detect(other)


def _v7_tar(path: Path, tex: bytes) -> bytes:
    """A pre-POSIX tar: no "ustar" magic, which tarfile (and the crawler) still accept."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.USTAR_FORMAT) as tar:
        info = tarfile.TarInfo("main.tex")
        info.size = len(tex)
        tar.addfile(info, io.BytesIO(tex))
    raw = bytearray(buf.getvalue())
    raw[257:265] = b"\0" * 8  # drop magic + version
    raw[148:156] = b" " * 8
    raw[148:156] = b"%06o\0 " % sum(raw[:512])
    path.write_bytes(bytes(raw))
    with tarfile.open(path) as tar:
        assert tar.getnames() == ["main.tex"]
    return bytes(raw)


def test_whatever_the_arxiv_crawler_saves_as_latex_is_latex(tmp_path):
    """The crawler keeps these files (and reuses them on every later crawl), so the
    detector refusing them would make those papers impossible to ingest."""
    from docingest.adapters.sources.arxiv import classify

    doc = b"\\documentclass{article}\\begin{document}\\section{Intro}Hi\\end{document}\n"
    samples = {
        # ~5 KB of comment header before \documentclass
        "long.tex.gz": gzip.compress((b"% " + b"x" * 70 + b"\n") * 75 + doc),
        "plain.tex.gz": gzip.compress(b"\\input harvmac\n\\Title{}{A paper}\n\\bye\n"),  # plain TeX
        "numbers.tex.gz": gzip.compress(b"just some numbers 1 2 3"),
    }
    for name, payload in samples.items():
        (tmp_path / name).write_bytes(payload)
    v7 = tmp_path / "v7.tar"
    raw = _v7_tar(v7, doc)
    (tmp_path / "v7.tar.gz").write_bytes(gzip.compress(raw))
    for path in sorted(tmp_path.iterdir()):
        assert classify(path) is not None, path.name  # the crawler would save it
        assert detect(path)[0] == SourceKind.LATEX, path.name


def test_truncated_gzip_is_unsupported_input(tmp_path):
    full = gzip.compress(b"\\documentclass{article}\n" + b"a" * 10000)
    trunc = tmp_path / "trunc.tex.gz"
    trunc.write_bytes(full[:40])
    with pytest.raises(UnsupportedInputError, match="corrupt gzip"):
        detect(trunc)


def test_text_starting_with_a_pdf_comment_is_not_a_pdf(tmp_path):
    thesis = tmp_path / "thesis.tex"
    thesis.write_text("%PDF-A compliant thesis template\n\\documentclass{article}\n")
    assert detect(thesis)[0] == SourceKind.LATEX
    notes = tmp_path / "notes.txt"
    notes.write_text("%PDF-1.4 is the version used here.\n")
    assert detect(notes)[0] == SourceKind.TEXT
    # Extensions lie: a real PDF (binary comment on line 2, as pdfTeX / Word / Acrobat
    # write it) named .txt is still a PDF.
    body = text_pdf(tmp_path / "a.pdf", LONG).read_bytes().removeprefix(b"%PDF-1.4\n")
    mislabeled = tmp_path / "actually_pdf.txt"
    mislabeled.write_bytes(b"%PDF-1.5\n%\xe2\xe3\xcf\xd3\n" + body)
    assert detect(mislabeled)[0] == SourceKind.PDF
