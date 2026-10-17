import math

import pytest
from reclamos_eval.retrieval import ndcg_at_k, recall_at_k, reciprocal_rank, score


def test_recall_counts_distinct_resolutions_in_the_first_k() -> None:
    # Three chunks of resolution 1 take one slot, so 3 is still inside the top 3.
    ranked = [1, 1, 1, 2, 3]
    assert recall_at_k({3, 9}, ranked, 3) == 0.5
    assert recall_at_k({3, 9}, ranked, 2) == 0.0


def test_reciprocal_rank_is_the_first_relevant_resolution() -> None:
    assert reciprocal_rank({2}, [1, 1, 2, 2]) == 0.5
    assert reciprocal_rank({7}, [1, 2]) == 0.0


def test_ndcg_rewards_relevant_resolutions_near_the_top() -> None:
    assert ndcg_at_k({1, 2}, [1, 2, 3], 10) == 1.0
    # One of two relevant resolutions, at rank 2: 1/log2(3) over 1 + 1/log2(3).
    third = 1 / math.log2(3)
    assert ndcg_at_k({1, 2}, [9, 1, 3], 10) == pytest.approx(third / (1 + third))
    assert ndcg_at_k({1}, [9, 8, 1], 2) == 0.0


def test_a_question_nothing_answers_has_no_score() -> None:
    with pytest.raises(ValueError, match="relevant"):
        score(set(), [1, 2])
