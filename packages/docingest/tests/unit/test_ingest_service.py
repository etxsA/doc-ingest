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


def test_swapping_the_image_source_invalidates_image_results_only(src):
    # Old: the image key held only the OCR fingerprint, so frames from another decoder
    # never reached OCR: the transcription of the old decoding was served from the cache.
    png, pdf = src("scan.png", b"png"), src("a.pdf", b"pdf")
    docs = {"a.pdf": [scanned()]}
    svc = make_service(docs)
    svc.ingest(png)
    svc.ingest(pdf)
    other = make_service(docs, images=FakeImages(fingerprint="other-decoder 2"))
    other.store = svc.store
    other.ingest(png)
    other.ingest(pdf)
    assert other.ocr.calls == 1  # the image again; the PDF (no images port) from the cache


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


def test_new_sidecar_reaches_a_cached_result_without_reprocessing(src, tmp_path):
    """A sidecar written after the first run must not need --force (which re-runs OCR)."""
    svc = make_service({"a.pdf": [scanned()]})
    path = src("a.pdf")
    first = svc.ingest(path)
    assert first.manifest.metadata is None and first.manifest.title == "a"
    meta = SourceMetadata(title="Attention Is All You Need", authors=["A. Vaswani"])
    (tmp_path / "a.pdf.meta.json").write_text(meta.model_dump_json())
    second = svc.ingest(path)
    assert svc.ocr.calls == 1  # served from the cache, not re-processed
    assert second.manifest.metadata == meta and second.manifest.title == meta.title
    assert second.location == first.location and second.canonical
    markdown = svc.store.markdown(svc.ingest(path))
    assert markdown.startswith("# Attention Is All You Need\n\n<!-- page 1 | method=vlm_ocr -->")
    assert "# a\n" not in markdown


def test_explicit_metadata_on_a_cache_hit_updates_the_manifest(src):
    """The crawl case: a later crawl learned the license of an already-ingested paper."""
    conv = FakeConverter([Segment("hi")])
    svc = make_service(converters={SourceKind.LATEX: conv})
    path = src("paper.tex")
    svc.ingest(path, metadata=SourceMetadata(title="P", license=None))
    later = SourceMetadata(title="P", license="http://creativecommons.org/licenses/by/4.0/")
    doc = svc.ingest(path, metadata=later)
    assert conv.calls == 1 and doc.manifest.metadata == later
    m = doc.manifest
    hit = svc.store.lookup(m.doc_id, m.config_hash, max_pages=None, ocr_all=False)
    assert hit and hit.manifest.metadata == later  # stored, not only returned


def test_unchanged_or_missing_metadata_leaves_the_cached_result_alone(src, monkeypatch):
    conv = FakeConverter([Segment("hi")], metadata=SourceMetadata(title="From LaTeX"))
    svc = make_service(converters={SourceKind.LATEX: conv})
    path = src("paper.tex")
    first = svc.ingest(path)
    saves = []
    monkeypatch.setattr(svc.store, "save", lambda *a, **kw: saves.append(a))
    # No sidecar and no explicit metadata: the converter's metadata must not be erased.
    assert svc.ingest(path).manifest == first.manifest
    assert svc.ingest(path, metadata=first.manifest.metadata).manifest == first.manifest
    assert saves == [] and conv.calls == 1


def test_degraded_conversion_is_kept_but_never_served_from_the_cache(src):
    """A fallback forced by a timeout must not be cached: the next run retries."""
    conv = FakeConverter([Segment("plain text")], method=PageMethod.LATEX_PLAINTEXT, degraded=True)
    svc = make_service(converters={SourceKind.LATEX: conv})
    path = src("paper.tex")
    first = svc.ingest(path)
    assert not first.canonical and svc.store.markdown(first).endswith("plain text\n")
    svc.ingest(path)
    assert conv.calls == 2  # retried, not served from the cache
    conv.kw["degraded"] = False  # the converter recovered
    good = svc.ingest(path)
    assert good.canonical and conv.calls == 3
    conv.kw["degraded"] = True
    svc.ingest(path, IngestOptions(force=True))  # a degraded --force run...
    cached = svc.ingest(path)  # ...never replaces the good canonical result
    assert cached.canonical and cached.manifest == good.manifest and conv.calls == 4
