"""Real detector / pdfium / pillow / filesystem adapters wired by the container; fake OCR."""

from builders import LONG, blank_pdf, image_pdf, text_pdf
from fakes import FakeOcr
from PIL import Image

from docingest.application.ingest import IngestOptions
from docingest.bootstrap import Container
from docingest.domain.models import PageMethod


def container(cfg):
    return Container(cfg, log=lambda _: None, overrides={"ocr": FakeOcr("# OCR page")})


def test_digital_and_scanned_pdfs(cfg, tmp_path):
    svc = container(cfg).ingest
    digital = svc.ingest(text_pdf(tmp_path / "d.pdf", LONG))
    assert digital.manifest.pages[0].method == PageMethod.TEXT_LAYER
    scan = svc.ingest(image_pdf(tmp_path / "s.pdf"))
    assert scan.manifest.pages[0].method == PageMethod.VLM_OCR
    assert "# OCR page" in svc.store.markdown(scan)


def test_image_and_markdown_inputs(cfg, tmp_path):
    svc = container(cfg).ingest
    png = tmp_path / "page.png"
    Image.new("RGB", (40, 40), "white").save(png)
    assert svc.ingest(png).manifest.pages[0].method == PageMethod.VLM_OCR
    md = tmp_path / "notes.md"
    md.write_text("# Notes\n" + LONG)
    doc = svc.ingest(md)
    assert doc.manifest.pages[0].method == PageMethod.PASSTHROUGH


def test_partial_run_never_replaces_complete_result(cfg, tmp_path):
    cfg.routing.min_chars = 0
    cfg.routing.min_alpha_ratio = 0
    svc = container(cfg).ingest
    src = blank_pdf(tmp_path / "three.pdf", 3)
    full = svc.ingest(src)
    part = svc.ingest(src, IngestOptions(max_pages=1, force=True))
    assert full.canonical and not part.canonical and "_variants" in part.location
    assert svc.ingest(src, IngestOptions(max_pages=2)).location == full.location
