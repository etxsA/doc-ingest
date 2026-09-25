"""IngestService with in-memory fakes for every port: no files parsed, no model loaded."""

from pathlib import Path

import pytest
from fakes import (
    FakeConverter,
    FakeDetector,
    FakeImages,
    FakeOcr,
    FakePdfReader,
    InMemoryStore,
    digital,
    scanned,
)

from docingest.application.ingest import IngestOptions, IngestService
from docingest.domain.errors import DocumentOpenError, UnsupportedInputError
from docingest.domain.models import PageMethod, SourceKind, SourceMetadata
from docingest.domain.routing import RoutingPolicy
from docingest.ports import Segment

TEXT = "Attention is all you need. " * 10


def make_service(docs=None, converters=None, ocr=None, images=None):
    return IngestService(
        detector=FakeDetector(),
        pdf=FakePdfReader(docs or {}),
        ocr=ocr or FakeOcr(),
        images=images or FakeImages(),
        converters=converters or {},
        store=InMemoryStore(),
        policy=RoutingPolicy(),
        log=lambda _: None,
    )


@pytest.fixture
def src(tmp_path) -> Path:
    def _make(name: str, content: bytes = b"x") -> Path:
        p = tmp_path / name
        p.write_bytes(content)
        return p

    return _make


def test_mixed_pdf_routes_each_page(src):
    svc = make_service({"mixed.pdf": [digital(TEXT), scanned(), digital(TEXT)]})
    doc = svc.ingest(src("mixed.pdf"))
    methods = [p.method for p in doc.manifest.pages]
    assert methods == [PageMethod.TEXT_LAYER, PageMethod.VLM_OCR, PageMethod.TEXT_LAYER]
    assert svc.ocr.calls == 1
    assert doc.manifest.ocr_model is not None and doc.canonical


def test_second_run_is_served_from_cache(src):
    svc = make_service({"a.pdf": [scanned()]})
    path = src("a.pdf")
    first = svc.ingest(path)
    second = svc.ingest(path)
    assert svc.ocr.calls == 1 and second.manifest.doc_id == first.manifest.doc_id
    svc.ingest(path, IngestOptions(force=True))
    assert svc.ocr.calls == 2


def test_swapping_the_ocr_adapter_invalidates_the_cache(src):
    path = src("a.pdf")
    docs = {"a.pdf": [scanned()]}
    svc = make_service(docs)
    store = svc.store
    svc.ingest(path)
    other = make_service(docs, ocr=FakeOcr(model="fake/other"))
    other.store = store
    other.ingest(path)
    assert other.ocr.calls == 1  # different fingerprint -> different config hash


def test_partial_run_is_a_variant_and_complete_run_serves_it(src):
    svc = make_service({"a.pdf": [digital(TEXT)] * 3})
    path = src("a.pdf")
    part = svc.ingest(path, IngestOptions(max_pages=1))
    assert not part.canonical and part.manifest.n_pages == 1
    full = svc.ingest(path)
    assert full.canonical and full.manifest.n_pages == 3
    again = svc.ingest(path, IngestOptions(max_pages=2))
    assert again.canonical  # complete result covers any --max-pages request


def test_ocr_all_forces_ocr_and_is_a_variant(src):
    svc = make_service({"a.pdf": [digital(TEXT)]})
    doc = svc.ingest(src("a.pdf"), IngestOptions(ocr_all=True))
    assert doc.manifest.pages[0].method == PageMethod.VLM_OCR and not doc.canonical


def test_images_go_to_ocr_frame_by_frame(src):
    svc = make_service(images=FakeImages(n_frames=3))
    doc = svc.ingest(src("scan.png"), IngestOptions(max_pages=2))
    assert doc.manifest.n_pages == 2 and doc.manifest.source_pages == 3
    assert svc.ocr.calls == 2


def test_converters_produce_segments_and_metadata(src):
    conv = FakeConverter(
        [Segment("Intro text", "Introduction"), Segment("Method text", "Method")],
        title="From LaTeX",
        metadata=SourceMetadata(title="Paper", authors=["A. Author"], year=2024),
    )
    svc = make_service(converters={SourceKind.LATEX: conv})
    doc = svc.ingest(src("paper.tex"))
    m = doc.manifest
    assert [p.title for p in m.pages] == ["Introduction", "Method"]
    assert all(p.method == PageMethod.LATEX for p in m.pages)
    assert m.title == "Paper" and m.metadata.year == 2024


def test_explicit_metadata_wins_and_sidecar_is_read(src, tmp_path):
    svc = make_service(converters={SourceKind.TEXT: FakeConverter([Segment("hi")])})
    path = src("note.md")
    (tmp_path / "note.md.meta.json").write_text(SourceMetadata(title="Sidecar").model_dump_json())
    assert svc.ingest(path).manifest.title == "Sidecar"
    doc = svc.ingest(path, IngestOptions(force=True), SourceMetadata(title="Explicit"))
    assert doc.manifest.title == "Explicit"


def test_missing_converter_and_unknown_type_are_domain_errors(src):
    svc = make_service()
    with pytest.raises(UnsupportedInputError):
        svc.ingest(src("paper.tex"))
    with pytest.raises(UnsupportedInputError):
        svc.ingest(src("data.xyz"))


def test_unreadable_pdf_is_a_domain_error(src):
    with pytest.raises(DocumentOpenError):
        make_service().ingest(src("broken.pdf"))


def test_invalid_max_pages_rejected():
    with pytest.raises(ValueError, match="max_pages"):
        IngestOptions(max_pages=0)
