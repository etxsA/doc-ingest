"""Router: detect input type, decide per page how to extract, emit canonical output."""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections.abc import Callable
from pathlib import Path

import pypdfium2 as pdfium
from PIL import Image, ImageSequence

from .config import PipelineConfig
from .detect import detect_kind, probe_pdf_page
from .extractors.office import convert_office
from .extractors.vlm_ocr import VlmOcr
from .schema import DocumentManifest, ModelRef, PageMethod, PageRecord, SourceKind

PIPELINE_VERSION = "0.1.0"

Log = Callable[[str], None]


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def clean_text_layer(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"(\w)-\n(\w)", r"\1\2", text)  # undo end-of-line hyphenation
    text = re.sub(r"[ \t]+\n", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


class Pipeline:
    def __init__(
        self,
        cfg: PipelineConfig,
        *,
        force: bool = False,
        ocr_all: bool = False,
        max_pages: int | None = None,
        log: Log = print,
    ):
        self.cfg = cfg
        self.force = force
        self.ocr_all = ocr_all
        self.max_pages = max_pages
        self.log = log
        self.ocr = VlmOcr(cfg.ocr)

    # ------------------------------------------------------------------ public
    def ingest(self, path: Path) -> tuple[DocumentManifest, Path]:
        path = path.resolve()
        kind, mime = detect_kind(path)
        doc_id = sha256_file(path)
        # Run options that change the output are part of the cache key.
        run_key = (
            f"{PIPELINE_VERSION}|{self.cfg.hash()}|ocr_all={self.ocr_all}|max_pages={self.max_pages}"
        )
        config_hash = hashlib.sha256(run_key.encode()).hexdigest()[:12]
        out_dir = Path(self.cfg.output_dir) / doc_id[:16]
        manifest_path = out_dir / "manifest.json"

        if manifest_path.exists() and not self.force:
            cached = DocumentManifest.model_validate_json(manifest_path.read_text())
            if cached.config_hash == config_hash:
                self.log(f"[cache] {path.name} -> {out_dir} (config {config_hash})")
                return cached, out_dir

        t0 = time.perf_counter()
        title = None
        match kind:
            case SourceKind.PDF:
                records, texts, title = self._pdf(path)
            case SourceKind.IMAGE:
                records, texts = self._image(path)
            case SourceKind.OFFICE:
                records, texts = self._single(path, PageMethod.DOCLING, convert_office)
            case SourceKind.TEXT:
                records, texts = self._single(
                    path, PageMethod.PASSTHROUGH, lambda p: p.read_text(errors="replace")
                )

        uses_ocr = any(r.method == PageMethod.VLM_OCR for r in records)
        manifest = DocumentManifest(
            doc_id=doc_id,
            source_path=str(path),
            source_name=path.name,
            source_kind=kind,
            mime=mime,
            size_bytes=path.stat().st_size,
            n_pages=len(records),
            pages=records,
            pipeline_version=PIPELINE_VERSION,
            config_hash=config_hash,
            ocr_model=ModelRef(repo_id=self.cfg.ocr.repo_id, revision=self.cfg.ocr.revision)
            if uses_ocr
            else None,
            title=title or path.stem,
            total_seconds=round(time.perf_counter() - t0, 2),
        )

        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "document.md").write_text(render_markdown(manifest, texts))
        manifest_path.write_text(manifest.model_dump_json(indent=2))
        return manifest, out_dir

    # --------------------------------------------------------------- handlers
    def _pdf(self, path: Path) -> tuple[list[PageRecord], list[str], str | None]:
        pdf = pdfium.PdfDocument(path)
        title = (pdf.get_metadata_dict().get("Title") or "").strip() or None
        n = len(pdf) if self.max_pages is None else min(len(pdf), self.max_pages)
        records, texts = [], []
        for i in range(n):
            page = pdf[i]
            t0 = time.perf_counter()
            probe, raw = probe_pdf_page(page, self.cfg.probe)
            if self.ocr_all and not probe.needs_ocr:
                probe.needs_ocr = True
                probe.reasons.append("forced (--ocr-all)")
            if probe.needs_ocr:
                image = page.render(scale=self.cfg.ocr.dpi / 72).to_pil()
                self.log(f"  p{i + 1}/{n}: OCR ({'; '.join(probe.reasons)})")
                records_text = self._ocr_record(i, image, probe=probe)
            else:
                text = clean_text_layer(raw)
                self.log(f"  p{i + 1}/{n}: text layer ({probe.n_chars} chars)")
                records_text = (
                    PageRecord(
                        index=i,
                        method=PageMethod.TEXT_LAYER,
                        n_chars=len(text),
                        seconds=round(time.perf_counter() - t0, 3),
                        probe=probe,
                    ),
                    text,
                )
            records.append(records_text[0])
            texts.append(records_text[1])
        pdf.close()
        return records, texts, title

    def _image(self, path: Path) -> tuple[list[PageRecord], list[str]]:
        records, texts = [], []
        with Image.open(path) as img:
            frames = [f.copy() for f in ImageSequence.Iterator(img)]  # multi-page TIFF
        if self.max_pages is not None:
            frames = frames[: self.max_pages]
        for i, frame in enumerate(frames):
            self.log(f"  frame {i + 1}/{len(frames)}: OCR (image input)")
            record, text = self._ocr_record(i, frame)
            records.append(record)
            texts.append(text)
        return records, texts

    def _single(self, path: Path, method: PageMethod, fn) -> tuple[list[PageRecord], list[str]]:
        t0 = time.perf_counter()
        text = fn(path)
        rec = PageRecord(
            index=0, method=method, n_chars=len(text), seconds=round(time.perf_counter() - t0, 3)
        )
        return [rec], [text]

    def _ocr_record(self, i: int, image: Image.Image, probe=None) -> tuple[PageRecord, str]:
        res = self.ocr(image)
        self.log(
            f"    -> {len(res.text)} chars, {res.gen_tokens} tok in {res.seconds:.1f}s"
            f" ({res.finish_reason})"
        )
        return (
            PageRecord(
                index=i,
                method=PageMethod.VLM_OCR,
                n_chars=len(res.text),
                seconds=round(res.seconds, 2),
                probe=probe,
                model=self.cfg.ocr.repo_id,
                model_revision=self.cfg.ocr.revision,
                gen_tokens=res.gen_tokens,
                finish_reason=res.finish_reason,
            ),
            res.text,
        )


def render_markdown(manifest: DocumentManifest, texts: list[str]) -> str:
    parts = [f"# {manifest.title}\n" if manifest.title else ""]
    for rec, text in zip(manifest.pages, texts, strict=True):
        parts.append(f"<!-- page {rec.index + 1} | method={rec.method.value} -->\n{text}\n")
    return "\n".join(parts).strip() + "\n"


def iter_inputs(paths: list[Path]) -> list[Path]:
    out: list[Path] = []
    for p in paths:
        if p.is_dir():
            out.extend(sorted(x for x in p.rglob("*") if x.is_file() and not x.name.startswith(".")))
        else:
            out.append(p)
    return out


def dump_index(manifests: list[DocumentManifest], out_dir: Path) -> Path:
    """A small catalog of everything ingested, handy for the agent / PaperQA step."""
    index = out_dir / "index.json"
    existing = json.loads(index.read_text()) if index.exists() else {}
    for m in manifests:
        existing[m.doc_id] = {
            "source_name": m.source_name,
            "title": m.title,
            "kind": m.source_kind.value,
            "pages": m.n_pages,
            "ocr_pages": m.ocr_pages,
            "dir": m.doc_id[:16],
        }
    index.write_text(json.dumps(existing, indent=2))
    return index
