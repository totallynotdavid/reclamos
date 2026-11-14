"""Find the resolutions most like a visitor's description, with their cards.

Nothing here calls a language model: the cards were written and judged offline,
so a visitor's request costs a search and a read.
"""

from collections import Counter
from dataclasses import dataclass
from typing import Any

from reclamos_api.cards import repository
from reclamos_api.cards.card import Field
from reclamos_api.retrieval.embedder import Embedder
from reclamos_api.retrieval.search import NO_FILTERS, Filters, Hit, search

# Enough chunks to usually hold the ten best distinct resolutions.
CHUNKS = 50
DEFAULT_CASES = 5
MAX_CASES = 10


@dataclass(slots=True)
class Case:
    # The resolution's best passage, which carries its parse fields.
    passage: Hit
    similarity: float
    card: dict[str, Field] | None


async def find_cases(
    pool: Any,
    embedder: Embedder,
    *,
    query: str,
    limit: int = DEFAULT_CASES,
    min_similarity: float,
    filters: Filters = NO_FILTERS,
) -> list[Case]:
    """The best `limit` resolutions with a passage at least `min_similarity`
    similar to `query`, best first. An empty list means no similar case."""
    hits = await search(pool, embedder, query=query, k=CHUNKS, filters=filters)
    cases = similar_cases(hits, limit=limit, min_similarity=min_similarity)
    if cases:
        async with pool.acquire() as connection:
            fields = await repository.served_fields(
                connection, [case.passage.resolution_id for case in cases]
            )
        for case in cases:
            case.card = fields.get(case.passage.resolution_id)
    return cases


def similar_cases(hits: list[Hit], *, limit: int, min_similarity: float) -> list[Case]:
    """One case per resolution, in the order of its best-ranked hit. A
    resolution's similarity is the best cosine similarity of its hits. The fused
    rank orders the cases, and the similarity decides whether one is shown."""
    best: dict[int, Case] = {}
    for hit in hits:
        similarity = hit.vector_score
        case = best.get(hit.resolution_id)
        if case is None:
            best[hit.resolution_id] = Case(hit, similarity or 0.0, None)
        elif similarity is not None and similarity > case.similarity:
            case.similarity = similarity
    kept = [case for case in best.values() if case.similarity >= min_similarity]
    return kept[:limit]


def outcome_counts(cases: list[Case]) -> dict[str, int]:
    """How many of the cases ended in each outcome, most common first. A case
    whose outcome parse could not read counts under "unknown"."""
    counts = Counter(case.passage.fields["outcome"] or "unknown" for case in cases)
    return dict(counts.most_common())
