"""Run a judged question set against the search of a database."""

import subprocess
from collections.abc import Sequence
from pathlib import Path
from time import perf_counter
from typing import Any

from reclamos_eval.questions import Question, locate
from reclamos_eval.report import MODES, Attempt, Result
from reclamos_eval.retrieval import distinct

from reclamos_api.retrieval.embedder import Embedder
from reclamos_api.retrieval.search import Hit, Mode, search

# Enough chunks to usually include the ten best distinct resolutions, and the
# most `POST /search` returns.
CHUNKS_PER_QUERY = 50


class DatasetError(Exception):
    """The questions do not fit the database they are run against."""


async def measure(
    pool: Any, embedder: Embedder, questions: Sequence[Question]
) -> list[Result]:
    """Search every question in every mode and time each search. The database
    must hold the resolutions the questions name and a vector for every chunk."""
    claim_types = await check_questions(pool, questions)
    # The first embedding loads the model. Keep that out of the search latency.
    await embedder.embed_query(questions[0].query)
    results: list[Result] = []
    for question in questions:
        attempts: dict[str, Attempt] = {}
        for mode in MODES:
            started = perf_counter()
            hits = await search(
                pool, embedder, query=question.query, k=CHUNKS_PER_QUERY, mode=mode
            )
            seconds = perf_counter() - started
            attempts[mode] = Attempt(
                ranked=tuple(distinct(hit.resolution_id for hit in hits)),
                seconds=seconds,
                top_score=_top_score(mode, hits),
            )
        claim_type = (
            claim_types.get(question.relevant[0]) if question.relevant else None
        )
        results.append(Result(question, claim_type, attempts))
    return results


def _top_score(mode: Mode, hits: Sequence[Hit]) -> float | None:
    """The score of the best hit on the scale of its own ranker. A fused score
    only reflects ranks, so the hybrid mode has none."""
    if not hits:
        return None
    if mode == "vector":
        return hits[0].vector_score
    if mode == "text":
        return hits[0].text_score
    return None


async def check_questions(pool: Any, questions: Sequence[Question]) -> dict[int, str]:
    """Check the judgments against the database and return the claim type of each
    relevant resolution. Raises `DatasetError` for a missing resolution, a quote
    that is not in the stored text, or a chunk without a vector."""
    wanted = sorted({i for q in questions for i in q.relevant})
    async with pool.acquire() as connection:
        rows = await connection.fetch(
            "SELECT id, claim_type, body FROM resolutions WHERE id = ANY($1)", wanted
        )
        unembedded = await connection.fetchval(
            "SELECT count(*) FROM chunks WHERE embedding IS NULL"
        )
    found = {row["id"]: row for row in rows}
    problems = [
        f"{q.id} names resolution {i}, which the database lacks"
        for q in questions
        for i in sorted(q.relevant)
        if i not in found
    ]
    for question in questions:
        for item in question.evidence:
            row = found.get(item.resolution_id)
            if (
                row is not None
                and row["body"]
                and locate(row["body"], item.quote) is None
            ):
                problems.append(
                    f"{question.id}: the quote {item.quote[:60]!r} is not in "
                    f"resolution {item.resolution_id}"
                )
    if unembedded:
        problems.append(f"{unembedded} chunks have no vector; finish the crawl first")
    if problems:
        raise DatasetError("\n".join(problems))
    return {i: row["claim_type"] for i, row in found.items()}


async def corpus_counts(pool: Any) -> dict[str, Any]:
    async with pool.acquire() as connection:
        totals = await connection.fetchrow(
            """
            SELECT (SELECT count(*) FROM resolutions) AS resolutions,
                   (SELECT count(DISTINCT resolution_id) FROM chunks) AS indexed,
                   (SELECT count(*) FROM chunks) AS chunks,
                   (SELECT count(embedding) FROM chunks) AS embedded_chunks,
                   (SELECT model FROM embedding_model) AS model
            """
        )
        claim_types = await connection.fetch(
            "SELECT claim_type, count(*) FROM resolutions "
            "WHERE claim_type IS NOT NULL AND id IN (SELECT resolution_id FROM chunks) "
            "GROUP BY 1 ORDER BY 2 DESC, 1"
        )
        outcomes = await connection.fetch(
            "SELECT outcome, count(*) FROM resolutions "
            "WHERE outcome IS NOT NULL AND id IN (SELECT resolution_id FROM chunks) "
            "GROUP BY 1 ORDER BY 2 DESC, 1"
        )
    return {
        "resolutions": totals["resolutions"],
        "indexed": totals["indexed"],
        "chunks": totals["chunks"],
        "embedded_chunks": totals["embedded_chunks"],
        "claim_types": {row[0]: row[1] for row in claim_types},
        "outcomes": {row[0]: row[1] for row in outcomes},
    }


def git_state() -> dict[str, Any] | None:
    """The commit this code is at and whether files differ from it, or None
    outside a git checkout."""
    here = Path(__file__).resolve().parent
    try:
        sha = _git(here, "rev-parse", "HEAD")
        dirty = bool(_git(here, "status", "--porcelain"))
    except (OSError, subprocess.CalledProcessError):
        return None
    return {"sha": sha, "dirty": dirty}


def _git(directory: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", "-C", str(directory), *arguments],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
