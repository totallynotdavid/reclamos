import argparse
import asyncio
from collections.abc import Sequence
from datetime import date, datetime
from zoneinfo import ZoneInfo

from rqueue import Queue, Worker

from reclamos_api.config import Settings
from reclamos_api.corpus.jobs import QUEUE_NAME, Pipeline
from reclamos_api.corpus.site import Fetcher
from reclamos_api.corpus.stats import collect_stats, format_stats
from reclamos_api.corpus.store import store_from_settings
from reclamos_api.db import database_pool

FIRST_YEAR = date(2020, 1, 1)
LIMA = ZoneInfo("America/Lima")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="reclamos")
    commands = parser.add_subparsers(dest="command", required=True)
    corpus = commands.add_parser("corpus", help="the TRASU resolution corpus")
    actions = corpus.add_subparsers(dest="action", required=True)

    crawl = actions.add_parser(
        "crawl", help="discover, download, extract and parse resolutions"
    )
    crawl.add_argument(
        "--since",
        type=date.fromisoformat,
        default=FIRST_YEAR,
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
    actions.add_parser("stats", help="print what the corpus holds")
    return parser


async def crawl(
    settings: Settings,
    fetcher: Fetcher,
    *,
    since: date,
    until: date,
    concurrency: int,
) -> None:
    async with database_pool(
        settings.database_url, queue_schema=settings.queue_schema
    ) as pool:
        queue = Queue(pool, name=QUEUE_NAME, schema=settings.queue_schema)
        pipeline = Pipeline(pool, queue, store_from_settings(settings), fetcher)
        pipeline.register()
        await pipeline.enqueue_discovery(since, until)
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
    arguments: argparse.Namespace, settings: Settings, fetcher: Fetcher | None
) -> str:
    if arguments.action == "crawl":
        fetcher = fetcher or Fetcher.create()
        try:
            await crawl(
                settings,
                fetcher,
                since=arguments.since,
                until=arguments.until or datetime.now(LIMA).date(),
                concurrency=arguments.concurrency,
            )
        finally:
            await fetcher.aclose()
    return await stats(settings)


def main(argv: Sequence[str] | None = None, *, fetcher: Fetcher | None = None) -> None:
    """Run a `reclamos` command. `fetcher` replaces the HTTP client for crawls."""
    arguments = build_parser().parse_args(argv)
    print(asyncio.run(run(arguments, Settings.from_environment(), fetcher)))


if __name__ == "__main__":
    main()
