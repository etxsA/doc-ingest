"""Statistics for comparing OCR candidates evaluated on the same samples.

Two candidates' scores on the same pages are strongly correlated (a hard page is hard
for every model), so overlapping independent CIs say little. The paired bootstrap
resamples *units* (pages, tests) and keeps each unit's two scores together, which is
the right test for "is A better than B on this benchmark". Stratified resampling
reproduces benchmarks whose headline number is a mean of per-category means
(olmOCR-bench averages its per-file pass rates).

numpy is used only for speed: the full olmOCR-bench has ~7k tests x 10k resamples.
"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np

from .metrics import bootstrap_ci

__all__ = ["PairedResult", "bootstrap_ci", "paired_bootstrap", "quantile"]

_CHUNK = 500  # resamples drawn per numpy batch (bounded memory for large strata)


@dataclass(frozen=True)
class PairedResult:
    diff: float  # mean(a) - mean(b) (stratified: mean over strata of per-stratum diffs)
    low: float
    high: float
    p_value: float  # two-sided: 2 x share of resampled diffs on the other side of 0
    n: int  # paired units used

    @property
    def significant(self) -> bool:
        return not self.low <= 0.0 <= self.high


def quantile(values: Sequence[float], q: float) -> float:
    """Linear-interpolated quantile (numpy's default method), NaN for no values."""
    vals = sorted(values)
    if not vals:
        return math.nan
    pos = (len(vals) - 1) * q
    lo, hi = math.floor(pos), math.ceil(pos)
    return vals[lo] + (vals[hi] - vals[lo]) * (pos - lo)


def paired_bootstrap(
    a: Sequence[float | None],
    b: Sequence[float | None],
    *,
    groups: Sequence[str] | None = None,
    n: int = 10_000,
    alpha: float = 0.05,
    seed: int = 0,
) -> PairedResult:
    """Percentile bootstrap of mean(a - b) over aligned per-unit scores.

    Pairs where either side is ``None`` are dropped. With ``groups``, units are
    resampled within their group and the statistic is the mean of group means.
    Deterministic for a given ``seed``.
    """
    if len(a) != len(b) or (groups is not None and len(groups) != len(a)):
        raise ValueError("a, b (and groups) must be aligned: same length")
    strata: dict[str, list[float]] = defaultdict(list)
    for i, (x, y) in enumerate(zip(a, b, strict=True)):
        if x is None or y is None:
            continue
        strata[groups[i] if groups is not None else ""].append(x - y)
    n_pairs = sum(len(v) for v in strata.values())
    if not n_pairs:
        return PairedResult(math.nan, math.nan, math.nan, math.nan, 0)

    diffs = [np.asarray(v, dtype=np.float64) for _, v in sorted(strata.items())]
    observed = float(np.mean([d.mean() for d in diffs]))
    rng = np.random.default_rng(seed)
    boot = np.zeros(n)
    for d in diffs:  # sum of per-stratum resampled means, then divide by #strata
        k = len(d)
        for start in range(0, n, _CHUNK):
            size = min(_CHUNK, n - start)
            boot[start : start + size] += d[rng.integers(0, k, size=(size, k))].mean(axis=1)
    boot /= len(diffs)
    boot.sort()
    low, high = boot[int(n * alpha / 2)], boot[int(n * (1 - alpha / 2)) - 1]
    if np.all(boot == 0.0):
        p = 1.0
    else:
        p = min(1.0, 2 * min(float(np.mean(boot <= 0.0)), float(np.mean(boot >= 0.0))))
    return PairedResult(observed, float(low), float(high), p, n_pairs)
