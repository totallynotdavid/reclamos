import pytest
from fastapi.testclient import TestClient
from reclamos_api.main import app


@pytest.mark.integration
def test_health_reports_database_and_queue(
    database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DATABASE_URL", database_url)
    with TestClient(app) as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "database": {"ok": True},
        "queue": {"ok": True, "schema": "task_queue"},
    }
