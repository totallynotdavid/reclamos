"""Retrieval metrics for one question.

Results come back as chunks, so a ranking is first reduced to the distinct
resolutions in order of their best chunk. Relevance is binary: a resolution
either answers the question or does not.
"""

import math
from collections.abc import Collection, Iterable, Sequence
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Scores:
    recall_at_5: float
    recall_at_10: float
    mrr: float
    ndcg_at_10: float


def distinct(ranked: Iterable[int]) -> list[int]:
    """Keep the first appearance of each resolution id."""
    return list(dict.fromkeys(ranked))


def recall_at_k(relevant: Collection[int], ranked: Sequence[int], k: int) -> float:
    """The share of relevant resolutions among the first `k` distinct ones."""
    if not relevant:
        raise ValueError("relevant must not be empty")
    found = set(distinct(ranked)[:k]) & set(relevant)
    return len(found) / len(relevant)


def reciprocal_rank(relevant: Collection[int], ranked: Sequence[int]) -> float:
    """1 / rank of the first relevant resolution, 0 when none appears."""
    for position, resolution_id in enumerate(distinct(ranked), 1):
        if resolution_id in relevant:
            return 1 / position
    return 0.0


def ndcg_at_k(relevant: Collection[int], ranked: Sequence[int], k: int) -> float:
    """Discounted gain of the first `k` distinct resolutions, over the best
    possible: every relevant resolution first."""
    if not relevant:
        raise ValueError("relevant must not be empty")
    gain = sum(
        1 / math.log2(position + 1)
        for position, resolution_id in enumerate(distinct(ranked)[:k], 1)
        if resolution_id in relevant
    )
    best = sum(
        1 / math.log2(position + 1) for position in range(1, min(k, len(relevant)) + 1)
    )
    return gain / best


def score(relevant: Collection[int], ranked: Sequence[int]) -> Scores:
    return Scores(
        recall_at_5=recall_at_k(relevant, ranked, 5),
        recall_at_10=recall_at_k(relevant, ranked, 10),
        mrr=reciprocal_rank(relevant, ranked),
        ndcg_at_10=ndcg_at_k(relevant, ranked, 10),
    )
