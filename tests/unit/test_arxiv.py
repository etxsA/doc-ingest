"""ArxivCrawler against a fake transport and a fake clock: no network, no real sleeping.

Atom / OAI-PMH bodies are trimmed recordings in tests/fixtures/arxiv/; source archives
are built on the fly (tar.gz, single gzipped .tex, PDF) to exercise format detection.
"""

from __future__ import annotations

import gzip
import io
import itertools
import tarfile
import urllib.error
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fakes import record as fake_record

from docingest import __version__
from docingest.adapters.sources.arxiv import (
    ArxivCrawler,
    classify,
    normalize_id,
    parse_feed,
    parse_oai_license,
    query_params,
    safe_filename,
    split_version,
)
from docingest.adapters.sources.http import (
    LocalWriteError,
    PoliteClient,
    RawResponse,
    _decoded,
    retry_after_seconds,
)
from docingest.application.crawl import CrawlService
from docingest.config import ArxivConfig
from docingest.domain.errors import SourceUnavailableError
from docingest.ports import SourceCrawler

FIXTURES = Path(__file__).parents[1] / "fixtures" / "arxiv"
API = "https://export.arxiv.org/api/query"
SRC = "https://arxiv.org/src/"
PDF = "https://arxiv.org/pdf/"
OAI = "https://oaipmh.arxiv.org/oai"
LICENSE = "http://creativecommons.org/licenses/by-sa/4.0/"


# ------------------------------------------------------------------ test doubles
class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


@dataclass
class Reply:
    status: int = 200
    body: bytes = b""
    headers: dict[str, str] = field(default_factory=dict)


ReplySpec = Reply | BaseException | Callable[[str], Reply]


class FakeTransport:
    """Routes by URL prefix to queued replies (the last one repeats); records every call."""

    def __init__(self, clock: FakeClock):
        self.clock = clock
        self.routes: list[tuple[str, list[ReplySpec]]] = []
        self.calls: list[tuple[float, str, dict[str, str]]] = []

    def route(self, prefix: str, *replies: ReplySpec) -> FakeTransport:
        self.routes.append((prefix, list(replies)))
        return self

    def urls(self, prefix: str = "") -> list[str]:
        return [url for _, url, _ in self.calls if url.startswith(prefix)]

    def __call__(self, url: str, headers, timeout: float) -> RawResponse:
        self.calls.append((self.clock(), url, dict(headers)))
        for prefix, replies in self.routes:
            if url.startswith(prefix):
                reply = replies.pop(0) if len(replies) > 1 else replies[0]
                if isinstance(reply, BaseException):
                    raise reply
                if callable(reply):
                    reply = reply(url)
                hdrs = {k.lower(): v for k, v in reply.headers.items()}
                return RawResponse(reply.status, url, hdrs, io.BytesIO(reply.body))
        return RawResponse(404, url, {}, io.BytesIO(b"<html>Not Found</html>"))


def make(**cfg) -> tuple[ArxivCrawler, FakeTransport, FakeClock, list[str]]:
    clock = FakeClock()
    transport = FakeTransport(clock)
    logs: list[str] = []
    crawler = ArxivCrawler(
        ArxivConfig(**cfg), transport=transport, clock=clock, sleep=clock.sleep, log=logs.append
    )
    return crawler, transport, clock, logs


# ---------------------------------------------------------------- payload helpers
def fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def feed(*ids: str, total: int | None = None) -> bytes:
    entries = "".join(
        f"<entry><id>http://arxiv.org/abs/{i}</id><title>Paper {i}</title>"
        f"<published>2024-01-02T00:00:00Z</published></entry>"
        for i in ids
    )
    total_el = f"<opensearch:totalResults>{total}</opensearch:totalResults>" if total else ""
    return (
        '<feed xmlns="http://www.w3.org/2005/Atom" '
        f'xmlns:opensearch="http://a9.com/-/spec/opensearch/1.1/">{total_el}{entries}</feed>'
    ).encode()


def tar_gz() -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        data = b"\\documentclass{article}\\begin{document}Hi\\end{document}"
        info = tarfile.TarInfo("main.tex")
        info.size = len(data)
        tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def tex_gz() -> bytes:  # like arXiv's single-file sources: gzip with an FNAME header
    buf = io.BytesIO()
    with gzip.GzipFile(filename="garikN.tex", fileobj=buf, mode="wb") as g:
        g.write(b"\\documentclass[12pt]{article}\n\\begin{document}x\\end{document}\n")
    return buf.getvalue()


PDF_BYTES = b"%PDF-1.5\n%\xe2\xe3\xcf\xd3\n1 0 obj<<>>endobj\n%%EOF\n"


def src_reply(body: bytes, filename: str, ctype: str = "application/gzip") -> Reply:
    return Reply(
        200,
        body,
        {
            "Content-Type": ctype,
            "Content-Length": str(len(body)),
            "Content-Disposition": f'attachment; filename="{filename}"',
            "ETag": '"sha256:57ddc809b42efb1163aad44aeac311df975f527a4b085731ad858948e89e137a"',
        },
    )


def paper():
    """The 2409.13740v2 record, parsed from the recorded API response."""
    return parse_feed(fixture("api_query.xml"))[0][0]


# ------------------------------------------------------------------------ parsing
def test_parse_feed_reads_the_recorded_entry():
    records, total = parse_feed(fixture("api_query.xml"))
    assert total == 2 and len(records) == 2
    r = records[0]
    m = r.metadata
    assert r.key == "2409.13740v2"
    assert (m.arxiv_id, m.version, m.year) == ("2409.13740", "v2", 2024)
    assert m.title == "Language agents achieve superhuman synthesis of scientific knowledge"
    assert m.authors == ["Michael D. Skarlinski", "Sam Cox", "Andrew D. White"]
    assert m.abstract.startswith("Language models are known to hallucinate")
    assert m.categories == ["cs.CL", "cs.AI", "cs.IR", "physics.soc-ph"]
    assert m.url == "https://arxiv.org/abs/2409.13740v2"
    assert m.doi is None and m.license is None  # the license comes from OAI-PMH at fetch time


def test_parse_feed_collapses_whitespace_and_reads_old_style_ids():
    r = parse_feed(fixture("api_query.xml"))[0][1]
    m = r.metadata
    assert r.key == "hep-th/9901001v3"
    assert (m.arxiv_id, m.version, m.year) == ("hep-th/9901001", "v3", 1999)
    assert m.title == "A Hard-Wrapped Title With Extra Spaces"
    assert m.abstract == "First line of the abstract wraps here, and tabs too."
    assert m.authors == ["Ann Example"]
    assert m.doi == "10.1000/example.123"
    assert m.categories == ["hep-th", "math.DG"]  # primary first, no duplicates


def test_api_errors_and_malformed_xml_raise_domain_errors():
    with pytest.raises(SourceUnavailableError, match="incorrect id format for 1234"):
        parse_feed(fixture("api_error.xml"))
    with pytest.raises(SourceUnavailableError, match="malformed"):
        parse_feed(b"<feed><entry>")


@pytest.mark.parametrize(
    ("key", "expected"),
    [
        ("1706.03762v7", ("1706.03762", "v7")),
        ("1706.03762", ("1706.03762", None)),
        ("0704.0001v12", ("0704.0001", "v12")),
        ("math/0211159v1", ("math/0211159", "v1")),
        ("solv-int/9901001", ("solv-int/9901001", None)),
    ],
)
def test_split_version(key, expected):
    assert split_version(key) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("arXiv:1706.03762", "1706.03762"),
        (" https://arxiv.org/abs/1706.03762v7 ", "1706.03762v7"),
        ("https://arxiv.org/pdf/1706.03762v7.pdf", "1706.03762v7"),
        ("math/0211159", "math/0211159"),
    ],
)
def test_normalize_id(raw, expected):
    assert normalize_id(raw) == expected


def test_query_params():
    assert query_params("cat:cs.CL AND ti:retrieval") == {
        "search_query": "cat:cs.CL AND ti:retrieval"
    }
    assert query_params("IDS: 1706.03762, math/0211159 arXiv:1706.03762") == {
        "search_query": "",
        "id_list": "1706.03762,math/0211159",
    }
    for bad in ("", "  ", "ids:", "ids: , "):
        with pytest.raises(ValueError, match=r"arXiv"):
            query_params(bad)


@pytest.mark.parametrize(
    ("key", "stem"),
    [
        ("1706.03762v7", "1706.03762v7"),
        ("math/0211159v1", "math_0211159v1"),
        ("../../etc/passwd", "etc_passwd"),
        ("a b;c|d", "a_b_c_d"),
    ],
)
def test_safe_filename(key, stem):
    assert safe_filename(key) == stem


def test_safe_filename_rejects_keys_without_usable_characters():
    with pytest.raises(SourceUnavailableError):
        safe_filename("../")


def test_retry_after_parsing():
    now = datetime(2026, 9, 25, 12, 0, 0, tzinfo=UTC)
    assert retry_after_seconds("120") == 120.0
    assert retry_after_seconds("Fri, 25 Sep 2026 12:00:30 GMT", now) == 30.0
    assert retry_after_seconds("Fri, 25 Sep 2026 11:00:00 GMT", now) == 0.0
    assert retry_after_seconds("soon") is None and retry_after_seconds(None) is None


# ------------------------------------------------------------------------- search
def test_crawler_satisfies_the_port_and_validates_prefer():
    assert isinstance(ArxivCrawler(ArxivConfig()), SourceCrawler)
    with pytest.raises(ValueError, match="prefer"):
        ArxivCrawler(ArxivConfig(prefer=["html"]))
    with pytest.raises(ValueError, match="prefer"):
        ArxivCrawler(ArxivConfig(prefer=[]))


def test_search_by_ids_sends_id_list_and_identifies_itself():
    crawler, transport, _, _ = make(contact="lab@example.org")
    transport.route(API, Reply(200, fixture("api_query.xml")))
    records = crawler.search("ids:2409.13740,hep-th/9901001", 5)
    assert [r.key for r in records] == ["2409.13740v2", "hep-th/9901001v3"]
    [(_, url, headers)] = transport.calls
    assert "search_query=&id_list=2409.13740%2Chep-th%2F9901001&start=0&max_results=2" in url
    assert headers["User-Agent"] == (
        f"docingest/{__version__} (+research ingestion; mailto:lab@example.org)"
    )
    assert headers["Accept"] == "*/*"


def test_user_agent_omits_mailto_without_contact():
    crawler, transport, _, _ = make()
    transport.route(API, Reply(200, feed("2401.00001v1")))
    crawler.search("ids:2401.00001", 1)
    assert transport.calls[0][2]["User-Agent"] == f"docingest/{__version__} (+research ingestion)"


def test_search_paginates_until_limit_and_stops_at_total():
    crawler, transport, _, _ = make()
    crawler.page_size = 2
    transport.route(
        API,
        Reply(200, feed("2401.00001v1", "2401.00002v1", total=5)),
        Reply(200, feed("2401.00003v2", total=5)),
    )
    records = crawler.search("cat:cs.CL", 3)
    assert [r.key for r in records] == ["2401.00001v1", "2401.00002v1", "2401.00003v2"]
    urls = transport.urls(API)
    assert "start=0&max_results=2" in urls[0] and "start=2&max_results=1" in urls[1]

    crawler, transport, _, _ = make()
    transport.route(API, Reply(200, feed("2401.00001v1", total=1)))
    assert len(crawler.search("cat:cs.CL", 50)) == 1
    assert len(transport.calls) == 1  # totalResults reached: no second page
    assert crawler.search("cat:cs.CL", 0) == []


def test_search_surfaces_api_errors_sent_with_http_400():
    crawler, transport, _, _ = make()
    transport.route(API, Reply(400, fixture("api_error.xml")))
    with pytest.raises(SourceUnavailableError, match="incorrect id format"):
        crawler.search("ids:1234", 1)


def test_a_refused_id_batch_is_retried_one_id_at_a_time():
    """arXiv's CDN answered 406 to id_list=1706.03762,math/0211159 (2026-09)."""

    def api(url: str) -> Reply:
        if "%2C" in url or "math%2F" in url:
            return Reply(406)
        return Reply(200, feed("1706.03762v7"))

    crawler, transport, _, logs = make()
    transport.route(API, api)
    records = crawler.search("ids:1706.03762,math/0211159", 5)
    assert [r.key for r in records] == ["1706.03762v7"]
    assert len(transport.calls) == 3  # the batch, then one request per id
    assert any("math/0211159" in line and "406" in line for line in logs)

    crawler, transport, _, _ = make()
    transport.route(API, Reply(406))
    with pytest.raises(SourceUnavailableError, match="refused every id"):
        crawler.search("ids:math/0211159,math/0211160", 5)


# -------------------------------------------------------------- politeness / retry
def test_every_request_shares_one_rate_limiter(tmp_path):
    crawler, transport, clock, _ = make(delay_s=3.0)
    transport.route(API, Reply(200, fixture("api_query.xml")))
    transport.route(SRC, src_reply(tar_gz(), "arXiv-2409.13740v2.tar.gz"))
    transport.route(OAI, Reply(200, fixture("oai_2409.13740.xml")))
    record = crawler.search("ids:2409.13740", 1)[0]
    crawler.fetch(record, tmp_path)
    starts = [t for t, _, _ in transport.calls]
    assert len(starts) == 3  # API, source, OAI license
    assert all(b - a >= 3.0 for a, b in itertools.pairwise(starts))
    assert clock.sleeps == [3.0, 3.0]  # nothing waits before the first request


def test_retries_503_with_exponential_backoff_then_succeeds():
    crawler, transport, clock, _ = make(delay_s=3.0, retries=3)
    transport.route(API, Reply(503), Reply(503), Reply(200, feed("2401.00001v1")))
    assert [r.key for r in crawler.search("ids:2401.00001", 1)] == ["2401.00001v1"]
    assert len(transport.calls) == 3 and crawler.http.requests == 3
    assert clock.sleeps == [3.0, 6.0]  # backoff >= delay_s, so the limiter adds nothing


def test_honors_retry_after_and_fails_fast_when_it_is_too_long():
    crawler, transport, clock, _ = make(delay_s=3.0)
    transport.route(API, Reply(429, headers={"Retry-After": "7"}), Reply(200, feed("2401.1v1")))
    crawler.search("cat:cs.CL", 1)
    (t0, _, _), (t1, _, _) = transport.calls
    assert t1 - t0 >= 7 and clock.sleeps == [7.0]

    crawler, transport, clock, _ = make()
    transport.route(API, Reply(503, headers={"Retry-After": "3600"}))
    with pytest.raises(SourceUnavailableError, match="retry after 3600s"):
        crawler.search("cat:cs.CL", 1)
    assert len(transport.calls) == 1 and clock.sleeps == []


def test_network_errors_and_truncated_bodies_are_retried_then_give_up():
    crawler, transport, _, _ = make(retries=2)
    body = feed("2401.00001v1")
    truncated = Reply(200, body[:10], {"Content-Length": str(len(body))})
    transport.route(API, urllib.error.URLError("reset"), truncated, Reply(200, body))
    assert len(crawler.search("cat:cs.CL", 1)) == 1 and len(transport.calls) == 3

    crawler, transport, clock, _ = make(retries=3, delay_s=3.0)
    transport.route(API, urllib.error.URLError("down"))
    with pytest.raises(SourceUnavailableError, match="gave up after 4 attempt"):
        crawler.search("cat:cs.CL", 1)
    assert clock.sleeps == [3.0, 6.0, 12.0]


def _client(transport: FakeTransport, clock: FakeClock, **kw) -> PoliteClient:
    return PoliteClient(
        user_agent="t",
        delay_s=0,
        timeout_s=1,
        retries=3,
        transport=transport,
        clock=clock,
        sleep=clock.sleep,
        **kw,
    )


def test_client_refuses_oversized_bodies_without_retrying(tmp_path):
    clock = FakeClock()
    transport = FakeTransport(clock).route("http://x/", Reply(200, b"x" * 100))
    with pytest.raises(SourceUnavailableError, match="larger than"):
        _client(transport, clock, max_bytes=10).download("http://x/big", tmp_path / "big")
    assert len(transport.calls) == 1


def test_a_cut_gzip_encoded_stream_is_retried():
    clock = FakeClock()
    body = gzip.compress(b"<feed/>" * 500)
    calls: list[str] = []

    def transport(url, headers, timeout):
        calls.append(url)
        data = body[:20] if len(calls) == 1 else body  # first attempt: connection cut
        return _decoded(200, url, {"content-encoding": "gzip"}, io.BytesIO(data))

    client = PoliteClient(
        user_agent="t",
        delay_s=0,
        timeout_s=1,
        retries=1,
        transport=transport,
        clock=clock,
        sleep=clock.sleep,
    )
    assert client.get("http://x/feed").body == b"<feed/>" * 500 and len(calls) == 2


def test_local_write_errors_are_not_retried(tmp_path):
    clock = FakeClock()
    transport = FakeTransport(clock).route("http://x/", Reply(200, b"data"))
    with pytest.raises(LocalWriteError, match="cannot write"):
        _client(transport, clock).download("http://x/f", tmp_path / "missing-dir" / "f")
    assert len(transport.calls) == 1 and clock.sleeps == []


# -------------------------------------------------------------------------- fetch
def test_fetch_saves_a_tar_gz_source_with_etag_and_license(tmp_path):
    crawler, transport, _, logs = make()
    body = tar_gz()
    transport.route(SRC, src_reply(body, "arXiv-2409.13740v2.tar.gz"))
    transport.route(OAI, Reply(200, fixture("oai_2409.13740.xml")))
    record = paper()
    fetched = crawler.fetch(record, tmp_path / "raw")
    assert fetched.format == "latex-archive"
    assert fetched.path == tmp_path / "raw" / "2409.13740v2.tar.gz"
    assert fetched.path.read_bytes() == body and fetched.size_bytes == len(body)
    assert fetched.etag and fetched.etag.startswith("sha256:57ddc809")
    assert not fetched.reused and fetched.url == SRC + "2409.13740v2"
    assert fetched.record.metadata.license == LICENSE
    assert record.metadata.license is None  # the input record is not mutated
    assert "identifier=oai%3AarXiv.org%3A2409.13740&metadataPrefix=arXiv" in transport.urls(OAI)[0]
    assert any(LICENSE in line for line in logs)
    assert [p.name for p in (tmp_path / "raw").iterdir()] == ["2409.13740v2.tar.gz"]


def test_fetch_saves_a_single_gzipped_tex_for_old_style_ids(tmp_path):
    crawler, transport, _, logs = make(fetch_license=False)
    transport.route(SRC, src_reply(tex_gz(), "arXiv-math0211159v1.gz"))
    fetched = crawler.fetch(fake_record("math/0211159v1"), tmp_path)
    assert fetched.format == "latex"
    assert fetched.path.name == "math_0211159v1.tex.gz"
    assert transport.urls() == [SRC + "math/0211159v1"]
    assert not any("bytes say" in line for line in logs)  # ".gz" is consistent with a .tex


def test_a_pdf_only_submission_is_taken_from_the_source_endpoint(tmp_path):
    crawler, transport, _, _ = make(fetch_license=False)
    transport.route(SRC, src_reply(PDF_BYTES, "arXiv-1710.05832v1.pdf", "application/pdf"))
    fetched = crawler.fetch(fake_record("1710.05832v1"), tmp_path)
    assert (fetched.format, fetched.path.name) == ("pdf", "1710.05832v1.pdf")
    assert transport.urls(PDF) == []  # no second request for the same bytes


def test_magic_bytes_beat_a_lying_content_disposition(tmp_path):
    crawler, transport, _, logs = make(fetch_license=False)
    transport.route(SRC, src_reply(tar_gz(), "arXiv-2401.00001v1.pdf", "application/pdf"))
    fetched = crawler.fetch(fake_record("2401.00001v1"), tmp_path)
    assert fetched.format == "latex-archive" and fetched.path.suffixes == [
        ".00001v1",
        ".tar",
        ".gz",
    ]
    assert any("bytes say latex-archive" in line for line in logs)


def test_html_instead_of_a_source_falls_back_to_the_pdf(tmp_path):
    crawler, transport, _, _ = make(fetch_license=False)
    transport.route(
        SRC, Reply(200, b"<!DOCTYPE html><html>no source</html>", {"Content-Type": "text/html"})
    )
    transport.route(PDF, Reply(200, PDF_BYTES, {"Content-Type": "application/pdf"}))
    fetched = crawler.fetch(fake_record("2401.00001v1"), tmp_path)
    assert fetched.format == "pdf" and fetched.path.name == "2401.00001v1.pdf"
    assert transport.urls() == [SRC + "2401.00001v1", PDF + "2401.00001v1"]


def test_prefer_pdf_skips_the_source(tmp_path):
    crawler, transport, _, _ = make(fetch_license=False, prefer=["pdf"])
    transport.route(PDF, Reply(200, PDF_BYTES))
    assert crawler.fetch(fake_record("2401.00001v1"), tmp_path).format == "pdf"
    assert transport.urls() == [PDF + "2401.00001v1"]


def test_missing_source_raises_source_unavailable_and_leaves_no_partial_files(tmp_path):
    crawler, transport, _, _ = make(fetch_license=False)  # src and pdf both 404
    with pytest.raises(SourceUnavailableError, match=r"2401\.00001v1: nothing downloadable"):
        crawler.fetch(fake_record("2401.00001v1"), tmp_path)
    assert list(tmp_path.iterdir()) == []

    crawler, transport, _, _ = make(fetch_license=False, prefer=["latex"])
    transport.route(SRC, src_reply(PDF_BYTES, "arXiv-1710.05832v1.pdf", "application/pdf"))
    with pytest.raises(SourceUnavailableError, match="PDF-only submission"):
        crawler.fetch(fake_record("1710.05832v1"), tmp_path)
    assert transport.urls(PDF) == [] and list(tmp_path.iterdir()) == []


def test_an_existing_file_is_reused_without_downloading(tmp_path):
    crawler, transport, _, logs = make(fetch_license=False)
    existing = tmp_path / "2409.13740v2.tar.gz"
    existing.write_bytes(tar_gz())
    fetched = crawler.fetch(paper(), tmp_path)
    assert fetched.path == existing and fetched.reused and fetched.format == "latex-archive"
    assert transport.calls == [] and any("reusing" in line for line in logs)

    crawler, transport, _, _ = make()  # the license lookup still happens
    transport.route(OAI, Reply(200, fixture("oai_2409.13740.xml")))
    fetched = crawler.fetch(paper(), tmp_path)
    assert fetched.reused and fetched.record.metadata.license == LICENSE
    assert transport.urls(SRC) == []


def test_fetch_stays_inside_dest_dir_for_hostile_keys(tmp_path):
    crawler, transport, _, _ = make(fetch_license=False)
    transport.route(SRC, src_reply(tar_gz(), "x.tar.gz"))
    dest = tmp_path / "raw"
    fetched = crawler.fetch(fake_record("../../evil"), dest)
    assert fetched.path.parent == dest and fetched.path.name == "evil.tar.gz"


# ------------------------------------------------------------------------ license
def test_license_parsing_from_oai_pmh():
    assert parse_oai_license(fixture("oai_2409.13740.xml")) == LICENSE
    arxiv_format = fixture("oai_2409.13740.xml").replace(
        b"http://arxiv.org/OAI/arXivRaw/", b"http://arxiv.org/OAI/arXiv/"
    )
    assert parse_oai_license(arxiv_format) == LICENSE
    no_license = fixture("oai_2409.13740.xml").replace(
        b"<license>http://creativecommons.org/licenses/by-sa/4.0/</license>", b""
    )
    assert parse_oai_license(no_license) is None
    error = (
        b'<OAI-PMH xmlns="http://www.openarchives.org/OAI/2.0/">'
        b'<error code="idDoesNotExist">No matching identifier</error></OAI-PMH>'
    )
    with pytest.raises(SourceUnavailableError, match="idDoesNotExist"):
        parse_oai_license(error)


def test_a_failed_license_lookup_does_not_fail_the_fetch(tmp_path):
    crawler, transport, _, logs = make(retries=0)
    transport.route(SRC, src_reply(tar_gz(), "arXiv-2409.13740v2.tar.gz"))
    transport.route(OAI, Reply(500))
    fetched = crawler.fetch(paper(), tmp_path)
    assert fetched.record.metadata.license is None
    assert any("license unknown" in line for line in logs)


# ---------------------------------------------------------------------- detection
def test_classify_by_bytes(tmp_path):
    def cls(data: bytes, hint: str | None = None):
        p = tmp_path / "body"
        p.write_bytes(data)
        return classify(p, hint)

    assert cls(tar_gz()) == ("latex-archive", ".tar.gz")
    assert cls(tex_gz()) == ("latex", ".tex.gz")
    assert cls(PDF_BYTES) == ("pdf", ".pdf")
    assert cls(gzip.decompress(tar_gz())) == ("latex-archive", ".tar")
    assert cls(b"\\documentclass{article}") == ("latex", ".tex")
    assert cls(b"plain words", "arXiv-x.tex") == ("latex", ".tex")
    assert cls(gzip.compress(PDF_BYTES)) is None  # gzipped PDF is not a LaTeX source
    assert cls(gzip.compress(b"%!PS-Adobe-2.0")) is None  # PostScript-only submission
    assert cls(b"<!doctype html><html></html>") is None
    assert cls(b"\x1f\x8b\x08garbage") is None


# ------------------------------------------------------------------- use case glue
def test_crawl_service_writes_the_sidecar_next_to_the_download(tmp_path):
    crawler, transport, _, _ = make(fetch_license=False)
    transport.route(API, Reply(200, fixture("api_query.xml")))
    transport.route(SRC, src_reply(tar_gz(), "arXiv-2409.13740v2.tar.gz"))
    report = CrawlService(crawler=crawler, ingest=None, raw_dir=tmp_path, log=lambda _: None).run(
        "ids:2409.13740", 1, ingest=False
    )
    assert [f.format for f in report.fetched] == ["latex-archive"] and not report.failures
    assert (tmp_path / "2409.13740v2.tar.gz.meta.json").exists()


def test_bootstrap_builds_the_arxiv_crawler(cfg):
    from docingest.bootstrap import build

    assert isinstance(build("crawler", cfg), ArxivCrawler)
