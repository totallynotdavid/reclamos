import pytest
from fastapi.testclient import TestClient
from reclamos_api.main import app
from testcontainers.community.postgres import PostgresContainer


@pytest.mark.integration
def test_health_reports_database_and_queue(
    docker_required: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    with PostgresContainer("pgvector/pgvector:pg18") as postgres:
        monkeypatch.setenv(
            "DATABASE_URL",
            postgres.get_connection_url().replace(
                "postgresql+psycopg2", "postgresql"
            ),
        )
        with TestClient(app) as client:
            response = client.get("/health")

        assert response.status_code == 200
        assert response.json() == {
            "status": "ok",
            "database": {"ok": True},
            "queue": {"ok": True, "schema": "task_queue"},
        }
