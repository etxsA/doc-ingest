import pypdfium2 as pdfium
import pytest
from builders import LONG, image_pdf, pdf, text_pdf

from docingest.adapters.pdf.pdfium import PdfiumReader
from docingest.domain.errors import DocumentOpenError
from docingest.domain.routing import RoutingPolicy, decide
from docingest.domain.text import clean_text_layer

reader = PdfiumReader()


def probe(path):
    doc = reader.open(path)
    signals, raw = doc.page(0).signals()
    return decide(signals, RoutingPolicy()), raw


def test_born_digital_page_uses_text_layer(tmp_path):
    p, raw = probe(text_pdf(tmp_path / "digital.pdf", LONG))
    assert not p.needs_ocr, p.reasons
    assert "Attention" in raw


def test_scanned_page_routes_to_ocr(tmp_path):
    p, _ = probe(image_pdf(tmp_path / "scan.pdf"))
    assert p.needs_ocr and p.image_coverage > 0.9 and p.n_chars == 0


def test_scan_wrapped_in_form_xobject_with_text_footer_routes_to_ocr(tmp_path):
    """Image nested two Form XObjects deep and scaled by the outer CTM, plus a digital footer."""
    footer = b"BT /F1 9 Tf 72 20 Td (" + b"Digitized by the library, page footer. " * 3 + b") Tj ET"
    content = b"q 612 0 0 792 0 0 cm /Fm1 Do Q " + footer
    extra = [
        b"<< /Type /XObject /Subtype /Form /BBox [0 0 1 1] "
        b"/Resources << /XObject << /Fm2 7 0 R >> >> /Length 8 >>\nstream\n/Fm2 Do\nendstream",
        b"<< /Type /XObject /Subtype /Form /BBox [0 0 1 1] "
        b"/Resources << /XObject << /Im1 8 0 R >> >> /Length 8 >>\nstream\n/Im1 Do\nendstream",
        b"<< /Type /XObject /Subtype /Image /Width 2 /Height 2 /ColorSpace /DeviceGray "
        b"/BitsPerComponent 8 /Length 4 >>\nstream\n\xff\xff\xff\xff\nendstream",
    ]
    path = pdf(tmp_path / "form_scan.pdf", content, b"/XObject << /Fm1 6 0 R >>", extra)
    p, _ = probe(path)
    assert 50 <= p.n_chars < 400
    assert p.image_coverage > 0.95, p
    assert p.needs_ocr


@pytest.mark.parametrize(
    ("page_box", "cm"),
    [
        (b"/MediaBox [0 0 400 1000] /Rotate 90", (400, 1000, 0, 0)),
        (b"/MediaBox [0 792 612 1584]", (612, 792, 0, 792)),
        (b"/MediaBox [0 0 1224 792] /CropBox [612 0 1224 792]", (612, 792, 612, 0)),
        (b"/MediaBox [-306 -396 306 396]", (612, 792, -306, -396)),
    ],
)
def test_full_page_scan_detected_on_rotated_or_offset_boxes(tmp_path, page_box, cm):
    w, h, x, y = cm
    stamp = b"Scanned by the archive service. " * 3
    content = (
        b"q %d 0 0 %d %d %d cm /Im1 Do Q " % (w, h, x, y)
        + b"BT /F1 9 Tf %d %d Td (" % (x + 20, y + 20)
        + stamp
        + b") Tj ET"
    )
    img = (
        b"<< /Type /XObject /Subtype /Image /Width 2 /Height 2 /ColorSpace /DeviceGray "
        b"/BitsPerComponent 8 /Length 4 >>\nstream\n\xff\xff\xff\xff\nendstream"
    )
    path = pdf(tmp_path / "boxed.pdf", content, b"/XObject << /Im1 6 0 R >>", [img], page_box)
    p, _ = probe(path)
    assert 50 <= p.n_chars < 400, p  # routed by coverage, not by an empty text layer
    assert p.image_coverage > 0.95, p
    assert p.needs_ocr


def test_real_pdfium_hyphenation_roundtrip(tmp_path):
    src = text_pdf(tmp_path / "hy.pdf", "The years 2019-", "2020 saw transduc-", "tion models.")
    raw = pdfium.PdfDocument(src)[0].get_textpage().get_text_bounded()
    out = clean_text_layer(raw)
    assert "\x02" not in out
    assert "2019-2020" in out
    assert "transduc-tion" in out  # no "transduction" elsewhere -> hyphen kept


def test_corrupt_pdf_is_a_document_open_error(tmp_path):
    bad = tmp_path / "bad.pdf"
    bad.write_bytes(b"%PDF-1.4\n1 0 obj\n<<")
    with pytest.raises(DocumentOpenError):
        reader.open(bad)
