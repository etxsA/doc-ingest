"""Turn born-digital PDF pages into a realistic *scanned* PDF with known ground truth.

Lets us measure OCR quality (CER/WER) without hand-labelled data: the original
text layer is the reference, the degraded raster is what the OCR model sees.
"""

from __future__ import annotations

import json
import random
from pathlib import Path

import numpy as np
import pypdfium2 as pdfium
from PIL import Image, ImageFilter

from .pipeline import clean_text_layer, sha256_file, text_vocabulary

MIN_TRUTH_CHARS = 50


def degrade(img: Image.Image, seed: int) -> Image.Image:
    rng = random.Random(seed)
    g = img.convert("L")
    g = g.rotate(rng.uniform(-1.2, 1.2), resample=Image.BICUBIC, expand=False, fillcolor=255)
    g = g.filter(ImageFilter.GaussianBlur(radius=0.6))
    arr = np.asarray(g, dtype=np.float32)
    arr += np.random.default_rng(seed).normal(0, 12, arr.shape)  # sensor noise
    arr = arr * 0.92 + 10  # slightly grey paper, lower contrast
    return Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8))


def make_scan(
    src: Path, out_pdf: Path, pages: list[int], dpi: int = 150, seed: int = 0
) -> Path:
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
                raise ValueError(f"page {i} of {src.name} has no usable text layer for ground truth")
        images = [
            degrade(pdf[i].render(scale=dpi / 72).to_pil(), seed + k) for k, i in enumerate(pages)
        ]
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
