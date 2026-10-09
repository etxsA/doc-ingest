"""In-memory fakes for every port: fast, deterministic application tests.

They are real implementations of the port contracts (the contract tests run
against them too), not mocks: no call assertions, just behaviour.
"""

from __future__ import annotations

import math
import re
import zlib
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar

from PIL import Image

from docingest.domain.errors import DocumentOpenError, UnsupportedInputError
from docingest.domain.models import (
    DocumentManifest,
    ModelRef,
    PageMethod,
    PageSignals,
    SourceKind,
    SourceMetadata,
)
from docingest.ports import (
    Chunk,
    Conversion,
    FetchedSource,
    Hit,
    IndexStats,
    OcrResult,
    Segment,
    SourceRecord,
    StoredDocument,
    Vector,
)


class FakeOcr:
    """Transcribes every image to a fixed text; counts calls."""

    def __init__(self, text: str = "ocr text", model: str = "fake/ocr"):
        self.text = text
        self.model = ModelRef(repo_id=model, revision="0" * 40)
        self.dpi = 72
        self.fingerprint = f"fake-ocr {model} {text!r}"
        self.calls = 0

    def transcribe(self, image: Image.Image) -> OcrResult:
        self.calls += 1
        return OcrResult(text=self.text, seconds=0.01, gen_tokens=3, finish_reason="stop")


@dataclass
class FakePage:
    signals_: PageSignals
    text: str

    def signals(self) -> tuple[PageSignals, str]:
        return self.signals_, self.text

    def render(self, dpi: int) -> Image.Image:
        return Image.new("RGB", (10, 10), "white")


@dataclass
class FakePdfDocument:
    pages: list[FakePage]
    title: str | None = None
    closed: bool = False

    def __len__(self) -> int:
        return len(self.pages)

    def page(self, index: int) -> FakePage:
        return self.pages[index]

    def close(self) -> None:
        self.closed = True


def digital(text: str) -> FakePage:
    n = len("".join(text.split()))
    return FakePage(PageSignals(n_chars=n, alpha_ratio=0.9), text)


def scanned() -> FakePage:
    return FakePage(PageSignals(n_chars=0, n_images=1, image_coverage=1.0), "")


class FakePdfReader:
    fingerprint = "fake-pdf 1"

    def __init__(self, docs: dict[str, list[FakePage]] | None = None):
        self.docs = docs or {}
        self.opened: list[FakePdfDocument] = []

    def open(self, path: Path) -> FakePdfDocument:
        if path.name not in self.docs:
            raise DocumentOpenError(f"cannot open {path.name}")
        doc = FakePdfDocument(self.docs[path.name])
        self.opened.append(doc)
        return doc


class FakeDetector:
    """Kind from the suffix only; the real magic-byte detector has its own tests."""

    KINDS: ClassVar[dict[str, tuple[SourceKind, str]]] = {
        ".pdf": (SourceKind.PDF, "application/pdf"),
        ".png": (SourceKind.IMAGE, "image/png"),
        ".docx": (SourceKind.OFFICE, "application/docx"),
        ".tex": (SourceKind.LATEX, "application/x-tex"),
        ".md": (SourceKind.TEXT, "text/markdown"),
    }

    def detect(self, path: Path) -> tuple[SourceKind, str]:
        try:
            return self.KINDS[path.suffix]
        except KeyError:
            raise UnsupportedInputError(path.name) from None


class FakeImages:
    def __init__(self, n_frames: int = 1, fingerprint: str = "fake-images 1"):
        self.n_frames = n_frames
        self.fingerprint = fingerprint

    def frames(self, path: Path) -> list[Image.Image]:
        return [Image.new("RGB", (10, 10), "white") for _ in range(self.n_frames)]


class FakeConverter:
    """``**kw`` goes into every ``Conversion`` (title, metadata, degraded, ...); counts calls."""

    def __init__(self, segments: list[Segment], method: PageMethod = PageMethod.LATEX, **kw):
        self.segments = segments
        self.method = method
        self.kw = kw
        self.fingerprint = f"fake-converter {method.value}"
        self.calls = 0

    def convert(self, path: Path) -> Conversion:
        self.calls += 1
        return Conversion(segments=self.segments, method=self.method, engine="fake", **self.kw)


class InMemoryStore:
    """Reference implementation of the DocumentStore contract."""

    def __init__(self):
        self.canonical: dict[str, tuple[DocumentManifest, str]] = {}
        self.variants: dict[tuple[str, str, int | None], tuple[DocumentManifest, str]] = {}
        # Environment-caused fallbacks: kept for the corpus, never served by lookup().
        self.degraded: dict[tuple[str, str, int | None], tuple[DocumentManifest, str]] = {}

    def lookup(self, doc_id, config_hash, *, max_pages, ocr_all):
        if doc_id in self.canonical:
            m, _ = self.canonical[doc_id]
            if m.config_hash == config_hash and m.complete and m.ocr_all == ocr_all:
                return StoredDocument(m, f"mem://{doc_id}", canonical=True)
        if max_pages is not None or ocr_all:
            key = (doc_id, config_hash, max_pages)
            if key in self.variants:
                return StoredDocument(self.variants[key][0], f"mem://{key}", canonical=False)
        return None

    def save(self, manifest, markdown, *, degraded=False):
        key = (manifest.doc_id, manifest.config_hash, manifest.max_pages)
        if degraded:
            self.degraded[key] = (manifest, markdown)
            return StoredDocument(manifest, f"mem://degraded/{key}", canonical=False)
        if manifest.complete and not manifest.ocr_all:
            self.canonical[manifest.doc_id] = (manifest, markdown)
            return StoredDocument(manifest, f"mem://{manifest.doc_id}", canonical=True)
        self.variants[key] = (manifest, markdown)
        return StoredDocument(manifest, f"mem://{key}", canonical=False)

    def markdown(self, doc):
        m = doc.manifest
        if doc.canonical:
            return self.canonical[m.doc_id][1]
        key = (m.doc_id, m.config_hash, m.max_pages)
        if doc.location.startswith("mem://degraded/"):
            return self.degraded[key][1]
        return self.variants[key][1]

    def corpus(self):
        docs = [StoredDocument(m, f"mem://{i}", True) for i, (m, _) in self.canonical.items()]
        best: dict[str, StoredDocument] = {}  # most complete non-canonical run per document
        for store, prefix in ((self.variants, "mem://"), (self.degraded, "mem://degraded/")):
            for key, (m, _) in store.items():
                seen = best.get(m.doc_id)
                if m.doc_id not in self.canonical and (
                    seen is None or m.n_pages > seen.manifest.n_pages
                ):
                    best[m.doc_id] = StoredDocument(m, f"{prefix}{key}", False)
        warnings = [
            f"{d.manifest.source_name}: only a degraded fallback conversion exists"
            if d.location.startswith("mem://degraded/")
            else f"{d.manifest.source_name}: only a partial run exists"
            for d in best.values()
        ]
        return docs + list(best.values()), warnings


@dataclass
class FakeCrawler:
    records: list[SourceRecord]
    payload: bytes = b"\\documentclass{article}\\begin{document}Hi\\end{document}"
    fail_keys: set[str] = field(default_factory=set)

    def search(self, query: str, limit: int) -> list[SourceRecord]:
        return self.records[:limit]

    def fetch(self, record: SourceRecord, dest_dir: Path) -> FetchedSource:
        if record.key in self.fail_keys:
            raise DocumentOpenError(f"no source for {record.key}")
        dest_dir.mkdir(parents=True, exist_ok=True)
        path = dest_dir / f"{record.key}.tex"
        path.write_bytes(self.payload)
        return FetchedSource(path=path, record=record, format="latex")


def record(key: str, title: str = "A paper") -> SourceRecord:
    return SourceRecord(key=key, metadata=SourceMetadata(title=title, arxiv_id=key, year=2024))


class FakeQA:
    def __init__(self):
        self.seen: list[str] = []
        self.contexts: list[Chunk] | None = None

    async def ask(
        self, question, documents, warn: Callable[[str], None], contexts: list[Chunk] | None = None
    ) -> str:
        self.seen = [d.manifest.source_name for d, _ in documents]
        self.contexts = contexts
        return f"answer to {question!r} from {len(documents)} docs"


def words(text: str) -> list[str]:
    return re.findall(r"\w+", text.lower())


class FakeEmbedder:
    """Hashed bag of words in ``dims`` dimensions: texts sharing words get close vectors."""

    def __init__(
        self, dims: int = 16, fingerprint: str = "fake-embedder 1", query_instruction: str = ""
    ):
        self.dims = dims
        self.fingerprint = fingerprint
        self.query_instruction = query_instruction
        self.calls = 0

    def _vector(self, text: str) -> Vector:
        v = [0.0] * self.dims
        for w in words(text):
            v[zlib.crc32(w.encode()) % self.dims] += 1.0
        return v

    def embed_documents(self, texts: Sequence[str]) -> list[Vector]:
        self.calls += 1
        return [self._vector(t) for t in texts]

    def embed_query(self, text: str) -> Vector:
        return self._vector(text)


class FakeIndex:
    """Reference implementation of the ChunkIndex contract: exact cosine search in memory.

    Changes are staged until ``commit()``: ``search`` sees only committed documents, so a
    service that forgets to commit fails its tests. ``keys()`` sees every change.
    """

    def __init__(
        self, fingerprint: str = "fake-index 1", embedder_fingerprint: str = "fake-embedder 1"
    ):
        self.fingerprint = fingerprint
        self.embedder_fingerprint = embedder_fingerprint
        self.committed_at: str | None = None
        self._committed: dict[str, tuple[str, list[Chunk], list[Vector]]] = {}
        self._staged: dict[str, tuple[str, list[Chunk], list[Vector]]] = {}

    def keys(self) -> dict[str, str]:
        return {doc_id: key for doc_id, (key, _, _) in self._staged.items()}

    def upsert(
        self, doc_id: str, key: str, chunks: Sequence[Chunk], vectors: Sequence[Vector]
    ) -> None:
        if len(chunks) != len(vectors):
            raise ValueError("one vector per chunk")
        self._staged[doc_id] = (key, list(chunks), list(vectors))

    def remove(self, doc_id: str) -> None:
        self._staged.pop(doc_id, None)

    def commit(self) -> None:
        self._committed = dict(self._staged)
        self.committed_at = "2026-01-01T00:00:00+00:00"

    def stats(self) -> IndexStats:
        return IndexStats(
            documents=len(self._staged),
            chunks=sum(len(c) for _, c, _ in self._staged.values()),
            searchable_documents=len(self._committed),
            searchable_chunks=sum(len(c) for _, c, _ in self._committed.values()),
            committed_at=self.committed_at,
            pending=self._staged != self._committed,
        )

    def search(self, question: str, vector: Vector, k: int) -> list[Hit]:
        def cosine(a: Vector, b: Vector) -> float:
            norm = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(x * x for x in b))
            return sum(x * y for x, y in zip(a, b, strict=True)) / norm if norm else 0.0

        hits = [
            Hit(chunk, cosine(vector, vec))
            for _, chunks, vectors in self._committed.values()
            for chunk, vec in zip(chunks, vectors, strict=True)
        ]
        return sorted(hits, key=lambda h: -h.score)[:k]  # stable: ties keep insertion order


class FakeReranker:
    """Scores a chunk by the share of the question's words it contains."""

    def rerank(self, question: str, chunks: Sequence[Chunk]) -> list[float]:
        wanted = set(words(question))
        return [len(wanted & set(words(c.text))) / len(wanted) if wanted else 0.0 for c in chunks]
