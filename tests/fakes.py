"""In-memory fakes for every port: fast, deterministic application tests.

They are real implementations of the port contracts (the contract tests run
against them too), not mocks: no call assertions, just behaviour.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

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
    Conversion,
    FetchedSource,
    OcrResult,
    Segment,
    SourceRecord,
    StoredDocument,
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

    KINDS = {
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
    def __init__(self, n_frames: int = 1):
        self.n_frames = n_frames

    def frames(self, path: Path) -> list[Image.Image]:
        return [Image.new("RGB", (10, 10), "white") for _ in range(self.n_frames)]


class FakeConverter:
    def __init__(self, segments: list[Segment], method: PageMethod = PageMethod.LATEX, **kw):
        self.segments = segments
        self.method = method
        self.kw = kw
        self.fingerprint = f"fake-converter {method.value}"

    def convert(self, path: Path) -> Conversion:
        return Conversion(segments=self.segments, method=self.method, engine="fake", **self.kw)


class InMemoryStore:
    """Reference implementation of the DocumentStore contract."""

    def __init__(self):
        self.canonical: dict[str, tuple[DocumentManifest, str]] = {}
        self.variants: dict[tuple[str, str, int | None], tuple[DocumentManifest, str]] = {}

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

    def save(self, manifest, markdown):
        if manifest.complete and not manifest.ocr_all:
            self.canonical[manifest.doc_id] = (manifest, markdown)
            return StoredDocument(manifest, f"mem://{manifest.doc_id}", canonical=True)
        key = (manifest.doc_id, manifest.config_hash, manifest.max_pages)
        self.variants[key] = (manifest, markdown)
        return StoredDocument(manifest, f"mem://{key}", canonical=False)

    def markdown(self, doc):
        m = doc.manifest
        if doc.canonical:
            return self.canonical[m.doc_id][1]
        return self.variants[(m.doc_id, m.config_hash, m.max_pages)][1]

    def corpus(self):
        docs = [StoredDocument(m, f"mem://{i}", True) for i, (m, _) in self.canonical.items()]
        warnings = []
        for (doc_id, _, _), (m, _) in self.variants.items():
            if doc_id not in self.canonical:
                docs.append(StoredDocument(m, f"mem://{doc_id}", False))
                warnings.append(f"{m.source_name}: only a partial run exists")
        return docs, warnings


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

    async def ask(self, question, documents, warn: Callable[[str], None]) -> str:
        self.seen = [d.manifest.source_name for d, _ in documents]
        return f"answer to {question!r} from {len(documents)} docs"
