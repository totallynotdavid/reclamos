"""The `reclamos eval` and `reclamos gate` commands."""

import json
import uuid
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import asyncpg
from reclamos_eval.gate import Baseline, check, read_baseline, write_baseline
from reclamos_eval.questions import QUESTIONS, Question, load_questions
from reclamos_eval.report import Result, format_table, summarize

from reclamos_api.config import Settings
from reclamos_api.db import database_pool
from reclamos_api.evaluation.build import build_subset, gate_questions
from reclamos_api.evaluation.run import corpus_counts, git_state, measure
from reclamos_api.evaluation.subset import (
    PrecomputedEmbedder,
    Subset,
    load_subset,
    read_subset,
    write_subset,
)
from reclamos_api.retrieval.embedder import Embedder

DEFAULT_OUTPUT = Path(".data/eval.json")
DEFAULT_CHUNK_BUDGET = 800


async def run_eval(
    settings: Settings,
    embedder: Embedder,
    *,
    questions_path: Path = QUESTIONS,
    output: Path = DEFAULT_OUTPUT,
) -> str:
    """Score the question set against the database in `settings`, write the
    results as JSON to `output` and return the table."""
    questions = load_questions(questions_path)
    async with database_pool(
        settings.database_url, queue_schema=settings.queue_schema
    ) as pool:
        results = await measure(pool, embedder, questions)
        corpus = await corpus_counts(pool)
    summary = summarize(results)
    report = {
        "date": datetime.now(UTC).isoformat(timespec="seconds"),
        "commit": git_state(),
        "model": embedder.model,
        "corpus": corpus,
    } | summary
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    return f"{format_table(summary)}\n\nresults written to {output}"


@asynccontextmanager
async def scratch_database(server_url: str) -> AsyncGenerator[str]:
    """A new empty database on the server, dropped on exit. Nothing already on
    the server is touched."""
    name = f"reclamos_gate_{uuid.uuid4().hex}"
    admin = await asyncpg.connect(server_url)
    try:
        await admin.execute(f'CREATE DATABASE "{name}"')
        try:
            yield urlunsplit(urlsplit(server_url)._replace(path=f"/{name}"))
        finally:
            await admin.execute(f'DROP DATABASE "{name}" WITH (FORCE)')
    finally:
        await admin.close()


async def measure_gate(
    settings: Settings, questions: list[Question], subset: Subset
) -> tuple[list[Result], Baseline]:
    """Search the gate's questions in a database that holds only the subset."""
    asked = [q for q in gate_questions(questions) if q.id in set(subset.questions)]
    if {q.id for q in asked} != set(subset.questions):
        missing = sorted(set(subset.questions) - {q.id for q in asked})
        raise SystemExit(
            f"the subset asks questions the file no longer has: {', '.join(missing)}; "
            "rebuild it with `reclamos gate subset`"
        )
    async with (
        scratch_database(settings.database_url) as url,
        database_pool(url, queue_schema=settings.queue_schema) as pool,
    ):
        async with pool.acquire() as connection:
            await load_subset(connection, subset)
        results = await measure(pool, PrecomputedEmbedder(subset), asked)
    recall = summarize(results)["modes"]["hybrid"]["all"]["recall_at_5"]
    return results, Baseline(recall, len(asked))


async def gate_check(
    settings: Settings, *, questions_path: Path, subset_path: Path, baseline_path: Path
) -> str:
    """Measure the gate and compare it with the baseline. Exits non-zero when
    recall fell too far. Never writes the baseline."""
    results, measured = await measure_gate(
        settings, load_questions(questions_path), read_subset(subset_path)
    )
    verdict = check(measured, read_baseline(baseline_path))
    report = f"{format_table(summarize(results))}\n\n{verdict.message}"
    if not verdict.passed:
        raise SystemExit(report)
    return report


async def gate_baseline(
    settings: Settings, *, questions_path: Path, subset_path: Path, baseline_path: Path
) -> str:
    """Measure the gate and write the result as the new baseline."""
    _, measured = await measure_gate(
        settings, load_questions(questions_path), read_subset(subset_path)
    )
    write_baseline(measured, baseline_path)
    return (
        f"baseline {baseline_path} is now fused recall@5 {measured.recall_at_5:.4f} "
        f"over {measured.questions} questions"
    )


async def gate_subset(
    settings: Settings,
    embedder: Embedder,
    *,
    questions_path: Path,
    subset_path: Path,
    chunk_budget: int,
) -> str:
    """Rebuild the fixed index from the corpus the database holds."""
    questions = load_questions(questions_path)
    async with database_pool(
        settings.database_url, queue_schema=settings.queue_schema
    ) as pool:
        subset = await build_subset(
            pool, embedder, questions, chunk_budget=chunk_budget
        )
    subset_path.parent.mkdir(parents=True, exist_ok=True)
    write_subset(subset_path, subset)
    return (
        f"{subset_path}: {len(subset.resolutions)} resolutions, "
        f"{len(subset.chunks)} chunks, {len(subset.questions)} questions\n"
        f"run `reclamos gate baseline` to measure it, then commit both files"
    )
