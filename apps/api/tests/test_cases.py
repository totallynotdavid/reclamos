import json
from typing import Any

import httpx2
import pytest
from fakeembed import HashingEmbedder
from fastapi.testclient import TestClient
from reclamos_api.cards.card import PROMPT_VERSION
from reclamos_api.cards.repository import body_sha256
from reclamos_api.config import Settings
from reclamos_api.main import create_app
from test_search import BILLING, CONTRACT, ENTEL, QUEJA, bodies, query

pytestmark = pytest.mark.integration

CLAIM = {"text": "El usuario reclama un cobro.", "quotes": ["q1"]}
EVIDENCE = {"text": "La empresa presentó su registro.", "quotes": ["q2"]}


def save_card(
    settings: Settings,
    resolution_id: int,
    *,
    prompt_version: str = PROMPT_VERSION,
    body: str | None = None,
    unsupported: list[dict[str, str]] | None = None,
) -> None:
    """A card with a claim and evidence. `unsupported` None leaves it unjudged."""
    query(
        settings,
        """
        INSERT INTO cards (resolution_id, prompt_version, body_sha256, model,
                           fields, readings, rejected, usd,
                           judge_model, unsupported, judged_at)
        VALUES ($1, $2, $3, 'm', $4::jsonb, '{}', '[]', 0, $5, $6::jsonb,
                CASE WHEN $6::jsonb IS NULL THEN NULL ELSE now() END)
        """,
        resolution_id,
        prompt_version,
        body_sha256(body if body is not None else bodies()[resolution_id]),
        json.dumps({"claim": CLAIM, "evidence": EVIDENCE}),
        None if unsupported is None else "judge",
        None if unsupported is None else json.dumps(unsupported),
    )


def find(
    settings: Settings,
    embedder: HashingEmbedder,
    monkeypatch: pytest.MonkeyPatch,
    min_similarity: float,
    **body: Any,
) -> dict[str, Any]:
    monkeypatch.setenv("REFUSAL_MIN_SIMILARITY", str(min_similarity))
    with TestClient(create_app(embedder)) as test_client:
        client: httpx2.Client = test_client
        response = client.post("/cases", json=body)
    assert response.status_code == 200, response.text
    return response.json()


def test_cases_group_by_resolution_and_serve_only_current_judged_fields(
    indexed: Settings, embedder: HashingEmbedder, monkeypatch: pytest.MonkeyPatch
) -> None:
    save_card(indexed, BILLING, unsupported=[{"field": "evidence", "claim": "x"}])
    save_card(indexed, CONTRACT)
    save_card(indexed, QUEJA, body="an older text", unsupported=[])
    save_card(indexed, ENTEL, prompt_version="old", unsupported=[])

    found = find(
        indexed,
        embedder,
        monkeypatch,
        0.0,
        query="cobro de un servicio no reconocido en el recibo",
        limit=10,
    )

    assert found["outcome"] == "found"
    cases = {case["resolution_id"]: case for case in found["cases"]}
    assert len(cases) == len(found["cases"]), "one case per resolution"
    assert set(cases) == {BILLING, CONTRACT, QUEJA, ENTEL}
    assert sum(found["outcomes"].values()) == len(cases)
    assert cases[BILLING]["card"] == {"claim": CLAIM}, "a flagged field is dropped"
    for unserved, why in [
        (CONTRACT, "unjudged"),
        (QUEJA, "written from another body"),
        (ENTEL, "written by another prompt"),
    ]:
        assert cases[unserved]["card"] is None, why
    similarities = [case["similarity"] for case in found["cases"]]
    assert all(0 <= s <= 1 for s in similarities)


def test_no_case_is_shown_below_the_similarity_threshold(
    indexed: Settings, embedder: HashingEmbedder, monkeypatch: pytest.MonkeyPatch
) -> None:
    found = find(indexed, embedder, monkeypatch, 1.01, query="cobro indebido")

    assert found == {
        "query": "cobro indebido",
        "outcome": "no_similar_case",
        "outcomes": {},
        "cases": [],
    }
