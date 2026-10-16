"""Search the chunks by meaning, by words, or by both.

Each ranker returns its best `CANDIDATES` chunks. Reciprocal rank fusion scores
a chunk by the sum of 1 / (RRF_K + rank) over the rankers that returned it, so
only the order of each list counts and the two score scales never mix.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Literal

from reclamos_api.corpus.repository import vector_literal
from reclamos_api.retrieval.embedder import Embedder

Mode = Literal["hybrid", "vector", "text"]

CANDIDATES = 100
RRF_K = 60


@dataclass(frozen=True, slots=True)
class Filters:
    claim_type: str | None = None
    operator: str | None = None
    outcome: str | None = None
    date_from: date | None = None
    date_to: date | None = None

    def where(self, first_parameter: int) -> tuple[str, list[Any]]:
        """An SQL condition over `r` (resolutions) and its parameters, numbered
        from `first_parameter`."""
        conditions: list[str] = []
        values: list[Any] = []
        for column, operator, value in (
            ("r.claim_type", "=", self.claim_type),
            ("r.operator", "=", self.operator),
            ("r.outcome", "=", self.outcome),
            ("r.resolved_on", ">=", self.date_from),
            ("r.resolved_on", "<=", self.date_to),
        ):
            if value is not None:
                values.append(value)
                conditions.append(
                    f"{column} {operator} ${first_parameter + len(values) - 1}"
                )
        return " AND ".join(conditions) or "true", values


@dataclass(slots=True)
class Hit:
    resolution_id: int
    ordinal: int
    score: float
    vector_score: float | None = None
    text_score: float | None = None
    fields: dict[str, Any] = field(default_factory=dict)


NO_FILTERS = Filters()


async def search(
    pool: Any,
    embedder: Embedder,
    *,
    query: str,
    k: int,
    mode: Mode = "hybrid",
    filters: Filters = NO_FILTERS,
) -> list[Hit]:
    vector_ranking: list[tuple[int, int, float]] = []
    text_ranking: list[tuple[int, int, float]] = []
    # Embedding can wait on the model's lock and its first load, so it runs
    # before a connection is taken: the pool is small and health checks use it.
    vector = await embedder.embed_query(query) if mode != "text" else None
    async with pool.acquire() as connection:
        if vector is not None:
            await _require_index_model(connection, embedder.model)
            vector_ranking = await _vector_ranking(connection, vector, filters)
        if mode in ("hybrid", "text"):
            text_ranking = await _text_ranking(connection, query, filters)
        fused = fuse(vector_ranking, text_ranking)[:k]
        await _attach_chunks(connection, fused)
    return fused


class IndexModelMismatch(Exception):
    """The query was embedded by a model other than the one that made the stored
    vectors, so their distances mean nothing."""


async def _require_index_model(connection: Any, model: str) -> None:
    held = await connection.fetchval("SELECT model FROM embedding_model")
    # No row yet: nothing has been embedded, so no vector can disagree.
    if held is not None and held != model:
        raise IndexModelMismatch(
            f"the index holds vectors of {held}, but this API embeds queries "
            f"with {model}"
        )


def fuse(
    vector_ranking: Sequence[tuple[int, int, float]],
    text_ranking: Sequence[tuple[int, int, float]],
) -> list[Hit]:
    """Reciprocal rank fusion of two rankings of (resolution id, ordinal, score)."""
    hits: dict[tuple[int, int], Hit] = {}
    for ranking, name in (
        (vector_ranking, "vector_score"),
        (text_ranking, "text_score"),
    ):
        for rank, (resolution_id, ordinal, score) in enumerate(ranking, start=1):
            hit = hits.setdefault(
                (resolution_id, ordinal), Hit(resolution_id, ordinal, score=0.0)
            )
            hit.score += 1 / (RRF_K + rank)
            setattr(hit, name, score)
    # Equal fused scores keep a stable order: earlier document, earlier passage.
    return sorted(hits.values(), key=lambda h: (-h.score, h.resolution_id, h.ordinal))


async def _vector_ranking(
    connection: Any, vector: Sequence[float], filters: Filters
) -> list[tuple[int, int, float]]:
    condition, values = filters.where(first_parameter=3)
    async with connection.transaction():
        # Without iterative scans a filter applied after the index could leave
        # fewer than CANDIDATES rows.
        await connection.execute("SET LOCAL hnsw.iterative_scan = strict_order")
        await connection.execute(f"SET LOCAL hnsw.ef_search = {CANDIDATES}")
        rows = await connection.fetch(
            f"""
            SELECT c.resolution_id, c.ordinal, 1 - (c.embedding <=> $1::vector) AS score
            FROM chunks c JOIN resolutions r ON r.id = c.resolution_id
            WHERE c.embedding IS NOT NULL AND {condition}
            ORDER BY c.embedding <=> $1::vector, c.resolution_id, c.ordinal
            LIMIT $2
            """,
            vector_literal(vector),
            CANDIDATES,
            *values,
        )
    return [(r["resolution_id"], r["ordinal"], r["score"]) for r in rows]


async def _text_ranking(
    connection: Any, query: str, filters: Filters
) -> list[tuple[int, int, float]]:
    condition, values = filters.where(first_parameter=3)
    # A question is not a list of required words, so any word may match and a
    # chunk with more of them ranks higher.
    rows = await connection.fetch(
        f"""
        WITH q AS (
            SELECT replace(
                plainto_tsquery('spanish_unaccent', $1)::text, ' & ', ' | '
            )::tsquery AS query
        )
        SELECT c.resolution_id, c.ordinal, ts_rank(c.search, q.query) AS score
        FROM chunks c
        JOIN resolutions r ON r.id = c.resolution_id
        CROSS JOIN q
        WHERE c.search @@ q.query AND {condition}
        ORDER BY score DESC, c.resolution_id, c.ordinal
        LIMIT $2
        """,
        query,
        CANDIDATES,
        *values,
    )
    return [(r["resolution_id"], r["ordinal"], r["score"]) for r in rows]


async def _attach_chunks(connection: Any, hits: list[Hit]) -> None:
    rows = await connection.fetch(
        """
        SELECT c.resolution_id, c.ordinal, c.section, c.page_start, c.page_end,
               c.char_start, c.char_end, c.text,
               r.expediente, r.number, r.resolved_on, r.operator, r.claim_type, r.outcome, r.page_url
        FROM unnest($1::bigint[], $2::integer[]) AS wanted (resolution_id, ordinal)
        JOIN chunks c USING (resolution_id, ordinal)
        JOIN resolutions r ON r.id = c.resolution_id
        """,
        [h.resolution_id for h in hits],
        [h.ordinal for h in hits],
    )
    by_key = {(r["resolution_id"], r["ordinal"]): dict(r) for r in rows}
    for hit in hits:
        hit.fields = by_key[(hit.resolution_id, hit.ordinal)]
