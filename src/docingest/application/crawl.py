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
        ingest: IngestService | None,
        raw_dir: Path,
        log: Callable[[str], None] = print,
    ):
        self.crawler = crawler
        self.ingest_service = ingest
        self.raw_dir = raw_dir
        self.log = log

    def run(
        self, query: str, limit: int, *, ingest: bool = True, options: IngestOptions = IngestOptions()
    ) -> CrawlReport:
        report = CrawlReport()
        records = self.crawler.search(query, limit)
        self.log(f"{len(records)} record(s) for {query!r}")
        for record in records:
            try:
                fetched = self.crawler.fetch(record, self.raw_dir)
                # Sidecar metadata keeps a later plain `docingest ingest` fully informed.
                sidecar = fetched.path.with_name(fetched.path.name + SIDECAR_SUFFIX)
                sidecar.write_text(record.metadata.model_dump_json(indent=2))
                report.fetched.append(fetched)
                self.log(f"  fetched {record.key} as {fetched.format} -> {fetched.path.name}")
                if ingest and self.ingest_service is not None:
                    stored = self.ingest_service.ingest(fetched.path, options, record.metadata)
                    report.ingested.append(stored)
            except Exception as e:  # one bad record must not stop the crawl
                report.failures.append((record.key, f"{type(e).__name__}: {e}"))
                self.log(f"  failed {record.key}: {e}")
        return report
