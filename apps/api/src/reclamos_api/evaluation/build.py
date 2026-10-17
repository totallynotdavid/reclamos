"""Choose the chunks of the CI gate's fixed index from the real corpus."""

import hashlib
import json
from collections.abc import Sequence
from typing import Any

from reclamos_eval.questions import TUNE, Question, locate

from reclamos_api.evaluation.run import DatasetError, check_questions
from reclamos_api.evaluation.subset import CHUNK_COLUMNS, RESOLUTION_COLUMNS, Subset
from reclamos_api.retrieval.embedder import Embedder
from reclamos_api.retrieval.search import search

# Chunks each single ranker puts first for a question. They are the passages
# the gate's questions are most likely to confuse with the right ones.
RIVALS_PER_RANKER = 4

Key = tuple[int, int]  # resolution id, ordinal


def gate_questions(questions: Sequence[Question]) -> list[Question]:
    """The questions the gate asks: answerable and never held out, so a change
    that only pleases the gate cannot also tune on the held-out split."""
    return [q for q in questions if q.answerable and q.split == TUNE]


async def build_subset(
    pool: Any,
    embedder: Embedder,
    questions: Sequence[Question],
    *,
    chunk_budget: int,
) -> Subset:
    """The chunks that answer each gate question, the chunks its rankers prefer
    instead, then chunks picked by a fixed hash until `chunk_budget` is reached.
    The answers always stay, so the subset can be larger than the budget."""
    asked = gate_questions(questions)
    await check_questions(pool, asked)
    chosen = await _answers(pool, asked)
    for key in await _rivals(pool, embedder, asked):
        if len(chosen) >= chunk_budget:
            break
        chosen.add(key)
    if len(chosen) < chunk_budget:
        chosen |= await _filler(pool, chosen, chunk_budget - len(chosen))
    subset = Subset(embedder.model, [q.id for q in asked])
    async with pool.acquire() as connection:
        resolution_ids = sorted({resolution_id for resolution_id, _ in chosen})
        for row in await connection.fetch(
            f"SELECT {', '.join(RESOLUTION_COLUMNS)} FROM resolutions "
            "WHERE id = ANY($1) ORDER BY id",
            resolution_ids,
        ):
            subset.resolutions.append(dict(row))
        for row in await connection.fetch(
            f"SELECT {', '.join(CHUNK_COLUMNS)}, embedding::text AS embedding "
            "FROM chunks WHERE resolution_id = ANY($1) ORDER BY resolution_id, ordinal",
            resolution_ids,
        ):
            if (row["resolution_id"], row["ordinal"]) in chosen:
                subset.chunks.append(
                    dict(row) | {"embedding": json.loads(row["embedding"])}
                )
    for question in asked:
        subset.queries[question.query] = await embedder.embed_query(question.query)
    return subset


async def _answers(pool: Any, questions: Sequence[Question]) -> set[Key]:
    """The chunks that overlap a quoted passage."""
    chosen: set[Key] = set()
    async with pool.acquire() as connection:
        for question in questions:
            for item in question.evidence:
                body = await connection.fetchval(
                    "SELECT body FROM resolutions WHERE id = $1", item.resolution_id
                )
                span = locate(body, item.quote)
                if span is None:
                    raise DatasetError(
                        f"{question.id}: a quote is not in resolution {item.resolution_id}"
                    )
                rows = await connection.fetch(
                    "SELECT ordinal FROM chunks WHERE resolution_id = $1 "
                    "AND char_start < $3 AND char_end > $2",
                    item.resolution_id,
                    *span,
                )
                if not rows:
                    raise DatasetError(
                        f"{question.id}: no chunk of resolution {item.resolution_id} "
                        "holds its quote"
                    )
                chosen |= {(item.resolution_id, row["ordinal"]) for row in rows}
    return chosen


async def _rivals(
    pool: Any, embedder: Embedder, questions: Sequence[Question]
) -> list[Key]:
    """Rival chunks, best first across all questions, so a short budget keeps
    every question's closest rivals before anyone's fourth."""
    ranked: list[list[Key]] = []
    for question in questions:
        for mode in ("vector", "text"):
            hits = await search(
                pool, embedder, query=question.query, k=RIVALS_PER_RANKER, mode=mode
            )
            ranked.append([(hit.resolution_id, hit.ordinal) for hit in hits])
    keys: list[Key] = []
    for place in range(RIVALS_PER_RANKER):
        keys += [ranking[place] for ranking in ranked if place < len(ranking)]
    return list(dict.fromkeys(keys))


async def _filler(pool: Any, taken: set[Key], count: int) -> set[Key]:
    async with pool.acquire() as connection:
        rows = await connection.fetch("SELECT resolution_id, ordinal FROM chunks")
    others = [(r["resolution_id"], r["ordinal"]) for r in rows]
    others = [key for key in others if key not in taken]
    others.sort(key=lambda key: hashlib.sha256(f"{key[0]}:{key[1]}".encode()).digest())
    return set(others[:count])
