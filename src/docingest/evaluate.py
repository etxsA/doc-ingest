"""OCR quality metrics against ground-truth text.

Markdown syntax and reading-order differences inflate raw CER, so we report
three views: CER/WER on normalized text, and an order-insensitive word F1.
"""

from __future__ import annotations

import re
import unicodedata
from collections import Counter

_MD = re.compile(r"[#*_`>|\[\]\\$^{}]|<!--.*?-->|-{3,}", re.DOTALL)


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)
    text = _MD.sub(" ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip().lower()


def word_f1(ref: str, hyp: str) -> float:
    r, h = Counter(ref.split()), Counter(hyp.split())
    overlap = sum((r & h).values())
    if not overlap:
        return 0.0
    p, rc = overlap / sum(h.values()), overlap / sum(r.values())
    return 2 * p * rc / (p + rc)


def score(reference: str, hypothesis: str) -> dict[str, float]:
    import jiwer

    ref, hyp = normalize(reference), normalize(hypothesis)
    return {
        "cer": round(jiwer.cer(ref, hyp), 4),
        "wer": round(jiwer.wer(ref, hyp), 4),
        "word_f1": round(word_f1(ref, hyp), 4),
        "ref_chars": len(ref),
        "hyp_chars": len(hyp),
    }
