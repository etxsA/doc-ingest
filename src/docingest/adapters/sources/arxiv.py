"""SourceCrawler adapter for arXiv: API search, LaTeX source (PDF fallback), license.

* Metadata comes from the Atom API (CC0). Search takes arXiv query syntax
  (``cat:cs.CL AND ti:retrieval``) or ``ids:1706.03762,math/0211159`` for an id list.
* Content comes from ``/src/<id><version>``, which serves whatever the authors
  submitted: a gzipped tar (``latex-archive``), one gzipped .tex (``latex``) or, for
  PDF-only submissions, the PDF. Headers are hints; the saved file is classified by its
  bytes. ``/pdf/<id>`` is the fallback (ADR 0003, ``[arxiv].prefer``).
* The license comes from OAI-PMH ``GetRecord``: e-prints may not be redistributed
  without permission, so each paper's license travels with its metadata.
* arXiv terms: every request (API, source, PDF, OAI) goes through one
  :class:`PoliteClient`, i.e. >= ``delay_s`` between request starts, one connection,
  and a User-Agent naming docingest (plus ``mailto:`` when ``contact`` is set).

Downloads are streamed to a hidden temp file in ``dest_dir`` and renamed into place,
so a file named ``<id><version>.<ext>`` is always complete and is reused as-is.
"""

from __future__ import annotations

import dataclasses
import gzip
import logging
import os
import re
import tarfile
import time
import xml.etree.ElementTree as ET
import zlib
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from ... import __version__
from ...config import ArxivConfig
from ...domain.errors import SourceUnavailableError
from ...domain.models import SourceMetadata
from ...ports import FetchedSource, SourceRecord
from .http import Clock, HttpResult, HttpStatusError, PoliteClient, Sleep, Transport

logger = logging.getLogger(__name__)

ATOM = "{http://www.w3.org/2005/Atom}"
ARXIV = "{http://arxiv.org/schemas/atom}"
OPENSEARCH = "{http://a9.com/-/spec/opensearch/1.1/}"
OAI = "{http://www.openarchives.org/OAI/2.0/}"

MAX_PAGE = 2000  # arXiv API: max_results per request
FORMATS = ("latex", "pdf")  # accepted values of [arxiv].prefer
# Saved suffix -> FetchedSource.format; order = reuse preference within one choice.
SUFFIXES: dict[str, tuple[tuple[str, str], ...]] = {
    "latex": (
        (".tar.gz", "latex-archive"),
        (".tex.gz", "latex"),
        (".tar", "latex-archive"),
        (".tex", "latex"),
    ),
    "pdf": ((".pdf", "pdf"),),
}
_VERSION = re.compile(r"^(?P<id>.+?)(?P<version>v\d+)?$")
_ID_PREFIX = re.compile(r"^(?:arxiv:|https?://(?:export\.)?arxiv\.org/(?:abs|pdf|src)/)", re.I)
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")
_TEX_HINT = re.compile(rb"\\(documentclass|documentstyle|begin\{document\}|section|input)")


# --------------------------------------------------------------------------- ids
def split_version(key: str) -> tuple[str, str | None]:
    """``"1706.03762v7"`` -> ``("1706.03762", "v7")``; old style ``math/0211159v1`` too."""
    m = _VERSION.match(key.strip())
    if not m:
        return key, None
    return m["id"], m["version"]


def normalize_id(raw: str) -> str:
    """Accept ``arXiv:1706.03762``, abs / pdf URLs and bare ids."""
    s = _ID_PREFIX.sub("", raw.strip())
    return s.removesuffix(".pdf").strip("/")


def safe_filename(key: str) -> str:
    """File stem for a record key: ``math/0211159v1`` -> ``math_0211159v1``; no traversal."""
    stem = _UNSAFE.sub("_", key.replace("/", "_")).strip("._")
    if not stem:
        raise SourceUnavailableError(f"record key {key!r} gives no usable file name")
    return stem


def user_agent(contact: str | None) -> str:
    mail = f"; mailto:{contact}" if contact else ""
    return f"docingest/{__version__} (+research ingestion{mail})"


def parse_ids(query: str) -> list[str] | None:
    """The ids of an ``ids:1706.03762,math/0211159`` query (deduplicated), else None."""
    q = query.strip()
    if q[:4].lower() != "ids:":
        return None
    ids = [normalize_id(x) for x in re.split(r"[,\s]+", q[4:]) if x.strip()]
    if not ids:
        raise ValueError(f"no arXiv ids in {query!r}")
    return list(dict.fromkeys(ids))


def id_params(ids: list[str]) -> dict[str, str]:
    # Same shape as a request arXiv answered (2026-09): empty search_query + id_list.
    return {"search_query": "", "id_list": ",".join(ids)}


def query_params(query: str) -> dict[str, str]:
    """arXiv query syntax -> ``search_query``; ``ids:a,b`` -> ``id_list``."""
    ids = parse_ids(query)
    if ids is not None:
        return id_params(ids)
    if not query.strip():
        raise ValueError("empty arXiv query")
    return {"search_query": query.strip()}


# ----------------------------------------------------------------------- parsing
def _text(el: ET.Element | None) -> str | None:
    """Element text with whitespace collapsed (titles and abstracts are hard-wrapped)."""
    if el is None or el.text is None:
        return None
    return " ".join(el.text.split()) or None


def parse_entry(entry: ET.Element) -> SourceRecord:
    entry_id = _text(entry.find(f"{ATOM}id")) or ""
    arxiv_id, version = split_version(entry_id.rsplit("/abs/", 1)[-1])
    published = _text(entry.find(f"{ATOM}published")) or _text(entry.find(f"{ATOM}updated"))
    primary = entry.find(f"{ARXIV}primary_category")
    terms = [primary.get("term")] if primary is not None else []
    terms += [c.get("term") for c in entry.findall(f"{ATOM}category")]
    url = next(
        (ln.get("href") for ln in entry.findall(f"{ATOM}link") if ln.get("rel") == "alternate"),
        None,
    )
    key = f"{arxiv_id}{version or ''}"
    metadata = SourceMetadata(
        title=_text(entry.find(f"{ATOM}title")),
        authors=[n for a in entry.findall(f"{ATOM}author") if (n := _text(a.find(f"{ATOM}name")))],
        year=int(published[:4]) if published and published[:4].isdigit() else None,
        abstract=_text(entry.find(f"{ATOM}summary")),
        doi=_text(entry.find(f"{ARXIV}doi")),
        arxiv_id=arxiv_id,
        version=version,
        url=url or f"https://arxiv.org/abs/{key}",
        categories=list(dict.fromkeys(t for t in terms if t)),  # primary first, deduplicated
    )
    return SourceRecord(key=key, metadata=metadata)


def parse_feed(xml: bytes) -> tuple[list[SourceRecord], int | None]:
    """Atom feed -> (records, totalResults). API errors come back as an entry."""
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as e:
        raise SourceUnavailableError(f"arXiv API returned malformed XML: {e}") from e
    records = []
    for entry in root.iter(f"{ATOM}entry"):
        if "/api/errors" in (_text(entry.find(f"{ATOM}id")) or ""):
            detail = _text(entry.find(f"{ATOM}summary")) or "unknown error"
            raise SourceUnavailableError(f"arXiv API error: {detail}")
        records.append(parse_entry(entry))
    total = _text(root.find(f"{OPENSEARCH}totalResults"))
    return records, int(total) if total and total.isdigit() else None


def parse_oai_license(xml: bytes) -> str | None:
    """License URL from an OAI-PMH GetRecord (``arXiv`` or ``arXivRaw`` format), if any."""
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as e:
        raise SourceUnavailableError(f"OAI-PMH returned malformed XML: {e}") from e
    error = root.find(f"{OAI}error")
    if error is not None:
        detail = _text(error) or ""
        raise SourceUnavailableError(f"OAI-PMH error {error.get('code')}: {detail}".rstrip(": "))
    metadata = root.find(f"{OAI}GetRecord/{OAI}record/{OAI}metadata")
    if metadata is None:
        return None
    for el in metadata.iter():
        if el.tag.rsplit("}", 1)[-1] == "license":
            return _text(el)
    return None


# ------------------------------------------------------------------ classifying
def _disposition_filename(value: str | None) -> str | None:
    if not value:
        return None
    m = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)"?', value, re.I)
    return m.group(1).strip() if m else None


def _hinted_formats(filename: str) -> set[str]:
    """Formats consistent with a served file name (``.gz`` may be a tar or a .tex)."""
    name = filename.lower()
    for suffixes, formats in (
        ((".tar.gz", ".tgz", ".tar"), {"latex-archive"}),
        ((".gz",), {"latex-archive", "latex"}),
        ((".pdf",), {"pdf"}),
        ((".tex",), {"latex"}),
    ):
        if name.endswith(suffixes):
            return formats
    return {"latex-archive", "latex", "pdf"}  # no usable suffix: nothing to contradict


def _looks_html(head: bytes) -> bool:
    return head.lstrip()[:15].lower().startswith((b"<!doctype html", b"<html"))


def _is_tar(path: Path, mode: Literal["r:gz", "r:"]) -> bool:
    try:
        with tarfile.open(path, mode) as tf:
            return tf.next() is not None
    except (tarfile.TarError, OSError, EOFError, zlib.error):
        return False


def _classify_gzip(path: Path) -> tuple[str, str] | None:
    try:
        with gzip.open(path, "rb") as g:
            inner = g.read(1024)
    except (OSError, EOFError, zlib.error):
        return None  # corrupt gzip
    if _is_tar(path, "r:gz"):
        return "latex-archive", ".tar.gz"
    if inner.startswith((b"%PDF-", b"%!PS")) or _looks_html(inner):
        return None  # gzipped PDF / PostScript-only submission: not a LaTeX source
    return "latex", ".tex.gz"


def classify(path: Path, filename: str | None = None) -> tuple[str, str] | None:
    """(format, suffix) of a downloaded body, from its bytes; None if unusable.

    Magic bytes win over the Content-Disposition ``filename`` (only a tie-breaker for an
    uncompressed .tex). A gzip holding a tar is an archive; holding anything else that is
    not a PDF / PostScript / HTML page, it is a single .tex.
    """
    with path.open("rb") as f:
        head = f.read(1024)
    if head.startswith(b"%PDF-"):
        return "pdf", ".pdf"
    if head[:2] == b"\x1f\x8b":
        return _classify_gzip(path)
    if (len(head) > 262 and head[257:262] == b"ustar") or _is_tar(path, "r:"):
        return "latex-archive", ".tar"
    if not _looks_html(head) and (
        (filename or "").lower().endswith(".tex") or _TEX_HINT.search(head)
    ):
        return "latex", ".tex"
    return None


def _etag(headers: Mapping[str, str]) -> str | None:
    value = headers.get("etag")
    return value.removeprefix("W/").strip('"') if value else None


# ------------------------------------------------------------------------ adapter
@dataclass(frozen=True)
class ArxivFetch(FetchedSource):
    """A FetchedSource plus download provenance.

    ``etag`` is the server's opaque tag (``sha256:...`` for most arXiv sources; it is not
    the hash of the served bytes, which is the manifest's ``doc_id``).
    """

    url: str | None = None
    etag: str | None = None
    size_bytes: int = 0
    seconds: float = 0.0
    reused: bool = False  # the file was already in dest_dir; nothing downloaded


class ArxivCrawler:
    def __init__(
        self,
        cfg: ArxivConfig,
        *,
        transport: Transport | None = None,
        clock: Clock = time.monotonic,
        sleep: Sleep = time.sleep,
        page_size: int = MAX_PAGE,
        log: Callable[[str], None] | None = None,
    ):
        unknown = [p for p in cfg.prefer if p not in FORMATS]
        if unknown or not cfg.prefer:
            raise ValueError(f"[arxiv] prefer must list some of {FORMATS}, got {cfg.prefer}")
        self.cfg = cfg
        self.page_size = max(1, min(page_size, MAX_PAGE))
        self.log = log or logger.info
        self.warn = log or logger.warning  # visible without logging configuration
        self.http = PoliteClient(
            user_agent=user_agent(cfg.contact),
            delay_s=cfg.delay_s,
            timeout_s=cfg.timeout_s,
            retries=cfg.retries,
            transport=transport,
            clock=clock,
            sleep=sleep,
        )

    # ---------------------------------------------------------------- search
    def search(self, query: str, limit: int) -> list[SourceRecord]:
        if limit < 1:
            return []
        ids = parse_ids(query)
        records = self._search_ids(ids[:limit]) if ids else self._search_query(query, limit)
        return list({r.key: r for r in records}.values())[:limit]

    def _page(
        self, params: dict[str, str], start: int, n: int
    ) -> tuple[list[SourceRecord], int | None]:
        res = self.http.get(
            self.cfg.api_url, {**params, "start": start, "max_results": n}, ok=(200, 400)
        )
        page, total = parse_feed(res.body)  # a 400 carries an error entry -> raises
        if res.status != 200:
            raise SourceUnavailableError(f"arXiv API HTTP {res.status} for {params}")
        return page, total

    def _search_query(self, query: str, limit: int) -> list[SourceRecord]:
        params = query_params(query)
        records: list[SourceRecord] = []
        start = 0
        while len(records) < limit:
            n = min(limit - len(records), self.page_size)
            page, total = self._page(params, start, n)
            records += page
            start += len(page)
            if not page or (total is not None and start >= total):
                break
            if total is None and len(page) < n:
                break
        return records

    def _search_ids(self, ids: list[str]) -> list[SourceRecord]:
        """Batches of ``page_size`` ids; a batch arXiv refuses (406) goes one id at a time.

        Observed 2026-09: ``id_list=1706.03762,math/0211159`` got an empty 406 from the
        CDN (encoded or not, with or without ``search_query``) while ``id_list=2409.13740``
        worked; whether the comma or the old-style id triggers it is not established.
        """
        records: list[SourceRecord] = []
        refused: list[str] = []
        for i in range(0, len(ids), self.page_size):
            batch = ids[i : i + self.page_size]
            try:
                records += self._page(id_params(batch), 0, len(batch))[0]
                continue
            except HttpStatusError as e:
                if e.status != 406 or len(batch) == 1:
                    raise
            self.warn(f"arXiv API refused {len(batch)} ids in one request (406); one by one")
            for one in batch:
                try:
                    records += self._page(id_params([one]), 0, 1)[0]
                except HttpStatusError as e:
                    if e.status != 406:
                        raise
                    refused.append(one)
                    self.warn(f"{one}: the arXiv API refused this id (HTTP 406); skipped")
        if refused and not records:
            raise SourceUnavailableError(f"arXiv API refused every id: {', '.join(refused)}")
        return records

    # ----------------------------------------------------------------- fetch
    def fetch(self, record: SourceRecord, dest_dir: Path) -> ArxivFetch:
        dest_dir.mkdir(parents=True, exist_ok=True)
        stem = safe_filename(record.key)
        fetched = self._existing(record, dest_dir, stem) or self._download(record, dest_dir, stem)
        return dataclasses.replace(fetched, record=self._with_license(record))

    def _existing(self, record: SourceRecord, dest_dir: Path, stem: str) -> ArxivFetch | None:
        for choice in self.cfg.prefer:
            for suffix, fmt in SUFFIXES[choice]:
                path = dest_dir / f"{stem}{suffix}"
                if path.is_file() and path.stat().st_size > 0:
                    self.log(f"{record.key}: reusing {path.name}")
                    size = path.stat().st_size
                    return ArxivFetch(path, record, fmt, size_bytes=size, reused=True)
        return None

    def _download(self, record: SourceRecord, dest_dir: Path, stem: str) -> ArxivFetch:
        reasons: list[str] = []
        tmp = dest_dir / f".{stem}.{os.getpid()}.part"  # hidden: never picked up as input
        try:
            for choice in self.cfg.prefer:
                src = self.cfg.src_url or self.cfg.eprint_url
                url = (src if choice == "latex" else self.cfg.pdf_url).format(id=record.key)
                try:
                    res = self.http.download(url, tmp)
                except SourceUnavailableError as e:
                    reasons.append(f"{choice}: {e}")
                    continue
                kind = self._accept(choice, tmp, res, reasons)
                if kind is None:
                    continue
                fmt, suffix = kind
                path = dest_dir / f"{stem}{suffix}"
                os.replace(tmp, path)
                self.log(f"{record.key}: {fmt}, {res.size} B in {res.seconds:.1f}s -> {path.name}")
                return ArxivFetch(
                    path,
                    record,
                    fmt,
                    url=res.url,
                    etag=_etag(res.headers),
                    size_bytes=res.size,
                    seconds=res.seconds,
                )
        finally:
            tmp.unlink(missing_ok=True)
        raise SourceUnavailableError(f"{record.key}: nothing downloadable ({'; '.join(reasons)})")

    def _accept(
        self, choice: str, tmp: Path, res: HttpResult, reasons: list[str]
    ) -> tuple[str, str] | None:
        hint = _disposition_filename(res.headers.get("content-disposition"))
        kind = classify(tmp, hint)
        ctype = res.headers.get("content-type", "?")
        if kind is None:
            reasons.append(f"{choice}: {res.url} is neither a source nor a PDF ({ctype})")
            return None
        if hint and kind[0] not in _hinted_formats(hint):
            self.log(f"{res.url}: served as {hint!r} but the bytes say {kind[0]}")
        if kind[0] == "pdf" and "pdf" not in self.cfg.prefer:
            reasons.append(f"{choice}: PDF-only submission and 'pdf' is not in [arxiv].prefer")
            return None
        if choice == "pdf" and kind[0] != "pdf":
            reasons.append(f"pdf: {res.url} did not return a PDF ({ctype})")
            return None
        return kind

    # --------------------------------------------------------------- license
    def _with_license(self, record: SourceRecord) -> SourceRecord:
        meta = record.metadata
        if not self.cfg.fetch_license or meta.license:
            return record
        arxiv_id = meta.arxiv_id or split_version(record.key)[0]
        params = {
            "verb": "GetRecord",
            "identifier": f"oai:arXiv.org:{arxiv_id}",
            "metadataPrefix": "arXiv",
        }
        try:
            res = self.http.get(self.cfg.oai_url, params)
            license_url = parse_oai_license(res.body)
        except SourceUnavailableError as e:  # the paper is still usable; say why it's unknown
            self.warn(f"{record.key}: license unknown ({e})")
            return record
        self.log(f"{record.key}: license {license_url or 'not stated in the OAI-PMH record'}")
        if license_url is None:
            return record
        meta = meta.model_copy(update={"license": license_url})
        return dataclasses.replace(record, metadata=meta)
