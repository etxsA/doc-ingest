from fractions import Fraction

import pytest

from docingest_index.fusion import RRF_K, rrf


def test_reciprocal_rank_fusion_on_a_hand_checked_example():
    # Ranks start at 1 and k is 60: an item scores the sum of 1 / (60 + rank) over the lists.
    #   10: rank 1 in the first, rank 3 in the second -> 1/61 + 1/63
    #   20: rank 2 in the first, rank 1 in the second -> 1/62 + 1/61
    #   30: rank 3 in the first only                  -> 1/63
    #   40: rank 2 in the second only                 -> 1/62
    fused = rrf([10, 20, 30], [20, 40, 10])
    expected = {
        10: Fraction(1, 61) + Fraction(1, 63),
        20: Fraction(1, 62) + Fraction(1, 61),
        30: Fraction(1, 63),
        40: Fraction(1, 62),
    }
    assert [item for item, _ in fused] == [20, 10, 40, 30]
    for item, score in fused:
        assert score == pytest.approx(float(expected[item]), rel=1e-12)
    assert RRF_K == 60


def test_equal_scores_keep_the_order_of_first_appearance():
    assert [i for i, _ in rrf([1, 2], [3, 4])] == [1, 3, 2, 4]  # 1 and 3 tie, then 2 and 4
    assert [i for i, _ in rrf([5], [5])] == [5]


def test_the_result_is_cut_to_n_and_either_list_may_be_empty():
    assert [i for i, _ in rrf(list(range(10)), [], n=3)] == [0, 1, 2]
    assert rrf([], []) == []
    only_second = rrf([], [7, 8])
    assert [i for i, _ in only_second] == [7, 8]
    assert only_second[0][1] == 1.0 / 61


def test_k_changes_how_much_the_top_ranks_dominate():
    flat = dict(rrf([1, 2], [2, 1], k=1000))
    steep = dict(rrf([1, 2], [2, 1], k=1))
    assert flat[1] == flat[2] and steep[1] == steep[2]  # symmetric lists tie either way
    assert dict(rrf([1, 2], [1], k=1))[1] > dict(rrf([1, 2], [1], k=1000))[1]
