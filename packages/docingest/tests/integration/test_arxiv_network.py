"""ArxivCrawler over real HTTP.

* A local stand-in server (always runs): the real urllib transport, redirects
  (/e-print/ -> /src/), gzip Content-Encoding, HTTPError -> retry with Retry-After.
* Live arXiv (``@pytest.mark.network``, opt in with DOCINGEST_NETWORK_TESTS=1): at most
  3 requests (search, source, OAI license), spaced by the 3 s the terms ask for.
"""

from __future__ import annotations

import gzip
import http.server
import io
import tarfile
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest
from fakes import record

from docingest.adapters.sources.arxiv import ArxivCrawler
from docingest.adapters.sources.http import UrllibTransport
from docingest.config import ArxivConfig
from docingest.domain.errors import SourceUnavailableError

FIXTURES = Path(__file__).parents[1] / "fixtures" / "arxiv"
LICENSE = "http://creativecommons.org/licenses/by-sa/4.0/"


def _tar_gz() -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        data = b"\\documentclass{article}\\begin{document}Hi\\end{document}"
        info = tarfile.TarInfo("main.tex")
        info.size = len(data)
        tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


class _Arxiv(http.server.BaseHTTPRequestHandler):
    seen: list[tuple[str, dict[str, str]]]
    busy: list[int]  # statuses to answer /api/query with before succeeding

    def do_GET(self) -> None:
        self.seen.append((self.path, dict(self.headers)))
        if self.path.startswith("/api/query"):
            if self.busy:
                self._send(self.busy.pop(0), b"busy", {"Retry-After": "0"})
                return
            body = gzip.compress((FIXTURES / "api_query.xml").read_bytes())
            self._send(200, body, {"Content-Encoding": "gzip"}, "application/atom+xml")
        elif self.path.startswith("/e-print/"):
            self._send(301, b"", {"Location": self.path.replace("/e-print/", "/src/")})
        elif self.path.startswith("/src/2409.13740v2"):
            headers = {
                "Content-Disposition": 'attachment; filename="arXiv-2409.13740v2.tar.gz"',
                "ETag": '"sha256:28c5077df664cd2e2821e2269c8e15b5"',
            }
            self._send(200, _tar_gz(), headers, "application/gzip")
        elif self.path.startswith("/oai"):
            self._send(200, (FIXTURES / "oai_2409.13740.xml").read_bytes(), {}, "text/xml")
        else:
            self._send(404, b"<html>not found</html>", {}, "text/html")

    def _send(self, status: int, body: bytes, headers: dict[str, str], ctype="text/plain"):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in headers.items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args) -> None:  # keep pytest output clean
        pass


@pytest.fixture
def local_arxiv() -> Iterator[tuple[str, type[_Arxiv]]]:
    handler = type("Handler", (_Arxiv,), {"seen": [], "busy": []})
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, args=(0.05,), daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", handler
    finally:
        server.shutdown()
        server.server_close()


def _local_crawler(base: str, **cfg) -> ArxivCrawler:
    config = ArxivConfig(
        api_url=f"{base}/api/query",
        src_url="",  # use eprint_url, which redirects to /src/ like arxiv.org does
        eprint_url=f"{base}/e-print/{{id}}",
        pdf_url=f"{base}/pdf/{{id}}",
        oai_url=f"{base}/oai",
        delay_s=0.0,
        **cfg,
    )
    return ArxivCrawler(config, transport=UrllibTransport(proxies={}))


def test_urllib_transport_end_to_end_against_a_local_arxiv(local_arxiv, tmp_path):
    base, handler = local_arxiv
    crawler = _local_crawler(base, contact="lab@example.org")
    [record, _] = crawler.search("ids:2409.13740,hep-th/9901001", 2)
    fetched = crawler.fetch(record, tmp_path)

    assert record.metadata.title.startswith("Language agents")  # gzip body was decoded
    assert fetched.format == "latex-archive" and fetched.path.name == "2409.13740v2.tar.gz"
    assert fetched.url == f"{base}/src/2409.13740v2"  # redirect followed
    assert fetched.etag == "sha256:28c5077df664cd2e2821e2269c8e15b5"
    assert tarfile.is_tarfile(fetched.path)
    assert fetched.record.metadata.license == LICENSE
    paths = [p for p, _ in handler.seen]
    assert paths[1:3] == ["/e-print/2409.13740v2", "/src/2409.13740v2"]
    assert len(paths) == 4 and paths[3].startswith("/oai?verb=GetRecord")
    ua = handler.seen[0][1]["User-Agent"]
    assert ua.startswith("docingest/") and "mailto:lab@example.org" in ua


def test_urllib_transport_retries_http_errors_with_retry_after(local_arxiv):
    base, handler = local_arxiv
    handler.busy.extend([503, 429])
    crawler = _local_crawler(base, retries=2)
    assert len(crawler.search("cat:cs.CL", 2)) == 2
    assert crawler.http.requests == 3


def test_a_404_source_and_pdf_is_source_unavailable(local_arxiv, tmp_path):
    crawler = _local_crawler(local_arxiv[0], retries=0, fetch_license=False)
    with pytest.raises(SourceUnavailableError, match="HTTP 404"):
        crawler.fetch(record("2401.99999v1"), tmp_path)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.network
def test_live_arxiv_search_and_fetch(tmp_path):
    crawler = ArxivCrawler(ArxivConfig(retries=0))  # <= 3 requests, 3 s apart
    [record] = crawler.search("ids:1706.03762", 1)
    meta = record.metadata
    assert meta.arxiv_id == "1706.03762" and meta.version and record.key.startswith("1706.03762v")
    assert meta.title == "Attention Is All You Need" and meta.year == 2017
    assert "cs.CL" in meta.categories and len(meta.authors) >= 8

    fetched = crawler.fetch(record, tmp_path)
    assert fetched.format == "latex-archive"
    assert fetched.path == tmp_path / f"{record.key}.tar.gz" and tarfile.is_tarfile(fetched.path)
    assert crawler.http.requests <= 4
