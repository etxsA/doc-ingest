"""Use case: discover documents at a remote source, download them, ingest them."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from ..domain.errors import DocingestError, RateLimitedError
from ..domain.models import SourceMetadata
from ..ports import FetchedSource, SourceCrawler, StoredDocument
from .ingest import SIDECAR_SUFFIX, IngestOptions, IngestService, read_sidecar


@dataclass
class CrawlReport:
    fetched: list[FetchedSource] = field(default_factory=list)
    ingested: list[StoredDocument] = field(default_factory=list)
    failures: list[tuple[str, str]] = field(default_factory=list)  # (record key, error)
    stopped: str | None = None  # why the crawl ended before its last record, if it did


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
        try:
            records = self.crawler.search(query, limit)
        except DocingestError as e:  # e.g. the search API is down: a report, not a traceback
            report.failures.append((f"search {query!r}", f"{type(e).__name__}: {e}"))
            report.stopped = f"search failed: {e}"
            return report
        self.log(f"{len(records)} record(s) for {query!r}")
        for i, record in enumerate(records):
            try:
                fetched = self.crawler.fetch(record, self.raw_dir)
                meta = self._metadata(fetched)
                # Sidecar metadata keeps a later plain `docingest ingest` fully informed.
                sidecar = fetched.path.with_name(fetched.path.name + SIDECAR_SUFFIX)
                sidecar.write_text(meta.model_dump_json(indent=2))
                report.fetched.append(fetched)
                self.log(f"  fetched {record.key} as {fetched.format} -> {fetched.path.name}")
                if ingest and self._ingest_factory is not None:
                    stored = self._ingest_factory().ingest(fetched.path, options, meta)
                    report.ingested.append(stored)
            except RateLimitedError as e:  # every later request would hit the same wall
                report.failures.append((record.key, f"{type(e).__name__}: {e}"))
                rest = records[i + 1 :]
                report.stopped = f"the source asked us to back off: {e}"
                skipped = "not attempted: the crawl stopped, the source asked us to back off"
                report.failures += [(r.key, skipped) for r in rest]
                self.log(f"  stopped at {record.key}: {e}; {len(rest)} record(s) not attempted")
                break
            except Exception as e:  # one bad record must not stop the crawl
                report.failures.append((record.key, f"{type(e).__name__}: {e}"))
                self.log(f"  failed {record.key}: {e}")
        return report

    def _metadata(self, fetched: FetchedSource) -> SourceMetadata:
        """The fetched record's metadata, keeping a license recorded by an earlier crawl.

        A license lookup can fail transiently (the crawler then reports none); that must
        not erase the license the existing sidecar already holds for the same file.
        """
        # The fetched record may be enriched (e.g. license from OAI-PMH).
        meta = fetched.record.metadata
        if meta.license is not None:
            return meta
        try:
            earlier = read_sidecar(fetched.path)
        except (OSError, ValueError):  # unreadable / invalid sidecar: rewritten below
            return meta
        if earlier is None or earlier.license is None:
            return meta
        self.log(f"  {fetched.record.key}: keeping the license recorded earlier")
        return meta.model_copy(update={"license": earlier.license})
