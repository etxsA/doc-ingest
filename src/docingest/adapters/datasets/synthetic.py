"""Turn born-digital PDF pages into a realistic *scanned* PDF with known ground truth.

Lets us measure OCR quality (CER/WER) without hand-labelled data: the original
text layer is the reference, the degraded raster is what the OCR model sees.
``make_scan`` writes one image-only PDF; ``SyntheticSuite`` is the same idea as a
benchmark suite, with several degradation levels of every selected page.
"""

from __future__ import annotations

import hashlib
import io
import json
import math
import random
import zlib
from collections.abc import Sequence
from dataclasses import asdict
from functools import partial
from importlib.metadata import version
from pathlib import Path
from typing import Any

import numpy as np
import pypdfium2 as pdfium
from PIL import Image, ImageFilter

from ...application.ingest import sha256_file
from ...application.metrics import plain_text, running_lines, strip_furniture
from ...application.metrics import score as score_text
from ...application.stats import cluster_bootstrap_ci
from ...domain.errors import DocumentOpenError
from ...domain.text import clean_text_layer, text_vocabulary
from ...ports import Estimate, Sample, SuiteScore

MIN_TRUTH_CHARS = 50


def _render(page: pdfium.PdfPage, dpi: int) -> Image.Image:
    bitmap = page.render(scale=dpi / 72)  # pyright: ignore[reportArgumentType]  # pypdfium2 accepts float scale
    return bitmap.to_pil()


def degrade(img: Image.Image, seed: int) -> Image.Image:
    rng = random.Random(seed)
    g = img.convert("L")
    g = g.rotate(
        rng.uniform(-1.2, 1.2), resample=Image.Resampling.BICUBIC, expand=False, fillcolor=255
    )
    g = g.filter(ImageFilter.GaussianBlur(radius=0.6))
    arr = np.asarray(g, dtype=np.float32)
    arr += np.random.default_rng(seed).normal(0, 12, arr.shape)  # sensor noise
    arr = arr * 0.92 + 10  # slightly grey paper, lower contrast
    return Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8))


def make_scan(src: Path, out_pdf: Path, pages: list[int], dpi: int = 150, seed: int = 0) -> Path:
    """Write an image-only PDF of ``pages`` (0-based) + ``<out>.truth.json``."""
    pdf = pdfium.PdfDocument(src)
    try:
        if bad := [i for i in pages if not 0 <= i < len(pdf)]:
            raise ValueError(f"pages {bad} out of range: {src.name} has {len(pdf)} pages")
        raws = [pdf[i].get_textpage().get_text_bounded() for i in pages]
        vocab = text_vocabulary(*raws)
        truth = [clean_text_layer(raw, vocab) for raw in raws]
        for i, t in zip(pages, truth, strict=True):
            if len("".join(t.split())) < MIN_TRUTH_CHARS:
                raise ValueError(
                    f"page {i} of {src.name} has no usable text layer for ground truth"
                )
        images = [degrade(_render(pdf[i], dpi), seed + k) for k, i in enumerate(pages)]
    finally:
        pdf.close()
    out_pdf.parent.mkdir(parents=True, exist_ok=True)
    # JPEG-compressed, image-only pages: no text layer at all, like a real scanner.
    # No timestamps -> identical bytes on every run, so the scan's sha256 / doc_id is stable.
    images[0].save(
        out_pdf,
        "PDF",
        resolution=dpi,
        save_all=True,
        append_images=images[1:],
        quality=70,
        title=src.stem,
        creationDate=None,
        modDate=None,
    )
    truth_path = out_pdf.with_suffix(".truth.json")
    meta = {
        "source": src.name,
        "source_sha256": sha256_file(src),
        "pages": pages,
        "dpi": dpi,
        "seed": seed,
        "scan_sha256": sha256_file(out_pdf),
        "text": truth,
    }
    truth_path.write_text(json.dumps(meta, indent=2))
    return truth_path


# --------------------------------------------------------------------------- benchmark suite

LEVELS = ("clean", "light", "heavy")
DEGRADE_VERSION = 1  # bump when degrade() / degrade_heavy() change: invalidates runs
# How outputs are scored. Not part of the fingerprint: a change here re-scores a finished
# run (``bench report`` does it automatically), it never forces a re-transcription.
#   1: metrics of plain_text(output) vs the page's cleaned text layer; units bootstrapped
#      independently.
#   2: page furniture (running headers, bare page numbers, arXiv margin stamps) removed
#      from reference and output; <img> descriptions dropped and LaTeX math normalized
#      (metrics.normalize); CIs and paired tests resample pages, all levels together.
SCORING_VERSION = 3
METRICS = ("cer", "wer", "word_f1", "char3_f1")
HIGHER_IS_BETTER = {"cer": False, "wer": False, "word_f1": True, "char3_f1": True}


def degrade_heavy(img: Image.Image, seed: int) -> Image.Image:
    """A bad scan: skewed, blurry, noisy, low contrast, then JPEG q35 (blocking, ringing)."""
    rng = random.Random(seed)
    g = img.convert("L")
    g = g.rotate(
        rng.uniform(-2.5, 2.5), resample=Image.Resampling.BICUBIC, expand=False, fillcolor=255
    )
    g = g.filter(ImageFilter.GaussianBlur(radius=1.1))
    arr = np.asarray(g, dtype=np.float32)
    arr += np.random.default_rng(seed).normal(0, 22, arr.shape)
    arr = arr * 0.85 + 18
    buf = io.BytesIO()
    Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8)).save(buf, "JPEG", quality=35)
    return Image.open(io.BytesIO(buf.getvalue())).convert("L")


def apply_level(img: Image.Image, level: str, seed: int) -> Image.Image:
    if level == "clean":  # the raster alone: isolates recognition from image quality
        return img.convert("RGB")
    if level == "light":
        return degrade(img, seed)
    if level == "heavy":
        return degrade_heavy(img, seed)
    raise ValueError(f"unknown degradation level {level!r}; choose from {LEVELS}")


# Markup a model may legitimately emit but a PDF text layer never contains: HTML tables
# (the olmocr / nanonets prompts ask for them), OCR-model tags, figure placeholders.
# Tags go, their text (cells, captions) stays; Markdown syntax is handled by metrics.
def _sample_seed(seed: int, *parts: object) -> int:
    """Stable across processes and platforms (unlike hash())."""
    return zlib.crc32(":".join(map(str, (seed, *parts))).encode())


def _round(x: float) -> float | None:
    """4 decimals; NaN (no estimable interval: a single page) -> None."""
    return None if math.isnan(x) else round(x, 4)


def _estimates(rows: list[tuple[str, dict[str, float | None]]]) -> dict[str, Estimate]:
    """Per-metric mean and CI of (cluster, unit scores) rows; clusters are resampled whole."""
    out = {}
    for m in METRICS:
        kept = [(c, v) for c, r in rows if (v := r.get(m)) is not None]
        vals = [v for _, v in kept]
        mean, lo, hi = cluster_bootstrap_ci(vals, clusters=[c for c, _ in kept])
        out[m] = (
            Estimate(_round(mean), _round(lo), _round(hi), len(vals)) if vals else Estimate(None)
        )
    return out


class SyntheticSuite:
    """Born-digital pages, rasterized at a fixed dpi and degraded deterministically.

    Reference = the page's cleaned text layer (pages with too little text are skipped).
    Every page appears at every level, so levels are directly comparable, and the levels
    of one page form one cluster for the statistics: they are not independent evidence.
    Page furniture is removed from reference and output at scoring time (``strip_furniture``):
    the prompts disagree on whether to transcribe it, and the text layer always has it.
    """

    name = "synthetic"
    scoring_version = SCORING_VERSION
    # Pages whose reference is known to be unrepresentative (cluster id -> reason). They
    # are scored and reported separately, not dropped, and never enter headline metrics
    # or paired tests. Set from [scoring.synthetic] (not part of the suite fingerprint).
    headline_exclusions: dict[str, str] = {}  # noqa: RUF012 - replaced per instance

    @property
    def scoring_options(self) -> dict[str, Any]:
        """Stamped on every score: changing [scoring.synthetic] makes saved scores stale."""
        return {"report_separately": dict(self.headline_exclusions)}

    def __init__(
        self,
        documents: Sequence[tuple[Path, Sequence[int]]],  # (pdf, 0-based page indices)
        *,
        levels: Sequence[str] = LEVELS,
        dpi: int = 200,
        seed: int = 0,
        min_ref_chars: int = 200,
        primary_metric: str = "cer",
    ):
        if bad := [lv for lv in levels if lv not in LEVELS]:
            raise ValueError(f"unknown degradation levels {bad}; choose from {LEVELS}")
        if primary_metric not in METRICS:
            raise ValueError(f"primary_metric must be one of {METRICS}")
        self.documents = [(Path(p), list(pages)) for p, pages in documents]
        self.levels = list(levels)
        self.dpi, self.seed, self.min_ref_chars = dpi, seed, min_ref_chars
        self.primary_metric = primary_metric
        for path, _ in self.documents:
            if not path.is_file():
                raise DocumentOpenError(f"synthetic suite source not found: {path}")
        # What the model sees and what it is scored against; not the choice of headline metric.
        definition = {
            "sources": [(p.name, sha256_file(p), pages) for p, pages in self.documents],
            "levels": self.levels,
            "dpi": dpi,
            "seed": seed,
            "min_ref_chars": min_ref_chars,
            "degrade": DEGRADE_VERSION,
            "libs": {d: version(d) for d in ("pypdfium2", "pillow", "numpy")},
        }
        blob = json.dumps(definition, sort_keys=True).encode()
        self.fingerprint = hashlib.sha256(blob).hexdigest()
        self.skipped: list[str] = []  # pages left out, with the reason
        self._samples: list[Sample] | None = None

    def samples(self) -> list[Sample]:
        if self._samples is None:
            self._samples = self._build()
        return self._samples

    def _build(self) -> list[Sample]:
        samples: list[Sample] = []
        for path, pages in self.documents:
            try:
                pdf = pdfium.PdfDocument(path)
            except pdfium.PdfiumError as e:
                raise DocumentOpenError(f"cannot open {path.name}: {e}") from e
            try:  # vocabulary of the whole document decides which hyphens to join
                raws = [pdf[i].get_textpage().get_text_bounded() for i in range(len(pdf))]
            finally:
                pdf.close()
            vocab = text_vocabulary(*raws)
            # Running headers / footers recur across the document: found on every page,
            # stripped when scoring (sample ids, references and the fingerprint unchanged).
            running = running_lines([clean_text_layer(raw, vocab) for raw in raws])
            for page in pages:
                if not 0 <= page < len(raws):
                    self.skipped.append(f"{path.name} page {page}: out of range ({len(raws)})")
                    continue
                ref = clean_text_layer(raws[page], vocab)
                if (n := len("".join(ref.split()))) < self.min_ref_chars:
                    self.skipped.append(f"{path.name} page {page}: {n} reference chars")
                    continue
                for level in self.levels:
                    samples.append(
                        Sample(
                            id=f"{path.stem}_p{page + 1:03d}_{level}",
                            category=level,
                            load_image=partial(self.render, path, page, level),
                            reference=ref,
                            extra={
                                "pdf": path.name,
                                "page": page,
                                "level": level,
                                "running": running,
                            },
                        )
                    )
        return samples

    def render(self, path: Path, page: int, level: str) -> Image.Image:
        pdf = pdfium.PdfDocument(path)
        try:
            img = _render(pdf[page], self.dpi)
        finally:
            pdf.close()
        return apply_level(img, level, _sample_seed(self.seed, path.name, page, level))

    def output_path(self, run_dir: Path, candidate: str, sample: Sample) -> Path:
        return run_dir / self.name / candidate / f"{sample.id}.md"

    @staticmethod
    def cluster(sample: Sample) -> str:
        """The page a sample shows: every degradation level of it is one cluster."""
        return f"{sample.extra['pdf']}#p{int(sample.extra['page']) + 1:03d}"

    def score(self, run_dir: Path, candidates: list[str]) -> dict[str, SuiteScore]:
        samples = self.samples()
        clusters = {s.id: self.cluster(s) for s in samples}
        out = {}
        for cand in candidates:
            units: dict[str, dict[str, float | None]] = {}
            for s in samples:
                p = self.output_path(run_dir, cand, s)
                if p.exists():
                    ref, hyp = strip_furniture(
                        s.reference or "",
                        plain_text(p.read_text(encoding="utf-8")),
                        s.extra.get("running", ()),
                    )
                    sc = score_text(ref, hyp)
                    units[s.id] = {m: sc[m] for m in METRICS}
            separate = {
                cluster: {
                    "reason": reason,
                    "metrics": {
                        m: asdict(e)
                        for m, e in _estimates(
                            [(clusters[u], units[u]) for u in units if clusters[u] == cluster]
                        ).items()
                    },
                }
                for cluster, reason in self.headline_exclusions.items()
                if any(clusters[u] == cluster for u in units)
            }
            units = {u: v for u, v in units.items() if clusters[u] not in self.headline_exclusions}
            by_level = {
                lv: _estimates(
                    [
                        (clusters[s.id], units[s.id])
                        for s in samples
                        if s.category == lv and s.id in units
                    ]
                )
                for lv in self.levels
            }
            missing = len(
                [s for s in samples if clusters[s.id] not in self.headline_exclusions]
            ) - len(units)
            out[cand] = SuiteScore(
                primary=self.primary_metric,
                higher_is_better=HIGHER_IS_BETTER[self.primary_metric],
                metrics=_estimates([(clusters[u], units[u]) for u in units]),
                by_category=by_level,
                units=units,
                unit_clusters={u: clusters[u] for u in units},
                n_outputs=len(units),
                n_samples=len(samples),
                errors=[f"{missing} of {len(samples)} samples have no output yet"]
                if missing
                else [],
                details={
                    "skipped_pages": self.skipped,
                    "hypothesis": "plain_text() minus page furniture",
                    "reference": "cleaned text layer minus page furniture",
                    "clusters": "a page with all its degradation levels",
                    "n_clusters": len({clusters[u] for u in units}),
                    "reported_separately": separate,
                },
                scoring_version=SCORING_VERSION,
            )
        return out
