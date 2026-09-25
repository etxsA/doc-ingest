"""AskService and CrawlService with fakes."""

import asyncio

import pytest
from fakes import FakeConverter, FakeCrawler, FakeDetector, FakeImages, FakeOcr, FakePdfReader
from fakes import FakeQA, InMemoryStore, record

from docingest.application.ask import AskService
from docingest.application.crawl import CrawlService
from docingest.application.ingest import IngestService
from docingest.domain.models import SourceKind
from docingest.domain.routing import RoutingPolicy
from docingest.ports import Segment


def ingest_service(store):
    return IngestService(
        detector=FakeDetector(),
        pdf=FakePdfReader(),
        ocr=FakeOcr(),
        images=FakeImages(),
        converters={SourceKind.LATEX: FakeConverter([Segment("body", "Intro")])},
        store=store,
        policy=RoutingPolicy(),
        log=lambda _: None,
    )


def test_crawl_fetches_writes_sidecar_ingests_and_survives_failures(tmp_path):
    store = InMemoryStore()
    crawler = FakeCrawler([record("2401.00001"), record("2401.00002")], fail_keys={"2401.00002"})
    report = CrawlService(
        crawler=crawler, ingest=ingest_service(store), raw_dir=tmp_path, log=lambda _: None
    ).run("cat:cs.CL", limit=5)
    assert len(report.fetched) == 1 and len(report.ingested) == 1
    assert report.failures[0][0] == "2401.00002"
    assert (tmp_path / "2401.00001.tex.meta.json").exists()
    assert report.ingested[0].manifest.metadata.arxiv_id == "2401.00001"


def test_ask_uses_corpus_and_reports_partial_runs():
    store = InMemoryStore()
    qa = FakeQA()
    warnings: list[str] = []
    with pytest.raises(RuntimeError):
        asyncio.run(AskService(store=store, qa=qa).ask("q?", warnings.append))
    from docingest.domain.models import DocumentManifest

    m = DocumentManifest(
        doc_id="a" * 64, source_path="x", source_name="x.pdf", source_kind=SourceKind.PDF,
        mime="application/pdf", size_bytes=1, n_pages=1, source_pages=2, max_pages=1,
        pages=[], pipeline_version="t", config_hash="h",
    )
    store.save(m, "# x\n")
    answer = asyncio.run(AskService(store=store, qa=qa).ask("q?", warnings.append))
    assert "1 docs" in answer and qa.seen == ["x.pdf"]
    assert any("partial" in w for w in warnings)
