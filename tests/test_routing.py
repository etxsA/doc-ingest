"""Fast tests (no model load): type detection, OCR routing heuristics, metrics."""

from pathlib import Path

import pypdfium2 as pdfium
import pytest
from PIL import Image, ImageDraw

from docingest.config import PipelineConfig, ProbeConfig
from docingest.detect import alpha_ratio, detect_kind, garbage_ratio, probe_pdf_page
from docingest.evaluate import normalize, score
from docingest.pipeline import Pipeline, clean_text_layer
from docingest.schema import PageMethod, SourceKind


def _text_pdf(path: Path, text: str) -> Path:
    """Minimal born-digital PDF with a real Helvetica text layer (no extra deps)."""
    stream = f"BT /F1 11 Tf 72 700 Td ({text}) Tj ET".encode()
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
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


def _image_pdf(path: Path) -> Path:
    img = Image.new("RGB", (1275, 1650), "white")
    ImageDraw.Draw(img).text((100, 100), "Scanned words live only in pixels", fill="black")
    img.save(path, "PDF", resolution=150)
    return path


def test_detect_kind_by_magic_not_extension(tmp_path):
    fake = tmp_path / "actually_png.pdf"
    Image.new("RGB", (10, 10)).save(fake, "PNG")
    assert detect_kind(fake) == (SourceKind.IMAGE, "image/png")
    md = tmp_path / "notes.md"
    md.write_text("# hi")
    assert detect_kind(md)[0] == SourceKind.TEXT
    with pytest.raises(ValueError):
        (tmp_path / "x.xyz").write_bytes(b"\x00\x01")
        detect_kind(tmp_path / "x.xyz")


def test_born_digital_page_uses_text_layer(tmp_path):
    text = "Attention is all you need. " * 10
    pdf = pdfium.PdfDocument(_text_pdf(tmp_path / "digital.pdf", text))
    probe, raw = probe_pdf_page(pdf[0], ProbeConfig())
    assert not probe.needs_ocr, probe.reasons
    assert "Attention" in raw


def test_scanned_page_routes_to_ocr(tmp_path):
    pdf = pdfium.PdfDocument(_image_pdf(tmp_path / "scan.pdf"))
    probe, _ = probe_pdf_page(pdf[0], ProbeConfig())
    assert probe.needs_ocr
    assert probe.image_coverage > 0.9
    assert probe.n_chars == 0


def test_garbled_and_symbolic_text_detection():
    assert garbage_ratio("(cid:12)(cid:7)(cid:99) ok") > 0.5
    assert garbage_ratio("clean text") == 0
    assert alpha_ratio("∑∫≈ 1234 ## %%") < 0.5
    assert alpha_ratio("plain english words") == 1.0


def test_clean_text_layer_dehyphenates():
    assert clean_text_layer("trans-\nformer\r\n\n\n\nnext") == "transformer\n\nnext"


def test_metrics_ignore_markdown_syntax():
    assert normalize("## **Bold** `x`") == "bold x"
    s = score("the quick brown fox", "# The quick brown fox")
    assert s["cer"] == 0 and s["word_f1"] == 1.0


def test_pipeline_is_content_addressed_and_cached(tmp_path):
    cfg = PipelineConfig(output_dir=str(tmp_path / "out"))
    src = _text_pdf(tmp_path / "d.pdf", "Cached document text " * 10)
    logs: list[str] = []
    m1, out1 = Pipeline(cfg, log=logs.append).ingest(src)
    m2, out2 = Pipeline(cfg, log=logs.append).ingest(src)
    assert out1 == out2 and m1.doc_id == m2.doc_id
    assert any(line.startswith("[cache]") for line in logs)
    assert m1.pages[0].method == PageMethod.TEXT_LAYER
    assert (out1 / "document.md").read_text().startswith("# ")
