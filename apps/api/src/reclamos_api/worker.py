import asyncio
import os

import asyncpg
from rqueue import Queue, Worker

from reclamos_api.config import Settings


async def wait_for_queue_schema(
    pool: asyncpg.Pool, *, queue_schema: str, attempts: int = 30
) -> None:
    for attempt in range(attempts):
        async with pool.acquire() as connection:
            ready = bool(
                await connection.fetchval(
                    "SELECT to_regclass($1) IS NOT NULL",
                    f"{queue_schema}.jobs",
                )
            )
        if ready:
            return
        if attempt < attempts - 1:
            await asyncio.sleep(1)
    raise RuntimeError(
        f"queue schema {queue_schema!r} was not ready after {attempts} attempts"
    )


async def run_worker() -> None:
    settings = Settings.from_environment()
    pool = await asyncpg.create_pool(settings.database_url)
    try:
        await wait_for_queue_schema(pool, queue_schema=settings.queue_schema)
        queue = Queue(pool, name="reclamos", schema=settings.queue_schema)

        @queue.task(name="noop")
        async def noop(payload: object, context: object) -> None:
            del payload, context

        await Worker(
            queue,
            worker_id=os.environ.get("WORKER_ID", "reclamos-worker"),
        ).run()
    finally:
        await pool.close()


if __name__ == "__main__":
    asyncio.run(run_worker())
