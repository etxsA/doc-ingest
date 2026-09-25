"""Router: detect input type, decide per page how to extract, emit canonical output."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import tempfile
import time
from collections.abc import Callable
from pathlib import Path

import pypdfium2 as pdfium
from PIL import Image, ImageSequence

from .config import PipelineConfig
from .detect import HYPHEN_MARK, detect_kind, probe_pdf_page
from .extractors.office import convert_office
from .extractors.vlm_ocr import VlmOcr
from .schema import DocumentManifest, ModelRef, PageMethod, PageRecord, SourceKind

PIPELINE_VERSION = "0.2.1"
VARIANTS_DIR = "_variants"  # partial / forced-OCR runs; never mixed into the corpus

Log = Callable[[str], None]

_SOFT_HYPHEN = re.compile(rf"(\w+){HYPHEN_MARK}(\w+)")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
_PAGE_MARKER = re.compile(r"^<!-- page (\d+) \| method=(\w+) -->$", re.MULTILINE)
_UMASK = os.umask(0)
os.umask(_UMASK)  # read once at import; os.umask is process-global


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def text_vocabulary(*texts: str) -> set[str]:
    return {w.lower() for t in texts for w in re.findall(r"\w+", t.replace(HYPHEN_MARK, " "))}


def clean_text_layer(text: str, vocab: set[str] | None = None) -> str:
    """Normalize a pdfium text layer.

    pdfium turns a line-end hyphen into U+0002 and joins the lines, for both real
    hyphenation ("transduc-tion") and compounds ("sequence-aligned"). Join the two
    halves only when the joined word occurs elsewhere in the document; otherwise
    keep the hyphen, which never loses information.
    """
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    vocab = text_vocabulary(text) if vocab is None else vocab

    def _join(m: re.Match) -> str:
        a, b = m.group(1), m.group(2)
        return a + b if (a + b).lower() in vocab else f"{a}-{b}"

    text = _SOFT_HYPHEN.sub(_join, text).replace(HYPHEN_MARK, "-")
    # Remaining "-\n" breaks are numbers / identifiers (2019-2020, Qwen-2.5-VL): keep the hyphen.
    text = re.sub(r"(?<=\w)-\n(?=\w)", "-", text)
    text = _CONTROL.sub("", text)
    text = re.sub(r"[ \t]+\n", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _atomic_write(path: Path, content: str) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        os.fchmod(fd, 0o666 & ~_UMASK)  # mkstemp creates 0600; match a normal write
        with os.fdopen(fd, "w") as f:
            f.write(content)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def _load_manifest(path: Path) -> DocumentManifest | None:
    try:
        return DocumentManifest.model_validate_json(path.read_text())
    except (OSError, ValueError):  # missing, truncated, or from an older schema -> cache miss
        return None


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
        if max_pages is not None and max_pages < 1:
            raise ValueError("max_pages must be >= 1")
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
        # config_hash covers everything that changes page content; max_pages only truncates.
        base_key = f"{PIPELINE_VERSION}|{self.cfg.hash()}|ocr_all={self.ocr_all}"
        config_hash = hashlib.sha256(base_key.encode()).hexdigest()[:12]
        root = Path(self.cfg.output_dir)
        canonical = root / doc_id[:16]
        pages_tag = "all" if self.max_pages is None else str(self.max_pages)
        variant = root / VARIANTS_DIR / f"{doc_id[:16]}-{config_hash}-p{pages_tag}"

        if not self.force:
            # A complete canonical result satisfies default and --max-pages requests alike.
            cached = _load_manifest(canonical / "manifest.json")
            if cached and cached.config_hash == config_hash and cached.n_pages == cached.source_pages:
                self.log(f"[cache] {path.name} -> {canonical} (config {config_hash})")
                return cached, canonical
            if self.max_pages is not None or self.ocr_all:
                cached = _load_manifest(variant / "manifest.json")
                if cached and cached.config_hash == config_hash and cached.max_pages == self.max_pages:
                    self.log(f"[cache] {path.name} -> {variant} (config {config_hash})")
                    return cached, variant

        t0 = time.perf_counter()
        title = None
        match kind:
            case SourceKind.PDF:
                records, texts, title, source_pages = self._pdf(path)
            case SourceKind.IMAGE:
                records, texts, source_pages = self._image(path)
            case SourceKind.OFFICE:
                records, texts = self._single(path, PageMethod.DOCLING, convert_office)
                source_pages = 1
            case SourceKind.TEXT:
                records, texts = self._single(
                    path, PageMethod.PASSTHROUGH, lambda p: p.read_text(errors="replace")
                )
                source_pages = 1

        uses_ocr = any(r.method == PageMethod.VLM_OCR for r in records)
        complete = len(records) == source_pages
        manifest = DocumentManifest(
            doc_id=doc_id,
            source_path=str(path),
            source_name=path.name,
            source_kind=kind,
            mime=mime,
            size_bytes=path.stat().st_size,
            n_pages=len(records),
            source_pages=source_pages,
            max_pages=self.max_pages,
            ocr_all=self.ocr_all,
            pages=records,
            pipeline_version=PIPELINE_VERSION,
            config_hash=config_hash,
            ocr_model=ModelRef(repo_id=self.cfg.ocr.repo_id, revision=self.cfg.ocr.revision)
            if uses_ocr
            else None,
            title=title or path.stem,
            total_seconds=round(time.perf_counter() - t0, 2),
        )

        # Only complete default runs become the canonical corpus entry; partial or
        # forced-OCR runs go to a variant dir so they never replace a full result.
        out_dir = canonical if complete and not self.ocr_all else variant
        out_dir.mkdir(parents=True, exist_ok=True)
        _atomic_write(out_dir / "document.md", render_markdown(manifest, texts))
        _atomic_write(out_dir / "manifest.json", manifest.model_dump_json(indent=2))
        return manifest, out_dir

    # --------------------------------------------------------------- handlers
    def _pdf(self, path: Path) -> tuple[list[PageRecord], list[str], str | None, int]:
        pdf = pdfium.PdfDocument(path)
        try:
            title = (pdf.get_metadata_dict().get("Title") or "").strip() or None
            total = len(pdf)
            n = total if self.max_pages is None else min(total, self.max_pages)
            records, texts, raw_layers = [], [], {}
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
                    record, text = self._ocr_record(i, image, probe=probe)
                else:
                    self.log(f"  p{i + 1}/{n}: text layer ({probe.n_chars} chars)")
                    raw_layers[i] = raw
                    record = PageRecord(
                        index=i,
                        method=PageMethod.TEXT_LAYER,
                        n_chars=0,  # set after cleaning
                        seconds=round(time.perf_counter() - t0, 3),
                        probe=probe,
                    )
                    text = ""
                records.append(record)
                texts.append(text)
        finally:
            pdf.close()

        # De-hyphenate with the whole document's vocabulary, not just one page's.
        vocab = text_vocabulary(*raw_layers.values(), *texts)
        for i, raw in raw_layers.items():
            texts[i] = clean_text_layer(raw, vocab)
            records[i].n_chars = len(texts[i])
        return records, texts, title, total

    def _image(self, path: Path) -> tuple[list[PageRecord], list[str], int]:
        records, texts = [], []
        with Image.open(path) as img:
            frames = [f.copy() for f in ImageSequence.Iterator(img)]  # multi-page TIFF
        total = len(frames)
        if self.max_pages is not None:
            frames = frames[: self.max_pages]
        for i, frame in enumerate(frames):
            self.log(f"  frame {i + 1}/{len(frames)}: OCR (image input)")
            record, text = self._ocr_record(i, frame)
            records.append(record)
            texts.append(text)
        return records, texts, total

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


def split_pages(markdown: str) -> dict[int, str]:
    """Inverse of :func:`render_markdown`: {1-based page number: page text}."""
    marks = list(_PAGE_MARKER.finditer(markdown))
    pages = {}
    for m, nxt in zip(marks, [*marks[1:], None], strict=True):
        end = nxt.start() if nxt else len(markdown)
        pages[int(m.group(1))] = markdown[m.end() : end].strip()
    return pages


def iter_inputs(paths: list[Path]) -> list[Path]:
    out: list[Path] = []
    for p in paths:
        if p.is_dir():
            out.extend(sorted(x for x in p.rglob("*") if x.is_file() and not x.name.startswith(".")))
        else:
            out.append(p)
    return out


def dump_index(manifests: list[DocumentManifest], out_dir: Path) -> Path:
    """Catalog of canonical (complete) documents, handy for the agent / PaperQA step."""
    index = out_dir / "index.json"
    try:
        existing = json.loads(index.read_text())
    except (OSError, ValueError):
        existing = {}
    for m in manifests:
        if m.n_pages != m.source_pages or m.ocr_all:
            continue  # variants are not part of the corpus
        existing[m.doc_id] = {
            "source_name": m.source_name,
            "title": m.title,
            "kind": m.source_kind.value,
            "pages": m.n_pages,
            "ocr_pages": m.ocr_pages,
            "dir": m.doc_id[:16],
        }
    out_dir.mkdir(parents=True, exist_ok=True)
    _atomic_write(index, json.dumps(existing, indent=2))
    return index
