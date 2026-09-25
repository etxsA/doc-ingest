"""Fast tests (no model load): detection, OCR routing, text cleanup, caching, CLI, metrics."""

import json
from pathlib import Path

import pypdfium2 as pdfium
import pytest
from PIL import Image, ImageDraw
from typer.testing import CliRunner

from docingest.cli import app
from docingest.config import PipelineConfig, ProbeConfig
from docingest.detect import alpha_ratio, detect_kind, garbage_ratio, probe_pdf_page
from docingest.evaluate import normalize, score
from docingest.extractors.vlm_ocr import fit_image
from docingest.pipeline import Pipeline, clean_text_layer, split_pages
from docingest.schema import PageMethod, SourceKind
from docingest.simulate import make_scan


def _pdf(
    path: Path,
    content: bytes,
    resources: bytes = b"",
    extra: list[bytes] = (),
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


def _text_pdf(path: Path, *lines: str) -> Path:
    ops = b"BT /F1 11 Tf 72 700 Td 14 TL " + b" ".join(
        b"(%s) '" % line.encode() for line in lines
    ) + b" ET"
    return _pdf(path, ops)


def _image_pdf(path: Path) -> Path:
    img = Image.new("RGB", (1275, 1650), "white")
    ImageDraw.Draw(img).text((100, 100), "Scanned words live only in pixels", fill="black")
    img.save(path, "PDF", resolution=150)
    return path


LONG = "Attention is all you need and the transformer architecture works well. " * 3


# ------------------------------------------------------------------ detection
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


def test_text_starting_with_bm_is_not_a_bitmap(tmp_path):
    md = tmp_path / "BMW_notes.md"
    md.write_text("BMW engine notes: the block is aluminium.")
    assert detect_kind(md)[0] == SourceKind.TEXT
    bmp = tmp_path / "real.bmp"
    Image.new("RGB", (4, 4)).save(bmp, "BMP")
    assert detect_kind(bmp) == (SourceKind.IMAGE, "image/bmp")


def test_pdf_header_after_leading_junk(tmp_path):
    src = _text_pdf(tmp_path / "a.pdf", LONG)
    junk = tmp_path / "junk.bin"
    junk.write_bytes(b"\x00garbage-before-header\n" + src.read_bytes())
    assert detect_kind(junk)[0] == SourceKind.PDF


# ------------------------------------------------------------------ routing
def test_born_digital_page_uses_text_layer(tmp_path):
    pdf = pdfium.PdfDocument(_text_pdf(tmp_path / "digital.pdf", LONG))
    probe, raw = probe_pdf_page(pdf[0], ProbeConfig())
    assert not probe.needs_ocr, probe.reasons
    assert "Attention" in raw


def test_scanned_page_routes_to_ocr(tmp_path):
    pdf = pdfium.PdfDocument(_image_pdf(tmp_path / "scan.pdf"))
    probe, _ = probe_pdf_page(pdf[0], ProbeConfig())
    assert probe.needs_ocr
    assert probe.image_coverage > 0.9
    assert probe.n_chars == 0


def test_scan_wrapped_in_form_xobject_with_text_footer_routes_to_ocr(tmp_path):
    """Image nested two Form XObjects deep and scaled by the outer CTM, plus a digital footer."""
    footer = b"BT /F1 9 Tf 72 20 Td (" + b"Digitized by the library, page footer. " * 3 + b") Tj ET"
    content = b"q 612 0 0 792 0 0 cm /Fm1 Do Q " + footer
    img = b"\xff" * 4
    extra = [
        # 6: outer form -> 7: inner form -> 8: image
        b"<< /Type /XObject /Subtype /Form /BBox [0 0 1 1] "
        b"/Resources << /XObject << /Fm2 7 0 R >> >> /Length 8 >>\nstream\n/Fm2 Do\nendstream",
        b"<< /Type /XObject /Subtype /Form /BBox [0 0 1 1] "
        b"/Resources << /XObject << /Im1 8 0 R >> >> /Length 8 >>\nstream\n/Im1 Do\nendstream",
        b"<< /Type /XObject /Subtype /Image /Width 2 /Height 2 /ColorSpace /DeviceGray "
        b"/BitsPerComponent 8 /Length 4 >>\nstream\n" + img + b"\nendstream",
    ]
    path = _pdf(tmp_path / "form_scan.pdf", content, b"/XObject << /Fm1 6 0 R >>", extra)
    probe, _ = probe_pdf_page(pdfium.PdfDocument(path)[0], ProbeConfig())
    assert 50 <= probe.n_chars < 400
    assert probe.image_coverage > 0.95, probe
    assert probe.needs_ocr


def test_garbled_and_symbolic_text_detection():
    assert garbage_ratio("(cid:12)(cid:7)(cid:99) ok") > 0.5
    assert garbage_ratio("clean text") == 0
    assert garbage_ratio("transduc\x02tion") == 0  # pdfium hyphen marker is not garbage
    assert alpha_ratio("∑∫≈ 1234 ## %%") < 0.5
    assert alpha_ratio("plain english words") == 1.0


# ------------------------------------------------------------- text cleanup
def test_clean_text_layer_handles_pdfium_hyphen_marker():
    raw = (
        "The transduc\x02tion model. Transduction works.\r\n"
        "A sequence\x02aligned RNN between 2019-\r\n2020 and Qwen-2.5-\r\nVL-7B."
    )
    out = clean_text_layer(raw)
    assert "\x02" not in out
    assert "transduction model" in out  # joined: the word occurs elsewhere
    assert "sequence-aligned" in out  # compound kept: no "sequencealigned" anywhere
    assert "2019-2020" in out and "Qwen-2.5-VL-7B" in out  # identifiers keep their hyphen


def test_real_pdfium_hyphenation_roundtrip(tmp_path):
    src = _text_pdf(tmp_path / "hy.pdf", "The years 2019-", "2020 saw transduc-", "tion models.")
    raw = pdfium.PdfDocument(src)[0].get_textpage().get_text_bounded()
    out = clean_text_layer(raw)
    assert "\x02" not in out
    assert "2019-2020" in out
    assert "transduc-tion" in out or "transduction" in out


# --------------------------------------------------------- pipeline / cache
def test_pipeline_is_content_addressed_and_cached(tmp_path):
    cfg = PipelineConfig(output_dir=str(tmp_path / "out"))
    src = _text_pdf(tmp_path / "d.pdf", LONG)
    logs: list[str] = []
    m1, out1 = Pipeline(cfg, log=logs.append).ingest(src)
    m2, out2 = Pipeline(cfg, log=logs.append).ingest(src)
    assert out1 == out2 and m1.doc_id == m2.doc_id
    assert any(line.startswith("[cache]") for line in logs)
    assert m1.pages[0].method == PageMethod.TEXT_LAYER
    assert (out1 / "document.md").read_text().startswith("# ")


def test_corrupt_cached_manifest_is_a_cache_miss(tmp_path):
    cfg = PipelineConfig(output_dir=str(tmp_path / "out"))
    src = _text_pdf(tmp_path / "d.pdf", LONG)
    _, out = Pipeline(cfg, log=lambda _: None).ingest(src)
    (out / "manifest.json").write_text('{"doc_id": "trunc')
    m, _ = Pipeline(cfg, log=lambda _: None).ingest(src)  # recomputes instead of raising
    assert m.n_pages == 1


def test_partial_run_never_replaces_complete_result(tmp_path):
    cfg = PipelineConfig(output_dir=str(tmp_path / "out"))
    pdf = pdfium.PdfDocument.new()
    for _ in range(3):
        pdf.new_page(612, 792)
    src = tmp_path / "three.pdf"
    pdf.save(src)
    quiet = {"log": lambda _: None}
    # Blank pages would go to OCR; make them text pages instead by lowering the threshold.
    cfg.probe.min_chars = 0
    cfg.probe.min_alpha_ratio = 0
    full, full_dir = Pipeline(cfg, **quiet).ingest(src)
    assert full.n_pages == 3 and full_dir.name == full.doc_id[:16]
    part, part_dir = Pipeline(cfg, max_pages=1, force=True, **quiet).ingest(src)
    assert part.n_pages == 1 and "_variants" in str(part_dir)
    canonical = json.loads((full_dir / "manifest.json").read_text())
    assert canonical["n_pages"] == 3  # untouched
    # And a --max-pages request is served by the complete canonical result.
    cached, cached_dir = Pipeline(cfg, max_pages=2, **quiet).ingest(src)
    assert cached_dir == full_dir


def test_split_pages_roundtrip_ignores_marker_like_text():
    md = (
        "# T\n\n<!-- page 1 | method=text_layer -->\nhello <!-- page 9 --> world\n\n"
        "<!-- page 2 | method=vlm_ocr -->\nsecond\n"
    )
    assert split_pages(md) == {1: "hello <!-- page 9 --> world", 2: "second"}


# ------------------------------------------------------------------- CLI
def test_cli_batch_survives_bad_file_and_writes_index(tmp_path):
    raw = tmp_path / "raw"
    raw.mkdir()
    (raw / "a_note.md").write_text("# Note\n" + LONG)
    (raw / "b_corrupt.pdf").write_bytes(b"%PDF-1.4\n1 0 obj\n<<")  # truncated
    (raw / "c_note.txt").write_text(LONG)
    cfg_path = tmp_path / "cfg.toml"
    cfg_path.write_text(f'output_dir = "{tmp_path / "out"}"\n')
    result = CliRunner().invoke(app, ["ingest", str(raw), "--config", str(cfg_path)])
    assert result.exit_code == 1, result.output
    index = json.loads((tmp_path / "out" / "index.json").read_text())
    assert sorted(v["source_name"] for v in index.values()) == ["a_note.md", "c_note.txt"]
    failed = result.output.split("Failed inputs", 1)
    assert len(failed) == 2 and "b_corrupt.pdf" in failed[1] and "PdfiumError" in failed[1]


# ------------------------------------------------------ images / eval / scan
def test_fit_image_flattens_transparency_to_white():
    img = Image.new("RGBA", (10, 10), (0, 0, 0, 0))
    assert fit_image(img, 100).getpixel((5, 5)) == (255, 255, 255)


def test_metrics_ignore_markdown_and_hyphenation():
    assert normalize("## **Bold** `x`") == "bold x"
    s = score("the quick brown fox", "# The quick brown fox")
    assert s["cer"] == 0 and s["word_f1"] == 1.0
    assert score("transduction", "transduc-tion")["cer"] == 0
    assert score("", "anything")["cer"] is None


def test_make_scan_is_byte_identical_across_runs(tmp_path):
    src = _text_pdf(tmp_path / "d.pdf", LONG)
    t1 = json.loads(make_scan(src, tmp_path / "s1.pdf", [0]).read_text())
    t2 = json.loads(make_scan(src, tmp_path / "s2.pdf", [0]).read_text())
    assert t1["scan_sha256"] == t2["scan_sha256"]
    data = (tmp_path / "s1.pdf").read_bytes()
    assert b"/CreationDate" not in data and b"/ModDate" not in data
    with pytest.raises(ValueError):
        make_scan(src, tmp_path / "s3.pdf", [5])


def test_text_mentioning_pdf_header_is_not_a_pdf(tmp_path):
    md = tmp_path / "notes.md"
    md.write_text("Every PDF file starts with the header `%PDF-1.7`.")
    assert detect_kind(md)[0] == SourceKind.TEXT
    html = tmp_path / "page.html"
    html.write_text("<p>The magic bytes are %PDF-1.4</p>")
    assert detect_kind(html)[0] == SourceKind.OFFICE
    from PIL.PngImagePlugin import PngInfo

    info = PngInfo()
    info.add_text("Comment", "converted from %PDF-1.4 source")
    png = tmp_path / "scan.png"
    Image.new("RGB", (8, 8)).save(png, pnginfo=info)
    assert detect_kind(png)[0] == SourceKind.IMAGE


@pytest.mark.parametrize(
    ("page_box", "cm"),
    [
        (b"/MediaBox [0 0 400 1000] /Rotate 90", b"400 0 0 1000 0 0"),
        (b"/MediaBox [0 792 612 1584]", b"612 0 0 792 0 792"),
        (b"/MediaBox [0 0 1224 792] /CropBox [612 0 1224 792]", b"612 0 0 792 612 0"),
        (b"/MediaBox [-306 -396 306 396]", b"612 0 0 792 -306 -396"),
    ],
)
def test_full_page_scan_detected_on_rotated_or_offset_boxes(tmp_path, page_box, cm):
    stamp = b"BT /F1 9 Tf 20 20 Td (" + b"Scanned by the archive service. " * 3 + b") Tj ET"
    # the stamp position must be inside each box: shift it with the image origin
    x, y = cm.split()[4:6]
    content = b"q " + cm + b" cm /Im1 Do Q BT /F1 9 Tf " + x + b" " + y + b" Td ET " + stamp.replace(
        b"20 20 Td", b"%s %s Td" % (str(float(x) + 20).encode(), str(float(y) + 20).encode())
    )
    img = b"<< /Type /XObject /Subtype /Image /Width 2 /Height 2 /ColorSpace /DeviceGray "
    img += b"/BitsPerComponent 8 /Length 4 >>\nstream\n\xff\xff\xff\xff\nendstream"
    path = _pdf(tmp_path / "boxed.pdf", content, b"/XObject << /Im1 6 0 R >>", [img], page_box)
    probe, _ = probe_pdf_page(pdfium.PdfDocument(path)[0], ProbeConfig())
    assert 50 <= probe.n_chars < 400, probe  # routed by coverage, not by an empty text layer
    assert probe.image_coverage > 0.95, probe
    assert probe.needs_ocr


def test_invalid_max_pages_rejected_and_never_hits_full_run_cache(tmp_path):
    cfg = PipelineConfig(output_dir=str(tmp_path / "out"))
    with pytest.raises(ValueError):
        Pipeline(cfg, max_pages=0)
    result = CliRunner().invoke(app, ["ingest", str(tmp_path), "--max-pages", "0"])
    assert result.exit_code != 0


def test_outputs_have_normal_file_permissions(tmp_path):
    import os
    import stat

    cfg = PipelineConfig(output_dir=str(tmp_path / "out"))
    _, out = Pipeline(cfg, log=lambda _: None).ingest(_text_pdf(tmp_path / "d.pdf", LONG))
    umask = os.umask(0)
    os.umask(umask)
    for name in ("document.md", "manifest.json"):
        assert stat.S_IMODE((out / name).stat().st_mode) == 0o666 & ~umask
    assert not list(out.glob(".*.tmp"))


def test_token_windows_cover_long_chunks_within_limit():
    pytest.importorskip("paperqa")
    from paperqa.types import Doc, Text
    from transformers import AutoTokenizer

    from docingest.qa import _token_windows

    try:
        from docingest.models import embedding_path

        tok = AutoTokenizer.from_pretrained(embedding_path(PipelineConfig()))
    except Exception:
        pytest.skip("embedder not in local HF cache")
    doc = Doc(docname="d", dockey="d", citation="c")
    long = " ".join(f"word{i}" for i in range(600))
    out = _token_windows([Text(text=long, name="d pages 1-1", doc=doc)], tok, 254)
    assert len(out) > 1 and all(t.name == "d pages 1-1" for t in out)
    assert all(len(tok(t.text, add_special_tokens=False)["input_ids"]) <= 254 for t in out)
    assert out[0].text.startswith("word0") and out[-1].text.endswith("word599")


def test_corpus_falls_back_to_partial_run_with_warning(tmp_path):
    pytest.importorskip("paperqa")
    from docingest.qa import _corpus_manifests

    cfg = PipelineConfig(output_dir=str(tmp_path / "out"))
    cfg.probe.min_chars = 0
    cfg.probe.min_alpha_ratio = 0
    pdf = pdfium.PdfDocument.new()
    for _ in range(3):
        pdf.new_page(612, 792)
    src = tmp_path / "three.pdf"
    pdf.save(src)
    Pipeline(cfg, max_pages=2, log=lambda _: None).ingest(src)
    warnings: list[str] = []
    corpus = _corpus_manifests(tmp_path / "out", warnings.append)
    assert len(corpus) == 1 and corpus[0][0].n_pages == 2
    assert any("partial" in w for w in warnings)
