"""Statistics for comparing OCR candidates evaluated on the same samples.

Two candidates' scores on the same pages are strongly correlated (a hard page is hard
for every model), so overlapping independent CIs say little. The paired analysis keeps
each unit's two scores together, which is the right design for "is A better than B on
this benchmark".

Units are not independent either. The synthetic suite scores every page at several
degradation levels, and an olmOCR-bench PDF carries many tests. Resampling those units
as if they were independent makes intervals too narrow and p-values too small (a null
simulation with 12 pages x 3 levels rejected 27% of the time at a nominal 5%). So units
are grouped into **clusters** (a page, a PDF): the bootstrap draws whole clusters and
the significance test flips the sign of whole clusters.

* ``cluster_bootstrap_ci`` / ``paired_bootstrap`` give percentile bootstrap CIs over
  clusters, optionally drawn within **strata** (olmOCR-bench draws PDFs within their
  jsonl category; strata of a single cluster are merged), of a statistic that may be a
  mean of per-**group** means (the olmOCR-bench headline is the mean of per-jsonl pass
  rates).
* ``paired_bootstrap``'s p-value is a paired randomization (sign-flip) test over
  clusters: under "A and B are exchangeable" every cluster's differences may flip sign
  together. It enumerates all 2^k patterns for k <= 16 clusters (exact; the smallest
  attainable p is 2/2^k, so a 2-page comparison can never be significant) and draws
  random patterns above that. Unlike a bootstrap it stays valid with few clusters.

numpy is used only for speed: the full olmOCR-bench has ~7k tests x 10k resamples.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np

from .metrics import bootstrap_ci

__all__ = [
    "MIN_CLUSTERS",
    "PairedResult",
    "bootstrap_ci",
    "cluster_bootstrap_ci",
    "paired_bootstrap",
    "quantile",
]

_CHUNK = 500  # resamples drawn per numpy batch (bounded memory for many clusters)
_EXACT_MAX_CLUSTERS = 16  # sign-flip test enumerates every pattern up to 2^16
# Below this many clusters a percentile bootstrap CI is noticeably too narrow; the
# report flags such comparisons and relies on the sign-flip p-value instead.
MIN_CLUSTERS = 10


@dataclass(frozen=True)
class PairedResult:
    diff: float  # mean(a) - mean(b) (grouped: mean over groups of per-group diffs)
    low: float  # percentile cluster-bootstrap CI of ``diff``
    high: float
    p_value: float  # two-sided sign-flip test over clusters
    n: int  # paired units used
    n_clusters: int = 0  # independent clusters those units form
    exact: bool = False  # p_value enumerated every sign pattern (else Monte Carlo)
    alpha: float = 0.05

    @property
    def significant(self) -> bool:
        """The sign-flip test rejects "no difference" at ``alpha``.

        Decided by the test, not by the CI: with few clusters the bootstrap CI can
        exclude 0 while the attainable p-values cannot go below 2/2^k.
        """
        return not math.isnan(self.p_value) and self.p_value < self.alpha


def quantile(values: Sequence[float], q: float) -> float:
    """Linear-interpolated quantile (numpy's default method), NaN for no values."""
    vals = sorted(values)
    if not vals:
        return math.nan
    pos = (len(vals) - 1) * q
    lo, hi = math.floor(pos), math.ceil(pos)
    return vals[lo] + (vals[hi] - vals[lo]) * (pos - lo)


@dataclass(frozen=True)
class _Design:
    """Per-cluster, per-group sums and counts of the kept units, plus the strata."""

    sums: np.ndarray  # (clusters, groups)
    counts: np.ndarray  # (clusters, groups)
    strata: list[np.ndarray]  # cluster indices per stratum

    @property
    def n_clusters(self) -> int:
        return self.sums.shape[0]

    def statistic(self, weights: np.ndarray) -> np.ndarray:
        """Mean over groups of per-group means, for rows of cluster weights (multiplicities)."""
        sums, counts = weights @ self.sums, weights @ self.counts
        with np.errstate(invalid="ignore", divide="ignore"):
            means = sums / counts  # a group absent from a resample is NaN, then skipped
        return np.nanmean(means, axis=1)

    def observed(self) -> float:
        return float(self.statistic(np.ones((1, self.n_clusters)))[0])

    def bootstrap(self, n: int, rng: np.random.Generator) -> np.ndarray:
        """``n`` resampled statistics: clusters drawn with replacement within each stratum."""
        out = np.empty(n)
        for start in range(0, n, _CHUNK):
            size = min(_CHUNK, n - start)
            weights = np.zeros((size, self.n_clusters))
            for idx in self.strata:
                k = len(idx)
                # counts of k draws with replacement from k clusters
                weights[:, idx] = rng.multinomial(k, np.full(k, 1.0 / k), size=size)
            out[start : start + size] = self.statistic(weights)
        return out


def _check_aligned(k: int, *labels: Sequence[str] | None) -> None:
    if any(x is not None and len(x) != k for x in labels):
        raise ValueError("scores, groups, clusters and strata must be aligned: same length")


def _design(
    values: Sequence[float],
    groups: Sequence[str] | None,
    clusters: Sequence[str] | None,
    strata: Sequence[str] | None,
) -> _Design:
    """Aligned per-unit labels -> ``_Design``. Defaults: one group; every unit its own
    cluster; strata = groups when units are their own clusters (the pre-cluster behaviour:
    resample within each group), else a single stratum."""
    k = len(values)
    _check_aligned(k, groups, clusters, strata)
    group_of = list(groups) if groups is not None else [""] * k
    cluster_of = list(clusters) if clusters is not None else [str(i) for i in range(k)]
    if strata is not None:
        stratum_of = list(strata)
    elif clusters is None:
        stratum_of = group_of
    else:
        stratum_of = [""] * k
    g_index = {g: i for i, g in enumerate(sorted(set(group_of)))}
    c_index: dict[str, int] = {}
    c_stratum: dict[int, str] = {}
    for c, s in zip(cluster_of, stratum_of, strict=True):
        i = c_index.setdefault(c, len(c_index))
        if c_stratum.setdefault(i, s) != s:
            raise ValueError(f"cluster {c!r} spans strata {c_stratum[i]!r} and {s!r}")
    sums = np.zeros((len(c_index), len(g_index)))
    counts = np.zeros_like(sums)
    rows = [c_index[c] for c in cluster_of]
    cols = [g_index[g] for g in group_of]
    np.add.at(sums, (rows, cols), np.asarray(values, dtype=np.float64))
    np.add.at(counts, (rows, cols), 1.0)
    by_stratum: dict[str, list[int]] = {}
    for i, s in c_stratum.items():
        by_stratum.setdefault(s, []).append(i)
    return _Design(sums, counts, [np.asarray(v) for v in _collapse(by_stratum)])


def _collapse(by_stratum: dict[str, list[int]]) -> list[list[int]]:
    """Merge strata holding a single cluster (the survey-statistics "collapsed strata").

    Resampling one cluster from a stratum of one always returns it, so that stratum would
    contribute no variance at all: a 1-PDF-per-category olmOCR-bench subset would get a
    zero-width CI. Singletons are pooled together, or a lone singleton joins the smallest
    other stratum.
    """
    strata = [v for _, v in sorted(by_stratum.items())]
    singles = [c for v in strata if len(v) == 1 for c in v]
    rest = [v for v in strata if len(v) > 1]
    if len(singles) == 1 and rest:
        smallest = min(range(len(rest)), key=lambda i: len(rest[i]))
        rest[smallest] = [*rest[smallest], *singles]
    elif singles:
        rest.append(singles)
    return rest


def _keep(mask: Sequence[bool], labels: Sequence[str] | None) -> list[str] | None:
    return None if labels is None else [x for x, m in zip(labels, mask, strict=True) if m]


def _percentiles(boot: np.ndarray, alpha: float) -> tuple[float, float]:
    boot = np.sort(boot)
    n = len(boot)
    return float(boot[int(n * alpha / 2)]), float(boot[int(n * (1 - alpha / 2)) - 1])


def cluster_bootstrap_ci(
    values: Sequence[float | None],
    *,
    groups: Sequence[str] | None = None,
    clusters: Sequence[str] | None = None,
    strata: Sequence[str] | None = None,
    n: int = 2000,
    alpha: float = 0.05,
    seed: int = 0,
) -> tuple[float, float, float]:
    """(statistic, low, high): percentile bootstrap that resamples whole clusters.

    The statistic is the mean of the units, or with ``groups`` the mean of per-group
    means. ``None`` values are dropped. A single cluster has no estimable spread, so its
    interval is (NaN, NaN), not a zero-width one. Deterministic for a given ``seed``.
    """
    _check_aligned(len(values), groups, clusters, strata)
    mask = [v is not None for v in values]
    kept = [float(v) for v in values if v is not None]
    if not kept:
        return math.nan, math.nan, math.nan
    d = _design(kept, _keep(mask, groups), _keep(mask, clusters), _keep(mask, strata))
    if d.n_clusters < 2:
        return d.observed(), math.nan, math.nan
    low, high = _percentiles(d.bootstrap(n, np.random.default_rng(seed)), alpha)
    return d.observed(), low, high


def _sign_flip_p(
    d: _Design, observed: float, n: int, rng: np.random.Generator
) -> tuple[float, bool]:
    """Two-sided p of the sign-flip test. The grouped mean is linear in each cluster's
    signs, so T(signs) = signs @ w with w_c = sum_g sums[c, g] / (count[g] * groups)."""
    total = d.counts.sum(axis=0)
    w = (d.sums / total).sum(axis=1) / d.sums.shape[1]
    k = len(w)
    tol = 1e-12 * max(1.0, abs(observed))
    if k <= _EXACT_MAX_CLUSTERS:
        patterns = ((np.arange(2**k)[:, None] >> np.arange(k)) & 1) * 2.0 - 1.0
        return float(np.mean(np.abs(patterns @ w) >= abs(observed) - tol)), True
    extreme = 0
    for start in range(0, n, _CHUNK):
        size = min(_CHUNK, n - start)
        signs = rng.choice(np.array([-1.0, 1.0]), size=(size, k))
        extreme += int(np.sum(np.abs(signs @ w) >= abs(observed) - tol))
    return (1 + extreme) / (1 + n), False  # the observed pattern counts: p is never 0


def paired_bootstrap(
    a: Sequence[float | None],
    b: Sequence[float | None],
    *,
    groups: Sequence[str] | None = None,
    clusters: Sequence[str] | None = None,
    strata: Sequence[str] | None = None,
    n: int = 10_000,
    alpha: float = 0.05,
    seed: int = 0,
) -> PairedResult:
    """Cluster bootstrap CI and sign-flip test of mean(a - b) over aligned per-unit scores.

    Pairs where either side is ``None`` are dropped. ``groups``: the statistic is the mean
    of group means. ``clusters``: units resampled / sign-flipped together (default: every
    unit alone). ``strata``: clusters are resampled within their stratum (default: the
    groups when there are no clusters, else one stratum). Deterministic for a ``seed``.
    """
    if len(a) != len(b):
        raise ValueError("a, b (and groups, clusters, strata) must be aligned: same length")
    _check_aligned(len(a), groups, clusters, strata)
    mask = [x is not None and y is not None for x, y in zip(a, b, strict=True)]
    diffs = [
        float(x) - float(y) for x, y in zip(a, b, strict=True) if x is not None and y is not None
    ]
    if not diffs:
        return PairedResult(math.nan, math.nan, math.nan, math.nan, 0, alpha=alpha)
    d = _design(diffs, _keep(mask, groups), _keep(mask, clusters), _keep(mask, strata))
    rng = np.random.default_rng(seed)
    observed = d.observed()
    low = high = math.nan  # one cluster: no estimable spread (and p is 1)
    if d.n_clusters >= 2:
        low, high = _percentiles(d.bootstrap(n, rng), alpha)
    p, exact = _sign_flip_p(d, observed, n, rng)
    return PairedResult(observed, low, high, p, len(diffs), d.n_clusters, exact, alpha)
