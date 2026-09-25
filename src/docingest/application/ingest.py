"""Use case: ingest one document -> canonical Markdown + manifest (via the store port)."""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from ..domain.errors import UnsupportedInputError
from ..domain.models import (
    DocumentManifest,
    ModelRef,
    PageMethod,
    PageRecord,
    SourceKind,
    SourceMetadata,
)
from ..domain.routing import RoutingPolicy, decide
from ..domain.text import clean_text_layer, render_markdown, text_vocabulary
from ..ports import (
    DocumentConverter,
    DocumentStore,
    ImageSource,
    OcrEngine,
    PdfReader,
    StoredDocument,
    TypeDetector,
)

PIPELINE_VERSION = "0.3.0"
SIDECAR_SUFFIX = ".meta.json"  # optional SourceMetadata next to an input file

Log = Callable[[str], None]


@dataclass(frozen=True)
class IngestOptions:
    force: bool = False  # ignore cached results
    ocr_all: bool = False  # OCR every PDF page, even with a good text layer
    max_pages: int | None = None  # only the first N pages / segments

    def __post_init__(self) -> None:
        if self.max_pages is not None and self.max_pages < 1:
            raise ValueError("max_pages must be >= 1")


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def read_sidecar(path: Path) -> SourceMetadata | None:
    sidecar = path.with_name(path.name + SIDECAR_SUFFIX)
    if not sidecar.exists():
        return None
    return SourceMetadata.model_validate_json(sidecar.read_text())


class IngestService:
    def __init__(
        self,
        *,
        detector: TypeDetector,
        pdf: PdfReader,
        ocr: OcrEngine,
        images: ImageSource,
        converters: Mapping[SourceKind, DocumentConverter],
        store: DocumentStore,
        policy: RoutingPolicy,
        log: Log = print,
    ):
        self.detector = detector
        self.pdf = pdf
        self.ocr = ocr
        self.images = images
        self.converters = converters
        self.store = store
        self.policy = policy
        self.log = log

    # ----------------------------------------------------------------- public
    def config_hash(self, kind: SourceKind, *, ocr_all: bool = False) -> str:
        """Cache key: everything that can change this kind's output, nothing else."""
        match kind:
            case SourceKind.PDF:
                parts = [self.policy.model_dump_json(), self.pdf.fingerprint, self.ocr.fingerprint]
            case SourceKind.IMAGE:
                parts = [self.ocr.fingerprint]
            case _:
                parts = [self._converter(kind).fingerprint]
        key = json.dumps([PIPELINE_VERSION, kind.value, ocr_all, *parts])
        return hashlib.sha256(key.encode()).hexdigest()[:12]

    def ingest(
        self,
        path: Path,
        options: IngestOptions = IngestOptions(),
        metadata: SourceMetadata | None = None,
    ) -> StoredDocument:
        path = path.resolve()
        kind, mime = self.detector.detect(path)
        doc_id = sha256_file(path)
        ocr_all = options.ocr_all and kind == SourceKind.PDF
        config_hash = self.config_hash(kind, ocr_all=ocr_all)
        metadata = metadata or read_sidecar(path)

        if not options.force:
            cached = self.store.lookup(
                doc_id, config_hash, max_pages=options.max_pages, ocr_all=ocr_all
            )
            if cached:
                self.log(f"[cache] {path.name} -> {cached.location} (config {config_hash})")
                return cached

        t0 = time.perf_counter()
        title = None
        match kind:
            case SourceKind.PDF:
                records, texts, title, source_pages = self._pdf(path, options)
            case SourceKind.IMAGE:
                records, texts, source_pages = self._image(path, options)
            case _:
                records, texts, title, source_pages, conv_meta = self._convert(
                    path, kind, options
                )
                metadata = metadata or conv_meta

        uses_ocr = any(r.method == PageMethod.VLM_OCR for r in records)
        manifest = DocumentManifest(
            doc_id=doc_id,
            source_path=str(path),
            source_name=path.name,
            source_kind=kind,
            mime=mime,
            size_bytes=path.stat().st_size,
            n_pages=len(records),
            source_pages=source_pages,
            max_pages=options.max_pages,
            ocr_all=ocr_all,
            pages=records,
            pipeline_version=PIPELINE_VERSION,
            config_hash=config_hash,
            ocr_model=self.ocr.model if uses_ocr else None,
            title=(metadata.title if metadata and metadata.title else None) or title or path.stem,
            metadata=metadata,
            total_seconds=round(time.perf_counter() - t0, 2),
        )
        return self.store.save(manifest, render_markdown(manifest, texts))

    # --------------------------------------------------------------- handlers
    def _converter(self, kind: SourceKind) -> DocumentConverter:
        try:
            return self.converters[kind]
        except KeyError:
            raise UnsupportedInputError(f"no converter configured for {kind.value} inputs") from None

    def _pdf(
        self, path: Path, options: IngestOptions
    ) -> tuple[list[PageRecord], list[str], str | None, int]:
        doc = self.pdf.open(path)
        try:
            title = doc.title
            total = len(doc)
            n = total if options.max_pages is None else min(total, options.max_pages)
            records: list[PageRecord] = []
            texts: list[str] = []
            raw_layers: dict[int, str] = {}
            for i in range(n):
                page = doc.page(i)
                t0 = time.perf_counter()
                signals, raw = page.signals()
                probe = decide(signals, self.policy, force_ocr=options.ocr_all)
                if probe.needs_ocr:
                    self.log(f"  p{i + 1}/{n}: OCR ({'; '.join(probe.reasons)})")
                    record, text = self._ocr_record(i, page.render(self.ocr.dpi), probe)
                else:
                    self.log(f"  p{i + 1}/{n}: text layer ({probe.n_chars} chars)")
                    raw_layers[i] = raw
                    record = PageRecord(
                        index=i,
                        method=PageMethod.TEXT_LAYER,
                        n_chars=0,  # set after cleaning
                        seconds=round(time.perf_counter() - t0, 3),
                        engine=self.pdf.fingerprint,
                        probe=probe,
                    )
                    text = ""
                records.append(record)
                texts.append(text)
        finally:
            doc.close()

        # De-hyphenate with the whole document's vocabulary, not just one page's.
        vocab = text_vocabulary(*raw_layers.values(), *texts)
        for i, raw in raw_layers.items():
            texts[i] = clean_text_layer(raw, vocab)
            records[i].n_chars = len(texts[i])
        return records, texts, title, total

    def _image(self, path: Path, options: IngestOptions) -> tuple[list[PageRecord], list[str], int]:
        frames = self.images.frames(path)  # multi-page TIFF -> several frames
        total = len(frames)
        if options.max_pages is not None:
            frames = frames[: options.max_pages]
        records, texts = [], []
        for i, frame in enumerate(frames):
            self.log(f"  frame {i + 1}/{len(frames)}: OCR (image input)")
            record, text = self._ocr_record(i, frame, None)
            records.append(record)
            texts.append(text)
        return records, texts, total

    def _convert(self, path: Path, kind: SourceKind, options: IngestOptions):
        t0 = time.perf_counter()
        conv = self._converter(kind).convert(path)
        for w in conv.warnings:
            self.log(f"  warning: {w}")
        segments = conv.segments
        total = len(segments)
        if options.max_pages is not None:
            segments = segments[: options.max_pages]
        per = round((time.perf_counter() - t0) / max(len(segments), 1), 3)
        records = [
            PageRecord(
                index=i,
                method=conv.method,
                n_chars=len(s.text),
                seconds=per,
                engine=conv.engine,
                title=s.title,
            )
            for i, s in enumerate(segments)
        ]
        self.log(f"  {conv.method.value}: {len(segments)} segment(s) via {conv.engine}")
        return records, [s.text for s in segments], conv.title, total, conv.metadata

    def _ocr_record(self, i: int, image, probe) -> tuple[PageRecord, str]:
        res = self.ocr.transcribe(image)
        self.log(
            f"    -> {len(res.text)} chars, {res.gen_tokens} tok in {res.seconds:.1f}s"
            f" ({res.finish_reason})"
        )
        ref: ModelRef = self.ocr.model
        return (
            PageRecord(
                index=i,
                method=PageMethod.VLM_OCR,
                n_chars=len(res.text),
                seconds=round(res.seconds, 2),
                engine=self.ocr.fingerprint,
                probe=probe,
                model=ref.repo_id,
                model_revision=ref.revision,
                gen_tokens=res.gen_tokens,
                finish_reason=res.finish_reason,
            ),
            res.text,
        )
