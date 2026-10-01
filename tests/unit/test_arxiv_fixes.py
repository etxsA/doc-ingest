"""Regressions from the third review of the arXiv crawler (fake transport, fake clock).

* a server's ``Retry-After`` pauses every later request and a long one stops the crawl
  (no PDF fallback on the same host, remaining records reported as not attempted);
* a failed license lookup keeps the license an earlier crawl recorded;
* a PDF saved because the LaTeX source failed transiently is upgraded on a later crawl;
* a transiently empty API page is asked for again instead of ending the search;
* Atom entries without an arXiv id are skipped, and pagination still counts them.
"""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
from fakes import FakeCrawler
from fakes import record as fake_record
from test_arxiv import (
    API,
    OAI,
    PDF,
    PDF_BYTES,
    SRC,
    FakeClock,
    FakeTransport,
    Reply,
    feed,
    fixture,
    make,
    paper,
    src_reply,
    tar_gz,
)

from docingest.adapters.sources.arxiv import parse_entry, parse_feed
from docingest.adapters.sources.http import PoliteClient, RateLimiter, RetriesExhaustedError
from docingest.application.crawl import CrawlService
from docingest.domain.errors import RateLimitedError, SourceUnavailableError
from docingest.domain.models import SourceMetadata

LICENSE = "http://creativecommons.org/licenses/by-sa/4.0/"  # the recorded OAI fixture
NS = 'xmlns="http://www.w3.org/2005/Atom" xmlns:opensearch="http://a9.com/-/spec/opensearch/1.1/"'


def client(transport: FakeTransport, clock: FakeClock, **kw) -> PoliteClient:
    kw = {"delay_s": 3.0, "retries": 0, **kw}
    return PoliteClient(
        user_agent="t", timeout_s=1, transport=transport, clock=clock, sleep=clock.sleep, **kw
    )


def crawl(crawler, raw: Path, query: str, limit: int = 1, **kw):
    service = CrawlService(crawler=crawler, ingest=None, raw_dir=raw, log=lambda _: None)
    return service.run(query, limit, ingest=False, **kw)


def sidecar_license(path: Path) -> str | None:
    return json.loads(path.with_name(path.name + ".meta.json").read_text())["license"]


# ------------------------------------------------------------ Retry-After / limits
def test_rate_limiter_defer_holds_the_next_start_and_never_shortens():
    clock = FakeClock()
    limiter = RateLimiter(3.0, clock, clock.sleep)
    limiter.wait()
    limiter.defer(60.0)
    limiter.defer(10.0)  # a shorter pause must not cut the longer one
    assert limiter.paused_s() == 60.0
    assert limiter.wait() == 60.0 and clock.sleeps == [60.0]
    assert limiter.paused_s() == 0.0

    fresh = RateLimiter(3.0, clock, clock.sleep)
    fresh.defer(5.0)  # a pause applies even before the first request
    assert fresh.wait() == 5.0


def test_a_retry_after_on_the_last_attempt_pauses_the_next_url_too():
    """Old code: the next URL started after delay_s (3 s), not after the 120 s asked for."""
    clock = FakeClock()
    transport = FakeTransport(clock)
    transport.route("http://x/a", Reply(503, headers={"Retry-After": "120"}))
    transport.route("http://x/b", Reply(200, b"ok"))
    http = client(transport, clock)
    with pytest.raises(RateLimitedError, match="gave up after 1 attempt"):
        http.get("http://x/a")
    assert http.get("http://x/b").body == b"ok"
    (t0, _, _), (t1, _, _) = transport.calls
    assert t1 - t0 >= 120


def test_while_a_long_pause_is_owed_no_request_is_sent():
    clock = FakeClock()
    transport = FakeTransport(clock)
    transport.route("http://x/a", Reply(503, headers={"Retry-After": "3600"}))
    transport.route("http://x/b", Reply(200, b"ok"))
    http = client(transport, clock, retries=3)
    with pytest.raises(RateLimitedError, match="retry after 3600s") as first:
        http.get("http://x/a")
    assert first.value.retry_after_s == 3600
    with pytest.raises(RateLimitedError, match="not sent"):
        http.get("http://x/b")
    assert len(transport.calls) == 1 and clock.sleeps == []  # failed fast, nothing waited

    clock.now += 3600  # the pause is over: requests flow again
    assert http.get("http://x/b").body == b"ok"


def test_exhausted_retries_are_rate_limited_only_when_told_to_back_off():
    clock = FakeClock()
    transport = FakeTransport(clock)
    transport.route("http://x/busy", Reply(503))
    transport.route("http://x/429", Reply(429))
    http = client(transport, clock, retries=1)
    with pytest.raises(RetriesExhaustedError) as e:
        http.get("http://x/busy")
    assert not isinstance(e.value, RateLimitedError)  # a crawler may fall back on this
    with pytest.raises(RateLimitedError, match="HTTP 429"):
        http.get("http://x/429")


def test_a_long_retry_after_stops_the_crawl_without_a_pdf_fallback(tmp_path):
    """Old code: /src, then /pdf 3 s later, then the next record's /src... every 3 s."""
    crawler, transport, clock, _ = make()
    transport.route(API, Reply(200, feed("2401.00001v1", "2401.00002v1", "2401.00003v1")))
    transport.route("https://arxiv.org/", Reply(503, b"busy", {"Retry-After": "3600"}))
    report = crawl(crawler, tmp_path, "cat:cs.CL", 3)
    assert transport.urls("https://arxiv.org/") == [SRC + "2401.00001v1"]
    assert transport.urls(PDF) == [] and max(clock.sleeps) <= 3.0
    assert report.stopped and "3600" in report.stopped
    first, *rest = report.failures
    assert first[0] == "2401.00001v1" and first[1].startswith("RateLimitedError")
    assert [k for k, _ in rest] == ["2401.00002v1", "2401.00003v1"]
    assert all(why.startswith("not attempted") for _, why in rest)
    assert list(tmp_path.iterdir()) == []


def test_crawl_service_stops_on_rate_limit_from_any_crawler(tmp_path):
    class Limited(FakeCrawler):
        def fetch(self, record, dest_dir):
            if record.key == "b":
                raise RateLimitedError("slow down", retry_after_s=600)
            return super().fetch(record, dest_dir)

    crawler = Limited([fake_record(k) for k in ("a", "b", "c", "d")])
    report = crawl(crawler, tmp_path, "q", 4)
    assert [f.record.key for f in report.fetched] == ["a"]
    assert [k for k, _ in report.failures] == ["b", "c", "d"]
    assert report.stopped and "slow down" in report.stopped


def test_a_rate_limited_license_lookup_keeps_the_file_and_the_next_download_is_refused(
    tmp_path,
):
    crawler, transport, _, logs = make()
    transport.route(SRC, src_reply(tar_gz(), "arXiv-2409.13740v2.tar.gz"))
    transport.route(OAI, Reply(429, headers={"Retry-After": "3600"}))
    fetched = crawler.fetch(paper(), tmp_path)  # the source is on disk: keep it
    assert fetched.format == "latex-archive" and any("license unknown" in x for x in logs)
    with pytest.raises(RateLimitedError, match="not sent"):
        crawler.fetch(fake_record("2401.00001v1"), tmp_path)
    assert len(transport.calls) == 2


# ------------------------------------------------------------------------ license
def test_a_failed_license_lookup_keeps_the_license_recorded_earlier(tmp_path):
    """Old code: the second crawl overwrote the sidecar with license=null."""
    seen: list[SourceMetadata] = []

    class Ingest:
        def ingest(self, path, options, metadata):
            seen.append(metadata)
            return path

    for oai in (Reply(200, fixture("oai_2409.13740.xml")), Reply(503)):
        crawler, transport, _, _ = make(retries=0)
        transport.route(API, Reply(200, fixture("api_query.xml")))
        transport.route(SRC, src_reply(tar_gz(), "arXiv-2409.13740v2.tar.gz"))
        transport.route(OAI, oai)
        service = CrawlService(crawler=crawler, ingest=Ingest, raw_dir=tmp_path, log=lambda _: None)
        report = service.run("ids:2409.13740", 1)
        assert not report.failures and sidecar_license(report.fetched[0].path) == LICENSE
    assert [m.license for m in seen] == [LICENSE, LICENSE]  # what ingest got, both runs


def test_an_unreadable_sidecar_is_rewritten_not_fatal(tmp_path):
    crawler = FakeCrawler([fake_record("2401.00001")])
    (tmp_path / "2401.00001.tex.meta.json").write_text("{not json")
    report = crawl(crawler, tmp_path, "q")
    assert not report.failures and sidecar_license(report.fetched[0].path) is None


# ------------------------------------------------------------ fallback lock-in
def test_a_transient_fallback_to_pdf_is_upgraded_on_the_next_fetch(tmp_path):
    """Old code: the PDF was reused forever, with zero /src requests."""
    crawler, transport, _, _ = make(retries=0)
    transport.route(API, Reply(200, fixture("api_query.xml")))
    transport.route(SRC, Reply(503))
    transport.route(PDF, Reply(200, PDF_BYTES))
    transport.route(OAI, Reply(200, fixture("oai_2409.13740.xml")))
    first = crawl(crawler, tmp_path, "ids:2409.13740").fetched[0]
    assert first.format == "pdf" and (tmp_path / ".2409.13740v2.fallback").exists()

    crawler, transport, _, _ = make(retries=0)  # /src is healthy again; OAI is down
    transport.route(API, Reply(200, fixture("api_query.xml")))
    transport.route(SRC, src_reply(tar_gz(), "arXiv-2409.13740v2.tar.gz"))
    transport.route(OAI, Reply(503))
    second = crawl(crawler, tmp_path, "ids:2409.13740").fetched[0]
    assert second.format == "latex-archive" and not second.reused
    assert transport.urls(SRC) == [SRC + "2409.13740v2"] and transport.urls(PDF) == []
    assert sorted(p.name for p in tmp_path.iterdir()) == [
        "2409.13740v2.tar.gz",
        "2409.13740v2.tar.gz.meta.json",
    ]  # the stopgap PDF and the marker are gone; its sidecar (and license) moved over
    assert sidecar_license(second.path) == LICENSE

    crawler, transport, _, _ = make(fetch_license=False)
    third = crawler.fetch(paper(), tmp_path)
    assert third.reused and third.format == "latex-archive" and transport.calls == []


def test_a_fallback_stays_marked_while_the_source_keeps_failing_transiently(tmp_path):
    crawler, transport, _, _ = make(retries=0, fetch_license=False)
    transport.route(SRC, Reply(503))
    transport.route(PDF, Reply(200, PDF_BYTES))
    crawler.fetch(fake_record("2401.00001v1"), tmp_path)
    marker = tmp_path / ".2401.00001v1.fallback"
    assert "latex" in marker.read_text()

    fetched = crawler.fetch(fake_record("2401.00001v1"), tmp_path)  # /src: 503 again
    assert fetched.reused and fetched.format == "pdf" and marker.exists()
    assert transport.urls(PDF) == [PDF + "2401.00001v1"]  # the PDF is not downloaded again

    crawler, transport, _, _ = make(retries=0, fetch_license=False)  # now a plain 404
    fetched = crawler.fetch(fake_record("2401.00001v1"), tmp_path)
    assert fetched.reused and fetched.format == "pdf" and not marker.exists()
    crawler.fetch(fake_record("2401.00001v1"), tmp_path)
    assert transport.urls(SRC) == [SRC + "2401.00001v1"]  # settled: no more /src requests


@pytest.mark.parametrize(
    "src",
    [
        src_reply(PDF_BYTES, "arXiv-1710.05832v1.pdf", "application/pdf"),  # PDF-only paper
        Reply(404),  # no source at all
    ],
)
def test_a_pdf_saved_for_a_permanent_reason_is_reused_without_requests(tmp_path, src):
    crawler, transport, _, _ = make(retries=0, fetch_license=False)
    transport.route(SRC, src)
    transport.route(PDF, Reply(200, PDF_BYTES))
    assert crawler.fetch(fake_record("1710.05832v1"), tmp_path).format == "pdf"
    assert not any(p.name.endswith(".fallback") for p in tmp_path.iterdir())

    crawler, transport, _, _ = make(retries=0, fetch_license=False)
    fetched = crawler.fetch(fake_record("1710.05832v1"), tmp_path)
    assert fetched.reused and transport.calls == []


# --------------------------------------------------------------------- pagination
def test_an_empty_page_inside_total_results_is_asked_for_again():
    """Old code: the empty first page ended the search with 0 records (exit status 0)."""
    crawler, transport, clock, logs = make(delay_s=3.0)
    ids = [f"2401.0000{i}v1" for i in range(1, 6)]
    empty = f"<feed {NS}><opensearch:totalResults>500</opensearch:totalResults></feed>"
    transport.route(API, Reply(200, empty.encode()), Reply(200, feed(*ids, total=500)))
    assert [r.key for r in crawler.search("cat:cs.CL", 5)] == ids
    urls = transport.urls(API)
    assert len(urls) == 2 and all("start=0&max_results=5" in u for u in urls)
    assert clock.sleeps == [3.0] and any("empty page" in line for line in logs)


def test_a_page_that_stays_empty_raises_or_keeps_partial_results():
    crawler, transport, _, _ = make()
    empty = f"<feed {NS}><opensearch:totalResults>500</opensearch:totalResults></feed>"
    transport.route(API, Reply(200, empty.encode()))
    with pytest.raises(SourceUnavailableError, match="stayed empty"):
        crawler.search("cat:cs.CL", 5)
    assert len(transport.calls) == 4  # the request and three more

    crawler, transport, _, logs = make()
    crawler.page_size = 2
    first = feed("2401.00001v1", "2401.00002v1", total=500)
    transport.route(API, Reply(200, first), Reply(200, empty.encode()))
    assert len(crawler.search("cat:cs.CL", 5)) == 2
    assert any("continuing with the first 2" in line for line in logs)

    crawler, transport, _, _ = make()  # no match at all is not an error
    none = f"<feed {NS}><opensearch:totalResults>0</opensearch:totalResults></feed>"
    transport.route(API, Reply(200, none.encode()))
    assert crawler.search("cat:cs.CL", 5) == [] and len(transport.calls) == 1


# ------------------------------------------------------------------ id-less entries
def test_entries_without_an_arxiv_id_are_skipped_but_counted():
    """Old code: SourceRecord(key='') took a slot of the limit and failed at fetch."""
    xml = (
        f"<feed {NS}><opensearch:totalResults>3</opensearch:totalResults>"
        "<entry><title>Error</title></entry>"
        "<entry><id>http://example.org/elsewhere</id></entry>"
        "<entry><id>http://arxiv.org/abs/</id></entry>"
        "<entry><id>http://arxiv.org/abs/2401.00001v1</id></entry></feed>"
    ).encode()
    records, total, entries = parse_feed(xml)
    assert [r.key for r in records] == ["2401.00001v1"] and (total, entries) == (3, 4)
    with pytest.raises(SourceUnavailableError, match="without an arXiv id"):
        parse_entry(ET.fromstring('<entry xmlns="http://www.w3.org/2005/Atom"/>'))


def test_pagination_advances_past_skipped_entries(tmp_path):
    crawler, transport, _, logs = make(fetch_license=False)
    crawler.page_size = 2
    page1 = (
        f"<feed {NS}><opensearch:totalResults>3</opensearch:totalResults>"
        "<entry><title>Error</title></entry>"
        "<entry><id>http://arxiv.org/abs/2401.00001v1</id></entry></feed>"
    ).encode()
    transport.route(API, Reply(200, page1), Reply(200, feed("2401.00002v1", total=3)))
    transport.route(SRC, src_reply(tar_gz(), "x.tar.gz"))
    report = crawl(crawler, tmp_path, "cat:cs.CL", 2)
    assert [f.record.key for f in report.fetched] == ["2401.00001v1", "2401.00002v1"]
    assert not report.failures
    assert "start=2&max_results=1" in transport.urls(API)[1]
    assert any("skipped 1 entry" in line for line in logs)


def test_a_failed_search_is_reported_not_raised(tmp_path):
    from docingest.domain.errors import SourceUnavailableError

    class DownCrawler:
        def search(self, query, limit):
            raise SourceUnavailableError("HTTP 503 from the API")

        def fetch(self, record, dest_dir):  # pragma: no cover - never reached
            raise AssertionError

    report = CrawlService(
        crawler=DownCrawler(), ingest=None, raw_dir=tmp_path, log=lambda _: None
    ).run("cat:cs.CL", 3)
    assert report.failures and report.failures[0][0].startswith("search")
    assert report.stopped and "search failed" in report.stopped


@pytest.mark.parametrize("query", ["", "   ", "ids:", "ids: , "])
def test_an_unusable_query_is_reported_not_raised(tmp_path, query):
    # Old: a plain ValueError escaped CrawlService.run (a traceback in the CLI).
    crawler, transport, _, _ = make()
    report = crawl(crawler, tmp_path, query, 3)
    assert report.stopped and "search failed" in report.stopped
    assert "InvalidQueryError" in report.failures[0][1] and "arXiv" in report.failures[0][1]
    assert transport.calls == []


def test_intermittent_406_from_the_api_is_retried():
    from docingest.adapters.sources.http import RETRYABLE_STATUS

    assert 406 in RETRYABLE_STATUS
