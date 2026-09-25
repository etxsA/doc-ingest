import math

import numpy as np
import pytest

from docingest.application import metrics
from docingest.application.stats import bootstrap_ci, paired_bootstrap, quantile


def test_paired_bootstrap_detects_a_consistent_improvement():
    b = [0.30, 0.50, 0.20, 0.90, 0.40, 0.60, 0.10, 0.70, 0.80, 0.35]
    a = [x + 0.05 for x in b]  # a beats b by 0.05 on every unit
    r = paired_bootstrap(a, b, n=2000, seed=1)
    assert r.diff == pytest.approx(0.05)
    assert r.low > 0 and r.significant and r.p_value < 0.01 and r.n == 10
    swapped = paired_bootstrap(b, a, n=2000, seed=1)  # sign flips, CI mirrors
    assert swapped.diff == pytest.approx(-0.05) and swapped.high < 0


def test_paired_bootstrap_noise_is_not_significant_and_identical_scores_give_p_1():
    rng = np.random.default_rng(0)
    b = rng.uniform(0, 1, 40).tolist()
    a = [x + e for x, e in zip(b, rng.normal(0, 0.1, 40), strict=True)]
    r = paired_bootstrap(a, b, n=2000)
    assert r.low <= 0 <= r.high or r.p_value > 0.01
    same = paired_bootstrap(b, b, n=500)
    assert same.diff == 0 and same.p_value == 1.0 and not same.significant


def test_paired_bootstrap_is_deterministic_and_drops_missing_pairs():
    a, b = [1.0, None, 0.0, 1.0, 0.5], [0.0, 1.0, None, 0.5, 0.5]
    r1 = paired_bootstrap(a, b, n=1000, seed=7)
    assert r1 == paired_bootstrap(a, b, n=1000, seed=7)
    assert r1.n == 3 and r1.diff == pytest.approx((1.0 + 0.5 + 0.0) / 3)
    with pytest.raises(ValueError, match="aligned"):
        paired_bootstrap([1.0], [1.0, 2.0])
    empty = paired_bootstrap([None], [1.0])
    assert empty.n == 0 and math.isnan(empty.diff)


def test_stratified_paired_bootstrap_averages_group_means():
    # Group "x" has 1 unit with diff 1, group "y" has 3 units with diff 0:
    # the plain mean is 0.25, the stratified (macro) mean 0.5, like olmOCR-bench's score.
    a, b = [1.0, 0.0, 0.0, 0.0], [0.0, 0.0, 0.0, 0.0]
    plain = paired_bootstrap(a, b, n=500)
    strat = paired_bootstrap(a, b, groups=["x", "y", "y", "y"], n=500)
    assert plain.diff == pytest.approx(0.25)
    assert strat.diff == pytest.approx(0.5) and strat.low == strat.high == pytest.approx(0.5)


def test_quantile_matches_numpy_linear_interpolation():
    vals = [3.0, 1.0, 4.0, 1.5, 9.0, 2.6]
    for q in (0.0, 0.5, 0.9, 1.0):
        assert quantile(vals, q) == pytest.approx(np.quantile(vals, q))
    assert math.isnan(quantile([], 0.5))


def test_bootstrap_ci_is_the_metrics_implementation():
    assert bootstrap_ci is metrics.bootstrap_ci
