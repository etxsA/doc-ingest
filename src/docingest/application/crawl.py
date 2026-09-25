"""Use case: discover documents at a remote source, download them, ingest them."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from ..ports import FetchedSource, SourceCrawler, StoredDocument
from .ingest import SIDECAR_SUFFIX, IngestOptions, IngestService


@dataclass
class CrawlReport:
    fetched: list[FetchedSource] = field(default_factory=list)
    ingested: list[StoredDocument] = field(default_factory=list)
    failures: list[tuple[str, str]] = field(default_factory=list)  # (record key, error)


class CrawlService:
    def __init__(
        self,
        *,
        crawler: SourceCrawler,
        ingest: Callable[[], IngestService] | None,  # lazy: --no-ingest never builds OCR etc.
        raw_dir: Path,
        log: Callable[[str], None] = print,
    ):
        self.crawler = crawler
        self._ingest_factory = ingest
        self.raw_dir = raw_dir
        self.log = log

    def run(
        self,
        query: str,
        limit: int,
        *,
        ingest: bool = True,
        options: IngestOptions | None = None,
    ) -> CrawlReport:
        options = options or IngestOptions()
        report = CrawlReport()
        records = self.crawler.search(query, limit)
        self.log(f"{len(records)} record(s) for {query!r}")
        for record in records:
            try:
                fetched = self.crawler.fetch(record, self.raw_dir)
                # The fetched record may be enriched (e.g. license from OAI-PMH).
                meta = fetched.record.metadata
                # Sidecar metadata keeps a later plain `docingest ingest` fully informed.
                sidecar = fetched.path.with_name(fetched.path.name + SIDECAR_SUFFIX)
                sidecar.write_text(meta.model_dump_json(indent=2))
                report.fetched.append(fetched)
                self.log(f"  fetched {record.key} as {fetched.format} -> {fetched.path.name}")
                if ingest and self._ingest_factory is not None:
                    stored = self._ingest_factory().ingest(fetched.path, options, meta)
                    report.ingested.append(stored)
            except Exception as e:  # one bad record must not stop the crawl
                report.failures.append((record.key, f"{type(e).__name__}: {e}"))
                self.log(f"  failed {record.key}: {e}")
        return report
