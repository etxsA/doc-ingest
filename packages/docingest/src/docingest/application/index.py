"""Use case: keep a chunk index in step with the normalized corpus.

``IndexService.build`` chunks every stored document exactly like ``docingest ask`` does,
embeds the chunks of the documents that are new or changed and leaves the others alone, so
a second run over an unchanged corpus makes no embedding request. A document is unchanged
when the SHA-256 of its page texts is the key the index recorded for it. Chunk settings
and the embedder are part of the index's identity (its fingerprint), not of the key.
"""

from __future__ import annotations

import hashlib
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace

from ..domain.chunking import Chunk, chunk_pages
from ..domain.errors import IndexMismatchError
from ..domain.text import split_pages
from ..ports import ChunkIndex, DocumentStore, Embedder, IndexStats, StoredDocument

Log = Callable[[str], None]
BATCH_CHUNKS = 512  # chunks embedded per call: enough for the client to keep the server busy
_REFERENCE_TITLE = re.compile(r"reference|bibliograph", re.IGNORECASE)


def content_key(pages: Mapping[int, str]) -> str:
    """The key of a document in the index: a SHA-256 of its page numbers and page texts."""
    digest = hashlib.sha256()
    for number, text in pages.items():
        digest.update(f"{number}:{len(text)}:".encode())
        digest.update(text.encode())
    return digest.hexdigest()


@dataclass(frozen=True)
class IndexStatus:
    stats: IndexStats
    index_fingerprint: str
    embedder_fingerprint: str  # of the index; compare with the configured embedder
    embedder_matches: bool
    corpus_documents: int
    new: int  # in the corpus, not indexed
    changed: int  # indexed under another key (the text changed)
    unchanged: int
    stale: list[str] = field(default_factory=list)  # indexed, no longer in the corpus
    chunks_to_embed: int = 0  # what a build would send to the embedder
    warnings: list[str] = field(default_factory=list)


@dataclass
class BuildReport:
    added: int = 0
    updated: int = 0
    unchanged: int = 0
    pruned: list[str] = field(default_factory=list)
    chunks_embedded: int = 0
    committed: bool = False
    seconds: float = 0.0
    warnings: list[str] = field(default_factory=list)


@dataclass
class _Pending:
    doc: StoredDocument
    key: str
    chunks: list[Chunk]


class IndexService:
    def __init__(
        self,
        *,
        store: DocumentStore,
        embedder: Embedder,
        index: ChunkIndex,
        chunk_chars: int,
        overlap: int,
        batch_chunks: int = BATCH_CHUNKS,
        log: Log = print,
    ):
        self.store = store
        self.embedder = embedder
        self.index = index
        self.chunk_chars = chunk_chars
        self.overlap = overlap
        self.batch_chunks = batch_chunks
        self.log = log

    def build(self, *, prune: bool = True) -> BuildReport:
        """Embed the new and changed documents, drop the ones that left the corpus (unless
        ``prune`` is false) and commit. Safe to interrupt and run again: what was embedded
        before stays."""
        self._check_embedder()
        started = time.perf_counter()
        documents, warnings = self.store.corpus()
        if not documents:
            raise RuntimeError("no ingested documents; run `docingest ingest` first")
        report = BuildReport(warnings=list(warnings))
        indexed = self.index.keys()
        batch: list[_Pending] = []
        in_batch = 0
        todo = 0
        for doc in documents:
            pages = split_pages(self.store.markdown(doc))
            key = content_key(pages)
            doc_id = doc.manifest.doc_id
            if indexed.get(doc_id) == key:
                report.unchanged += 1
                continue
            if doc_id in indexed:
                report.updated += 1
            else:
                report.added += 1
            chunks = self._chunks(doc, pages)
            batch.append(_Pending(doc, key, chunks))
            in_batch += len(chunks)
            todo += len(chunks)
            if in_batch >= self.batch_chunks:
                report.chunks_embedded += self._flush(batch)
                self.log(f"embedded {report.chunks_embedded} chunks")
                batch, in_batch = [], 0
        report.chunks_embedded += self._flush(batch)
        if prune:
            current = {d.manifest.doc_id for d in documents}
            report.pruned = sorted(set(indexed) - current)
            for doc_id in report.pruned:
                self.index.remove(doc_id)
        stats = self.index.stats()
        changed = report.added or report.updated or report.pruned
        if changed or stats.pending or not stats.committed_at:
            self.index.commit()
            report.committed = True
        report.seconds = time.perf_counter() - started
        return report

    def status(self) -> IndexStatus:
        documents, warnings = self.store.corpus()
        indexed = self.index.keys()
        new = changed = unchanged = to_embed = 0
        for doc in documents:
            pages = split_pages(self.store.markdown(doc))
            doc_id = doc.manifest.doc_id
            if indexed.get(doc_id) == content_key(pages):
                unchanged += 1
                continue
            if doc_id in indexed:
                changed += 1
            else:
                new += 1
            to_embed += len(self._chunks(doc, pages))
        current = {d.manifest.doc_id for d in documents}
        return IndexStatus(
            stats=self.index.stats(),
            index_fingerprint=self.index.fingerprint,
            embedder_fingerprint=self.index.embedder_fingerprint,
            embedder_matches=self.index.embedder_fingerprint == self.embedder.fingerprint,
            corpus_documents=len(documents),
            new=new,
            changed=changed,
            unchanged=unchanged,
            stale=sorted(set(indexed) - current),
            chunks_to_embed=to_embed,
            warnings=list(warnings),
        )

    def remove(self, doc_id: str) -> str:
        """Forget one document and commit. ``doc_id`` may be a unique prefix (at least 8
        characters). Returns the full id."""
        indexed = self.index.keys()
        if doc_id in indexed:
            full = doc_id
        else:
            matches = [d for d in indexed if len(doc_id) >= 8 and d.startswith(doc_id)]
            if len(matches) != 1:
                reason = "is ambiguous" if matches else "is not in the index"
                raise ValueError(f"document {doc_id!r} {reason}")
            full = matches[0]
        self.index.remove(full)
        self.index.commit()
        return full

    def _check_embedder(self) -> None:
        held, configured = self.index.embedder_fingerprint, self.embedder.fingerprint
        if held != configured:
            raise IndexMismatchError(
                f"the index holds vectors of embedder {held!r} but the configured one is "
                f"{configured!r}; use another [index] dir or restore the [embedder] settings"
            )

    def _chunks(self, doc: StoredDocument, pages: Mapping[int, str]) -> list[Chunk]:
        titles = {p.index + 1: p.title or "" for p in doc.manifest.pages}
        chunks = chunk_pages(
            doc.manifest.doc_id, pages, chunk_chars=self.chunk_chars, overlap=self.overlap
        )
        return [
            replace(c, is_reference=True)
            if any(
                _REFERENCE_TITLE.search(titles.get(n, ""))
                for n in range(c.first_page, c.last_page + 1)
            )
            else c
            for c in chunks
        ]

    def _flush(self, batch: list[_Pending]) -> int:
        """Embed the chunks of several documents in one call and store each document."""
        texts = [c.text for p in batch for c in p.chunks]
        vectors = self.embedder.embed_documents(texts) if texts else []
        if len(vectors) != len(texts):
            raise RuntimeError(
                f"the embedder returned {len(vectors)} vectors for {len(texts)} texts"
            )
        at = 0
        for pending in batch:
            n = len(pending.chunks)
            self.index.upsert(
                pending.doc.manifest.doc_id, pending.key, pending.chunks, vectors[at : at + n]
            )
            at += n
        return len(texts)
