"""Retrieval metrics over a judged query set.

A judgment names the resolutions that answer a query. Results come back as
chunks, so a ranking is first reduced to the distinct resolutions in order of
their best chunk.
"""

import json
from collections.abc import Callable, Collection, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class Judgment:
    id: str
    query: str
    relevant: frozenset[int]


@dataclass(frozen=True, slots=True)
class Scores:
    recall: float
    mrr: float
    queries: int


def load_judgments(path: Path) -> list[Judgment]:
    """Read a JSON Lines file with `id`, `query` and `relevant` (resolution ids)."""
    judgments: list[Judgment] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        record = json.loads(line)
        relevant = frozenset(record["relevant"])
        if not relevant:
            raise ValueError(f"{path}:{number}: a query needs a relevant resolution")
        judgments.append(Judgment(record["id"], record["query"], relevant))
    return judgments


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


def evaluate(
    judgments: Sequence[Judgment],
    search: Callable[[str], Sequence[int]],
    *,
    k: int = 5,
) -> Scores:
    """Mean recall@k and MRR of `search`, which maps a query to ranked resolution ids."""
    if not judgments:
        raise ValueError("no judgments")
    recalls: list[float] = []
    reciprocals: list[float] = []
    for judgment in judgments:
        ranked = search(judgment.query)
        recalls.append(recall_at_k(judgment.relevant, ranked, k))
        reciprocals.append(reciprocal_rank(judgment.relevant, ranked))
    return Scores(
        recall=sum(recalls) / len(recalls),
        mrr=sum(reciprocals) / len(reciprocals),
        queries=len(judgments),
    )
