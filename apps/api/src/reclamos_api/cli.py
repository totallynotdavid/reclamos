import argparse
import asyncio
from collections.abc import Sequence
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from reclamos_eval.gate import BASELINE, SUBSET
from reclamos_eval.questions import QUESTIONS, QuestionFileError
from rqueue import Queue, Worker  # pyright: ignore[reportMissingTypeStubs]

from reclamos_api.config import Settings
from reclamos_api.corpus import repository
from reclamos_api.corpus.jobs import QUEUE_NAME, Pipeline
from reclamos_api.corpus.site import Fetcher
from reclamos_api.corpus.stats import collect_stats, format_stats
from reclamos_api.corpus.store import store_from_settings
from reclamos_api.db import database_pool
from reclamos_api.evaluation import commands as evaluation
from reclamos_api.evaluation.run import DatasetError
from reclamos_api.retrieval.embedder import Embedder, embedder_from_settings

FIRST_PUBLISHED = date(2003, 1, 1)
LIMA = ZoneInfo("America/Lima")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="reclamos")
    commands = parser.add_subparsers(dest="command", required=True)
    corpus = commands.add_parser("corpus", help="the TRASU resolution corpus")
    actions = corpus.add_subparsers(dest="action", required=True)

    crawl = actions.add_parser(
        "crawl",
        help="discover, download, extract, parse, chunk and embed resolutions",
    )
    crawl.add_argument(
        "--since",
        type=date.fromisoformat,
        default=FIRST_PUBLISHED,
        help="first publication date to list, YYYY-MM-DD (default: %(default)s)",
    )
    crawl.add_argument(
        "--until",
        type=date.fromisoformat,
        default=None,
        help="last publication date to list (default: today in Lima)",
    )
    crawl.add_argument(
        "--concurrency",
        type=int,
        default=4,
        help="jobs to run at once; requests stay one per second (default: %(default)s)",
    )
    reindex = actions.add_parser(
        "reindex", help="rebuild the search index from the stored text"
    )
    reindex.add_argument(
        "what",
        choices=("chunks", "embeddings"),
        help="chunks splits and embeds again; embeddings only embeds again",
    )
    reindex.add_argument("--concurrency", type=int, default=4)
    actions.add_parser("stats", help="print what the corpus holds")

    scoring = commands.add_parser(
        "eval", help="score the judged questions against the corpus database"
    )
    scoring.add_argument("--questions", type=Path, default=QUESTIONS)
    scoring.add_argument(
        "--output",
        type=Path,
        default=evaluation.DEFAULT_OUTPUT,
        help="where to write the results as JSON (default: %(default)s)",
    )

    gate = commands.add_parser("gate", help="the CI regression gate")
    steps = gate.add_subparsers(dest="action", required=True)
    for name, help_text in (
        (
            "check",
            "fail when fused recall@5 on the fixed index fell below the baseline",
        ),
        ("baseline", "measure the fixed index and rewrite the baseline file"),
        ("subset", "rebuild the fixed index from the corpus database"),
    ):
        step = steps.add_parser(name, help=help_text)
        step.add_argument("--questions", type=Path, default=QUESTIONS)
        step.add_argument("--subset", type=Path, default=SUBSET)
        if name == "subset":
            step.add_argument(
                "--chunks",
                type=int,
                default=evaluation.DEFAULT_CHUNK_BUDGET,
                help="chunks to keep, at least the answers (default: %(default)s)",
            )
        else:
            step.add_argument("--baseline", type=Path, default=BASELINE)
    return parser


async def crawl(
    settings: Settings,
    fetcher: Fetcher,
    embedder: Embedder,
    *,
    window: tuple[date, date] | None,
    reset: str | None = None,
    concurrency: int,
) -> None:
    """Run every job until the queue is empty. `window` lists that publication
    range first; `reset` forgets "chunks" or "embeddings" so they are redone."""
    async with database_pool(
        settings.database_url, queue_schema=settings.queue_schema
    ) as pool:
        queue = Queue(pool, name=QUEUE_NAME, schema=settings.queue_schema)
        pipeline = Pipeline(
            pool, queue, store_from_settings(settings), fetcher, embedder
        )
        pipeline.register()
        if reset is not None:
            async with pool.acquire() as connection:
                if reset == "chunks":
                    await repository.reset_chunks(connection)
                else:
                    await repository.reset_embeddings(connection, embedder.model)
        if window is not None:
            await pipeline.enqueue_discovery(*window)
        await pipeline.enqueue_unfinished()
        worker = Worker(queue, worker_id="reclamos-crawl", concurrency=concurrency)
        while True:
            await worker.drain()
            # A failed attempt waits out its backoff, so drain can return while
            # retries are still scheduled.
            if (await queue.stats()).depth == 0:
                break
            await asyncio.sleep(1)


async def stats(settings: Settings) -> str:
    async with database_pool(
        settings.database_url, queue_schema=settings.queue_schema
    ) as pool:
        return format_stats(
            await collect_stats(pool, queue_schema=settings.queue_schema)
        )


async def run(
    arguments: argparse.Namespace,
    settings: Settings,
    fetcher: Fetcher | None,
    embedder: Embedder | None,
) -> str:
    if arguments.command == "eval":
        return await evaluation.run_eval(
            settings,
            embedder or embedder_from_settings(settings),
            questions_path=arguments.questions,
            output=arguments.output,
        )
    if arguments.command == "gate":
        return await run_gate(arguments, settings, embedder)
    if arguments.action in ("crawl", "reindex"):
        fetcher = fetcher or Fetcher.create()
        embedder = embedder or embedder_from_settings(settings)
        crawling = arguments.action == "crawl"
        try:
            await crawl(
                settings,
                fetcher,
                embedder,
                window=(
                    (arguments.since, arguments.until or datetime.now(LIMA).date())
                    if crawling
                    else None
                ),
                reset=None if crawling else arguments.what,
                concurrency=arguments.concurrency,
            )
        finally:
            await fetcher.aclose()
    return await stats(settings)


async def run_gate(
    arguments: argparse.Namespace, settings: Settings, embedder: Embedder | None
) -> str:
    """`check` and `baseline` need no model: the fixed index holds its vectors."""
    if arguments.action == "subset":
        return await evaluation.gate_subset(
            settings,
            embedder or embedder_from_settings(settings),
            questions_path=arguments.questions,
            subset_path=arguments.subset,
            chunk_budget=arguments.chunks,
        )
    step = (
        evaluation.gate_check
        if arguments.action == "check"
        else evaluation.gate_baseline
    )
    return await step(
        settings,
        questions_path=arguments.questions,
        subset_path=arguments.subset,
        baseline_path=arguments.baseline,
    )


def main(
    argv: Sequence[str] | None = None,
    *,
    fetcher: Fetcher | None = None,
    embedder: Embedder | None = None,
) -> None:
    """Run a `reclamos` command. `fetcher` and `embedder` replace the HTTP
    client and the embedding model."""
    arguments = build_parser().parse_args(argv)
    try:
        print(
            asyncio.run(run(arguments, Settings.from_environment(), fetcher, embedder))
        )
    except (DatasetError, QuestionFileError) as error:
        raise SystemExit(str(error)) from error


if __name__ == "__main__":
    main()
