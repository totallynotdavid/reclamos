import asyncio
import os
import shutil
import subprocess
import uuid
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import asyncpg
import pytest
from fakeembed import HashingEmbedder
from reclamos_api.config import Settings
from test_search import seed_and_run
from testcontainers.community.postgres import PostgresContainer


def docker_daemon_available() -> bool:
    docker = shutil.which("docker")
    if docker is None:
        return False
    try:
        result = subprocess.run(
            [docker, "info"],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


@pytest.fixture(scope="session")
def postgres_server() -> Iterator[str]:
    """URL of a PostgreSQL server the tests may create databases on.

    TEST_DATABASE_URL names an existing server with the pgvector extension
    installed; otherwise a container starts.
    """
    configured = os.environ.get("TEST_DATABASE_URL")
    if configured:
        yield configured
        return
    if not docker_daemon_available():
        pytest.skip(
            "Set TEST_DATABASE_URL or run a Docker daemon to get a PostgreSQL server"
        )
    with PostgresContainer("pgvector/pgvector:pg18") as postgres:
        yield postgres.get_connection_url().replace("postgresql+psycopg2", "postgresql")


@pytest.fixture
def database_url(postgres_server: str) -> Iterator[str]:
    """An empty database that exists for one test."""
    name = f"test_{uuid.uuid4().hex}"

    async def run_admin(statement: str) -> None:
        connection = await asyncpg.connect(postgres_server)
        try:
            await connection.execute(statement)
        finally:
            await connection.close()

    asyncio.run(run_admin(f'CREATE DATABASE "{name}"'))
    parts = urlsplit(postgres_server)
    try:
        yield urlunsplit(parts._replace(path=f"/{name}"))
    finally:
        asyncio.run(run_admin(f'DROP DATABASE "{name}" WITH (FORCE)'))


@pytest.fixture
def embedder() -> HashingEmbedder:
    return HashingEmbedder()


@pytest.fixture
def settings(
    database_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Settings:
    monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.setenv("PDF_STORE_PATH", str(tmp_path / "pdfs"))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    return Settings.from_environment()


@pytest.fixture
def indexed(settings: Settings, embedder: HashingEmbedder, tmp_path: Path) -> Settings:
    """A database holding the three fixture resolutions, chunked and embedded."""
    asyncio.run(seed_and_run(settings, embedder, tmp_path / "pdfs"))
    return settings
