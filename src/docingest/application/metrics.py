"""OCR quality metrics against reference text, with bootstrap confidence intervals.

Markdown syntax, hyphenation and reading order inflate raw CER, so several views
are reported: CER/WER on normalized text, order-insensitive word F1, and a
character 3-gram F1 that tolerates reordering but still penalizes misspellings.
"""

from __future__ import annotations

import random
import re
import unicodedata
from collections import Counter
from collections.abc import Sequence

_MD = re.compile(r"[#*_`>|\[\]\\$^{}]|<!--.*?-->|-{3,}", re.DOTALL)


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)
    # Hyphenation is a formatting choice ("transduc-tion" / "transduction"): ignore it.
    text = re.sub(r"(\w)[-\x02]\s*(\w)", r"\1\2", text)
    text = _MD.sub(" ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip().lower()


def _bag_f1(ref: Counter[str], hyp: Counter[str]) -> float:
    overlap = sum((ref & hyp).values())
    if not overlap:
        return 0.0
    p, r = overlap / sum(hyp.values()), overlap / sum(ref.values())
    return 2 * p * r / (p + r)


def word_f1(ref: str, hyp: str) -> float:
    return _bag_f1(Counter(ref.split()), Counter(hyp.split()))


def _word_trigrams(text: str) -> Counter[str]:
    """Trigrams inside space-padded words: independent of word order, sensitive to spelling."""
    grams: Counter[str] = Counter()
    for w in text.split():
        w = f" {w} "
        grams.update(w[i : i + 3] for i in range(len(w) - 2))
    return grams


def char3_f1(ref: str, hyp: str) -> float:
    return _bag_f1(_word_trigrams(ref), _word_trigrams(hyp))


def score(reference: str, hypothesis: str) -> dict[str, float | None]:
    import jiwer

    ref, hyp = normalize(reference), normalize(hypothesis)
    if not ref:  # jiwer would return a raw insertion count, not a rate
        return {
            "cer": None,
            "wer": None,
            "word_f1": None,
            "char3_f1": None,
            "ref_chars": 0,
            "hyp_chars": len(hyp),
        }
    return {
        "cer": round(min(jiwer.cer(ref, hyp), 1.0), 4),  # capped: runaway output -> 1.0
        "wer": round(min(jiwer.wer(ref, hyp), 1.0), 4),
        "word_f1": round(word_f1(ref, hyp), 4),
        "char3_f1": round(char3_f1(ref, hyp), 4),
        "ref_chars": len(ref),
        "hyp_chars": len(hyp),
    }


def bootstrap_ci(
    values: Sequence[float], *, n: int = 2000, alpha: float = 0.05, seed: int = 0
) -> tuple[float, float, float]:
    """(mean, low, high) percentile bootstrap CI of the mean. Deterministic via seed."""
    vals = [v for v in values if v is not None]
    if not vals:
        return (float("nan"),) * 3
    rng = random.Random(seed)
    k = len(vals)
    means = sorted(sum(rng.choices(vals, k=k)) / k for _ in range(n))
    lo, hi = means[int(n * alpha / 2)], means[int(n * (1 - alpha / 2)) - 1]
    return sum(vals) / k, lo, hi
