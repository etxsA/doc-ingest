"""Rank fusion, as in the measured retrieval."""

from __future__ import annotations

RRF_K = 60


def rrf(
    first: list[int], second: list[int], *, k: int = RRF_K, n: int = 100
) -> list[tuple[int, float]]:
    """Reciprocal rank fusion of two rankings: ``(item, score)`` best first, at most ``n``.

    An item's score is the sum of ``1 / (k + rank)`` over the rankings that hold it (rank from
    1). Ties keep the order of first appearance (``first`` before ``second``), because the
    sort is stable.
    """
    scores: dict[int, float] = {}
    for ranking in (first, second):
        for rank, item in enumerate(ranking):
            scores[item] = scores.get(item, 0.0) + 1.0 / (k + rank + 1)
    return sorted(scores.items(), key=lambda pair: -pair[1])[:n]
