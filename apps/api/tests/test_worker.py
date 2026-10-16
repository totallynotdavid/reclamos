import asyncio

import asyncpg
import pytest
from reclamos_api.worker import wait_for_queue_schema
from testcontainers.community.postgres import PostgresContainer


@pytest.mark.integration
def test_worker_waits_for_queue_schema(docker_required: None) -> None:
    with PostgresContainer("pgvector/pgvector:pg18") as postgres:
        database_url = postgres.get_connection_url().replace(
            "postgresql+psycopg2", "postgresql"
        )

        async def scenario() -> None:
            pool = await asyncpg.create_pool(database_url)
            try:
                async def create_queue_table() -> None:
                    await asyncio.sleep(0.1)
                    async with pool.acquire() as connection:
                        await connection.execute("CREATE SCHEMA task_queue")
                        await connection.execute(
                            "CREATE TABLE task_queue.jobs (id integer)"
                        )

                creator = asyncio.create_task(create_queue_table())
                await wait_for_queue_schema(
                    pool, queue_schema="task_queue", attempts=3
                )
                await creator

                async with pool.acquire() as connection:
                    await connection.execute("DROP SCHEMA task_queue CASCADE")
                with pytest.raises(RuntimeError, match="was not ready"):
                    await wait_for_queue_schema(
                        pool, queue_schema="task_queue", attempts=2
                    )
            finally:
                await pool.close()

        asyncio.run(scenario())
