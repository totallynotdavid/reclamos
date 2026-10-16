import asyncio
from collections.abc import Iterator
from datetime import date
from typing import Any, cast

import asyncpg
import httpx
import pytest
from fakeembed import STALE_VECTOR, CountingPool, GatedEmbedder, HashingEmbedder
from fastapi.testclient import TestClient
from gobpe import FIXTURES
from reclamos_api import cli
from reclamos_api.config import Settings
from reclamos_api.corpus import repository
from reclamos_api.corpus.chunk import chunk_text
from reclamos_api.corpus.jobs import QUEUE_NAME, Pipeline
from reclamos_api.corpus.site import Fetcher, ListedResolution
from reclamos_api.corpus.store import LocalStore
from reclamos_api.db import database_pool
from reclamos_api.main import create_app
from rqueue import Queue, Worker

pytestmark = pytest.mark.integration

BILLING, CONTRACT, QUEJA = 6134934, 6134935, 6134941
# A copy of the billing resolution as another operator ruled it in 2024.
ENTEL = 9000001


def bodies() -> dict[int, str]:
    billing = (FIXTURES / f"{BILLING}.txt").read_text()
    return {
        BILLING: billing,
        CONTRACT: (FIXTURES / f"{CONTRACT}.txt").read_text(),
        QUEJA: (FIXTURES / f"{QUEJA}.txt").read_text(),
        ENTEL: billing.replace("AMERICA MOVIL PERU S.A.C.", "ENTEL PERU S.A.")
        .replace("24 de agosto de 2022", "3 de marzo de 2024")
        .replace("INFUNDADO", "FUNDADO"),
    }


async def seed_and_run(
    settings: Settings,
    embedder: HashingEmbedder,
    pdfs: Any,
    *,
    only: list[int] | None = None,
    texts: dict[int, str] | None = None,
    wait_for_retries: bool = False,
) -> None:
    """Store the extracted text of the resolutions, then run chunk and embed.
    Without `wait_for_retries`, a job that failed an attempt stays pending."""
    async with database_pool(
        settings.database_url, queue_schema=settings.queue_schema
    ) as pool:
        async with pool.acquire() as connection:
            for resolution_id, body in (texts or bodies()).items():
                if only is not None and resolution_id not in only:
                    continue
                await repository.upsert_listed(
                    connection,
                    [
                        ListedResolution(
                            id=resolution_id,
                            page_url=f"https://www.gob.pe/institucion/osiptel/normas-legales/{resolution_id}-x",
                            expediente="Expediente N.° 0000123-2022/TRASU/ST-RA",
                            published_on=date(2022, 12, 31),
                        )
                    ],
                )
                await repository.record_download(
                    connection,
                    resolution_id,
                    summary="",
                    pdf_url="https://cdn.www.gob.pe/x.pdf",
                    pdf_key=f"resolutions/{resolution_id}.pdf",
                    sha256="0" * 64,
                    size=1,
                )
                await repository.record_extraction(
                    connection,
                    resolution_id,
                    body=body,
                    method="ocr",
                    pages=body.count("\f") + 1,
                    ocr_pages=body.count("\f") + 1,
                )
        counted = CountingPool(pool)
        embedder.pool = counted
        queue = Queue(pool, name=QUEUE_NAME, schema=settings.queue_schema)
        fetcher = Fetcher.create(
            min_interval=0,
            transport=httpx.MockTransport(lambda request: httpx.Response(500)),
        )
        pipeline = Pipeline(
            cast("asyncpg.Pool", counted), queue, LocalStore(pdfs), fetcher, embedder
        )
        pipeline.register()
        await pipeline.enqueue_unfinished()
        worker = Worker(queue, worker_id="test")
        async with asyncio.timeout(30):
            while True:
                await worker.drain()
                if not wait_for_retries or (await queue.stats()).depth == 0:
                    break
                await asyncio.sleep(0.2)
        await fetcher.aclose()


def query(settings: Settings, statement: str, *arguments: Any) -> list[Any]:
    async def run() -> list[Any]:
        connection = await asyncpg.connect(settings.database_url)
        try:
            return await connection.fetch(statement, *arguments)
        finally:
            await connection.close()

    return asyncio.run(run())


@pytest.fixture
def embedder() -> HashingEmbedder:
    return HashingEmbedder()


@pytest.fixture
def settings(
    database_url: str, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> Settings:
    monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.setenv("PDF_STORE_PATH", str(tmp_path / "pdfs"))
    return Settings.from_environment()


@pytest.fixture
def indexed(settings: Settings, embedder: HashingEmbedder, tmp_path: Any) -> Settings:
    asyncio.run(seed_and_run(settings, embedder, tmp_path / "pdfs"))
    return settings


@pytest.fixture
def client(indexed: Settings, embedder: HashingEmbedder) -> Iterator[TestClient]:
    with TestClient(create_app(embedder)) as test_client:
        yield test_client


def search(client: TestClient, **body: Any) -> list[dict[str, Any]]:
    response = client.post("/search", json=body)
    assert response.status_code == 200, response.text
    return response.json()["results"]


def test_chunk_and_embed_jobs_store_quotable_chunks_for_every_parsed_resolution(
    indexed: Settings,
) -> None:
    rows = query(indexed, "SELECT * FROM resolutions ORDER BY id")
    assert [r["id"] for r in rows] == sorted(bodies())
    for row in rows:
        assert row["parsed_at"] is not None, row["failure_detail"]
        assert row["chunked_at"] is not None
        assert row["embedded_at"] is not None
        chunks = query(
            indexed,
            "SELECT * FROM chunks WHERE resolution_id = $1 ORDER BY ordinal",
            row["id"],
        )
        assert len(chunks) > 3
        assert [c["ordinal"] for c in chunks] == list(range(len(chunks)))
        for chunk in chunks:
            assert row["body"][chunk["char_start"] : chunk["char_end"]] == chunk["text"]
            assert chunk["embedding"] is not None
            assert chunk["page_start"] == 1 + row["body"][: chunk["char_start"]].count(
                "\f"
            )


def test_embedding_holds_no_database_connection_while_the_model_runs(
    indexed: Settings, embedder: HashingEmbedder
) -> None:
    # A job that keeps its connection through a slow stage starves the worker
    # of the connection it needs to renew its leases.
    assert embedder.connections_in_use == [0] * len(bodies())


def test_a_rerun_skips_finished_chunking_and_embedding(
    indexed: Settings, embedder: HashingEmbedder, tmp_path: Any
) -> None:
    embedded = embedder.passages_embedded
    assert embedded == query(indexed, "SELECT count(*) AS n FROM chunks")[0]["n"]
    before = query(
        indexed, "SELECT resolution_id, ordinal, text FROM chunks ORDER BY 1, 2"
    )
    stamps = query(
        indexed, "SELECT id, chunked_at, embedded_at FROM resolutions ORDER BY id"
    )

    asyncio.run(seed_and_run(indexed, embedder, tmp_path / "pdfs", only=[]))

    assert embedder.passages_embedded == embedded
    assert (
        query(indexed, "SELECT resolution_id, ordinal, text FROM chunks ORDER BY 1, 2")
        == before
    )
    assert (
        query(
            indexed, "SELECT id, chunked_at, embedded_at FROM resolutions ORDER BY id"
        )
        == stamps
    )


def test_search_returns_ranked_chunks_with_what_a_citation_needs(
    client: TestClient, indexed: Settings
) -> None:
    results = search(
        client, query="cobro de cargo fijo y venta en cuotas del recibo", k=5
    )

    assert 1 <= len(results) <= 5
    top = results[0]
    assert top["resolution_id"] in (BILLING, ENTEL)
    assert top["number"].endswith("-2022-TRASU/OSIPTEL") or top["number"].endswith(
        "-2024-TRASU/OSIPTEL"
    )
    assert top["claim_type"] == "billing"
    assert top["expediente"].endswith("TRASU/ST-RA")
    assert top["url"].startswith("https://www.gob.pe/")
    assert top["vector_score"] is not None and top["text_score"] is not None
    scores = [r["score"] for r in results]
    assert scores == sorted(scores, reverse=True)

    body = query(
        indexed, "SELECT body FROM resolutions WHERE id = $1", top["resolution_id"]
    )[0]["body"]
    assert body[top["char_start"] : top["char_end"]] == top["text"]
    assert top["page_start"] <= top["page_end"]


def test_the_fused_score_is_the_reciprocal_rank_sum_of_the_two_rankings(
    client: TestClient,
) -> None:
    question = "queja por falta de respuesta al reclamo"

    def ranks(mode: str) -> dict[tuple[int, int], int]:
        results = search(client, query=question, k=50, mode=mode)
        return {(r["resolution_id"], r["ordinal"]): n for n, r in enumerate(results, 1)}

    vector, text = ranks("vector"), ranks("text")
    fused = search(client, query=question, k=50)

    assert fused
    for result in fused:
        key = (result["resolution_id"], result["ordinal"])
        expected = sum(
            1 / (60 + ranking[key]) for ranking in (vector, text) if key in ranking
        )
        assert result["score"] == pytest.approx(expected)
    assert [r["score"] for r in fused] == sorted(
        (r["score"] for r in fused), reverse=True
    )


def test_each_ranker_can_be_asked_alone(client: TestClient) -> None:
    question = "baja del servicio por contrato no solicitado"

    vector = search(client, query=question, mode="vector")
    text = search(client, query=question, mode="text")

    assert vector
    assert all(
        r["text_score"] is None and r["vector_score"] is not None for r in vector
    )
    assert text
    assert all(r["vector_score"] is None and r["text_score"] is not None for r in text)


def test_full_text_search_ignores_accents_and_word_endings(client: TestClient) -> None:
    unaccented = search(
        client, query="analisis del caso facturacion", mode="text", k=50
    )
    accented = search(client, query="análisis del caso facturación", mode="text", k=50)

    assert unaccented
    assert [(r["resolution_id"], r["ordinal"]) for r in unaccented] == [
        (r["resolution_id"], r["ordinal"]) for r in accented
    ]
    # "cobros" stems to the same lexeme as "cobro" in the text.
    assert search(client, query="cobros", mode="text")


@pytest.mark.parametrize(
    ("filters", "expected"),
    [
        ({"claim_type": "unrequested_contract"}, {CONTRACT}),
        ({"claim_type": "procedural"}, {QUEJA}),
        ({"operator": "ENTEL PERU S.A."}, {ENTEL}),
        ({"outcome": "improcedente"}, {QUEJA}),
        ({"outcome": "infundado"}, {BILLING, CONTRACT}),
        ({"outcome": "fundado"}, {ENTEL}),
        ({"date_from": "2024-01-01"}, {ENTEL}),
        ({"date_to": "2022-09-30"}, {BILLING, CONTRACT}),
        ({"date_from": "2022-11-01", "date_to": "2022-11-30"}, {QUEJA}),
        ({"claim_type": "billing", "operator": "AMERICA MOVIL PERU S.A.C."}, {BILLING}),
    ],
)
def test_filters_keep_only_matching_resolutions_in_both_rankers(
    client: TestClient, filters: dict[str, str], expected: set[int]
) -> None:
    for mode in ("vector", "text", "hybrid"):
        results = search(
            client,
            query="el reclamante apeló la resolución de la empresa operadora",
            k=50,
            mode=mode,
            **filters,
        )
        assert {r["resolution_id"] for r in results} == expected, mode


def test_a_resolution_whose_header_cannot_be_read_is_searchable_but_not_filterable(
    settings: Settings, embedder: HashingEmbedder, tmp_path: Any
) -> None:
    unreadable = 9000002
    text = (
        "ilegible\n\nANTECEDENTES\n\n1. El usuario reclamó por la tarjeta zafiro "
        "que nunca recibió.\n\nHA RESUELTO\n\nDeclarar fundado el recurso."
    )
    asyncio.run(
        seed_and_run(settings, embedder, tmp_path / "pdfs", texts={unreadable: text})
    )
    row = query(settings, "SELECT * FROM resolutions WHERE id = $1", unreadable)[0]
    assert row["failure_stage"] == "parse"
    assert row["chunked_at"] is not None and row["embedded_at"] is not None

    with TestClient(create_app(embedder)) as client:
        found = search(client, query="tarjeta zafiro", mode="text")
        filtered = search(client, query="tarjeta zafiro", outcome="fundado")

    assert [(r["resolution_id"], r["date"], r["operator"]) for r in found] == [
        (unreadable, None, None)
    ]
    assert filtered == []


def test_a_filter_nothing_matches_gives_no_results(client: TestClient) -> None:
    assert search(client, query="facturación", operator="VIETTEL PERU S.A.C.") == []


def test_a_query_of_only_stop_words_gives_no_text_results(client: TestClient) -> None:
    assert search(client, query="de la que el", mode="text") == []


def test_k_limits_the_number_of_chunks(client: TestClient) -> None:
    assert len(search(client, query="recurso de apelación", k=3)) == 3


@pytest.mark.parametrize(
    "body",
    [
        {"query": ""},
        {"query": "   "},
        {"query": "a" * 501},
        {"query": "ok", "k": 0},
        {"query": "ok", "k": 51},
        {"query": "ok", "k": "diez"},
        {"query": "ok", "claim_type": "other"},
        {"query": "ok", "outcome": "ganado"},
        {"query": "ok", "mode": "magic"},
        {"query": "ok", "date_from": "ayer"},
        {"query": "ok", "date_from": "2024-02-01", "date_to": "2024-01-01"},
        {"query": "ok", "limit": 3},
        {"k": 3},
    ],
)
def test_invalid_input_is_rejected_before_any_search(
    client: TestClient, body: dict[str, Any]
) -> None:
    assert client.post("/search", json=body).status_code == 422


def test_the_longest_allowed_query_and_k_are_accepted(client: TestClient) -> None:
    longest = ("facturación " * 50)[:500]
    assert len(longest) == 500
    assert len(search(client, query=longest, k=50)) <= 50


def test_reindexing_embeddings_embeds_again_and_keeps_the_chunks(
    indexed: Settings, embedder: HashingEmbedder
) -> None:
    chunks = query(
        indexed, "SELECT resolution_id, ordinal, text FROM chunks ORDER BY 1, 2"
    )
    embedded = embedder.passages_embedded

    cli.main(["corpus", "reindex", "embeddings"], embedder=embedder)

    assert embedder.passages_embedded == 2 * embedded
    assert (
        query(indexed, "SELECT resolution_id, ordinal, text FROM chunks ORDER BY 1, 2")
        == chunks
    )
    assert query(indexed, "SELECT 1 FROM chunks WHERE embedding IS NULL") == []


def test_reindexing_chunks_splits_and_embeds_again(
    indexed: Settings, embedder: HashingEmbedder
) -> None:
    before = query(indexed, "SELECT count(*) AS n FROM chunks")[0]["n"]
    embedded = embedder.passages_embedded

    cli.main(["corpus", "reindex", "chunks"], embedder=embedder)

    assert query(indexed, "SELECT count(*) AS n FROM chunks")[0]["n"] == before
    assert embedder.passages_embedded == 2 * embedded
    assert query(indexed, "SELECT 1 FROM chunks WHERE embedding IS NULL") == []


def test_an_embed_job_stores_nothing_when_its_chunks_were_replaced_under_it(
    settings: Settings, tmp_path: Any
) -> None:
    embedder = GatedEmbedder()

    async def replace_chunks_while_embedding() -> None:
        async def replace() -> None:
            await embedder.started.wait()
            async with (
                database_pool(
                    settings.database_url, queue_schema=settings.queue_schema
                ) as pool,
                pool.acquire() as connection,
            ):
                await repository.reset_chunks(connection)
                await repository.record_chunks(
                    connection, QUEJA, chunk_text(bodies()[QUEJA])
                )
            embedder.release.set()

        await asyncio.gather(
            seed_and_run(
                settings,
                embedder,
                tmp_path / "pdfs",
                only=[QUEJA],
                wait_for_retries=True,
            ),
            replace(),
        )

    asyncio.run(replace_chunks_while_embedding())

    # The attempt that embedded the old chunks stored nothing, and the retry that
    # rqueue ran embedded the new ones.
    assert query(settings, "SELECT 1 FROM chunks") != []
    assert query(settings, "SELECT 1 FROM chunks WHERE embedding IS NULL") == []
    row = query(settings, "SELECT * FROM resolutions")[0]
    assert row["embedded_at"] is not None
    assert row["parsed_at"] is not None
    assert row["failure_stage"] is None
    assert (
        query(
            settings,
            "SELECT 1 FROM chunks WHERE embedding <=> $1::vector < 0.01",
            repository.vector_literal(STALE_VECTOR),
        )
        == []
    )


class OtherModelEmbedder(HashingEmbedder):
    model = "other-model"


def clear_vectors(settings: Settings) -> None:
    async def clear() -> None:
        connection = await asyncpg.connect(settings.database_url)
        try:
            await repository.reset_embeddings(connection, HashingEmbedder.model)
        finally:
            await connection.close()

    asyncio.run(clear())


def test_an_embed_job_refused_for_its_model_is_embedded_by_a_process_with_the_index_model(
    indexed: Settings, embedder: HashingEmbedder, tmp_path: Any
) -> None:
    clear_vectors(indexed)

    # A worker still on another model claims every embed job first.
    asyncio.run(seed_and_run(indexed, OtherModelEmbedder(), tmp_path / "pdfs", only=[]))

    assert query(indexed, "SELECT 1 FROM chunks WHERE embedding IS NOT NULL") == []
    assert {
        (r["embedded_at"], r["failure_stage"])
        for r in query(indexed, "SELECT embedded_at, failure_stage FROM resolutions")
    } == {(None, None)}

    asyncio.run(
        seed_and_run(
            indexed, embedder, tmp_path / "pdfs", only=[], wait_for_retries=True
        )
    )

    assert query(indexed, "SELECT 1 FROM chunks WHERE embedding IS NULL") == []
    assert [
        (r["embedded_at"] is not None, r["parsed_at"] is not None, r["failure_stage"])
        for r in query(
            indexed, "SELECT embedded_at, parsed_at, failure_stage FROM resolutions"
        )
    ] == [(True, True, None)] * len(bodies())


def test_a_job_refused_for_its_model_on_every_attempt_records_the_failure(
    indexed: Settings, tmp_path: Any
) -> None:
    clear_vectors(indexed)

    asyncio.run(
        seed_and_run(
            indexed,
            OtherModelEmbedder(),
            tmp_path / "pdfs",
            only=[],
            wait_for_retries=True,
        )
    )

    assert query(indexed, "SELECT 1 FROM chunks WHERE embedding IS NOT NULL") == []
    assert {
        (r["failure_stage"], r["failure_reason"])
        for r in query(indexed, "SELECT failure_stage, failure_reason FROM resolutions")
    } == {("embed", "other_model")}


def test_search_refuses_a_query_embedded_by_another_model_than_the_index(
    indexed: Settings,
) -> None:
    with TestClient(create_app(OtherModelEmbedder())) as other:
        for mode in ("hybrid", "vector"):
            response = other.post(
                "/search", json={"query": "cobro del recibo", "mode": mode}
            )
            assert response.status_code == 503
            assert "hashing" in response.json()["detail"]
            assert "other-model" in response.json()["detail"]
        # Words do not depend on the model.
        assert search(other, query="cobro del recibo", mode="text") != []
