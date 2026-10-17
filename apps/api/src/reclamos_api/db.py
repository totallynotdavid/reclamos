from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from importlib.resources import files
from typing import Any

import asyncpg
from rqueue.migrations import migrate  # pyright: ignore[reportMissingTypeStubs]

SCHEMA_SQL = files("reclamos_api").joinpath("schema.sql")


async def prepare_database(connection: Any, *, queue_schema: str) -> None:
    """Apply the app schema and explicitly install rqueue's packaged schema."""
    await connection.execute(SCHEMA_SQL.read_text(encoding="utf-8"))
    await migrate(connection, schema=queue_schema)


@asynccontextmanager
async def database_pool(database_url: str, *, queue_schema: str) -> AsyncGenerator[Any]:
    pool = await asyncpg.create_pool(database_url, min_size=1, max_size=5)
    try:
        async with pool.acquire() as connection:
            await prepare_database(connection, queue_schema=queue_schema)
        yield pool
    finally:
        await pool.close()
