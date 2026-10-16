from typing import Any

import asyncpg


async def dependency_health(pool: Any, *, queue_schema: str) -> dict[str, Any]:
    database_ok = False
    queue_ok = False
    async with pool.acquire() as connection:
        try:
            await connection.fetchval("SELECT 1")
            database_ok = True
        except asyncpg.PostgresError:
            database_ok = False
        try:
            queue_ok = bool(
                await connection.fetchval(
                    "SELECT to_regclass($1) IS NOT NULL",
                    f"{queue_schema}.jobs",
                )
            )
        except asyncpg.PostgresError:
            queue_ok = False
    return {
        "status": "ok" if database_ok and queue_ok else "degraded",
        "database": {"ok": database_ok},
        "queue": {"ok": queue_ok, "schema": queue_schema},
    }
