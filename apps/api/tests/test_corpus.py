import asyncio
from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Any

import asyncpg
import httpx
import pytest
from fakeembed import HashingEmbedder
from gobpe import FIXTURES, LISTED_IDS, RECORDED_IDS, RecordedSite
from reclamos_api import cli
from reclamos_api.config import Settings
from reclamos_api.corpus import repository
from reclamos_api.corpus.extract import missing_tools
from reclamos_api.corpus.jobs import QUEUE_NAME, Pipeline
from reclamos_api.corpus.site import USER_AGENT, Fetcher, ListedResolution
from reclamos_api.corpus.store import LocalStore
from reclamos_api.db import database_pool
from rqueue import Queue, Worker
from textpdf import text_pdf

pytestmark = pytest.mark.integration

CRAWL = ["corpus", "crawl", "--since", "2020-01-01", "--until", "2026-10-16"]

RESOLUTION_LINES = [
    "TRIBUNAL ADMINISTRATIVO DE SOLUCION DE RECLAMOS DE USUARIOS",
    "RECURSO DE APELACION",
    "EXPEDIENTE N. 0000123-2024/TRASU/ST-RA",
    "RESOLUCION FINAL",
    "RESOLUCION N. 0000456-2024-TRASU/OSIPTEL",
    "Lima, 5 de marzo de 2024",
    "RECLAMANTE :",
    "EMPRESA OPERADORA : ENTEL PERU S.A.",
    "MATERIA RECLAMADA : Solicitud de portabilidad numerica",
    "CODIGO DE RECLAMO : 1-000",
    "RESOLUCION DEL TRIBUNAL : FUNDADO",
]


@pytest.fixture
def environment(
    database_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Settings:
    monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.setenv("PDF_STORE", "local")
    monkeypatch.setenv("PDF_STORE_PATH", str(tmp_path / "pdfs"))
    return Settings.from_environment()


def stat(label: str, count: int) -> str:
    return label.ljust(26) + str(count)


def recorded_fetcher(site: RecordedSite) -> Fetcher:
    return Fetcher.create(min_interval=0, transport=httpx.MockTransport(site))


def crawl_main(argv: list[str], **options: Any) -> None:
    """Run a `reclamos` command with the fake embedder."""
    cli.main(argv, embedder=HashingEmbedder(), **options)


def query(settings: Settings, statement: str, *arguments: Any) -> list[Any]:
    async def run() -> list[Any]:
        connection = await asyncpg.connect(settings.database_url)
        try:
            return await connection.fetch(statement, *arguments)
        finally:
            await connection.close()

    return asyncio.run(run())


def run_jobs(
    settings: Settings, seed: Callable[[Any, LocalStore], Any] | None = None
) -> None:
    """Run the pipeline's jobs over what is already in the database and store.

    No request may leave the process: the fetcher fails any it is asked to make.
    """
    site = RecordedSite()

    async def run() -> None:
        async with database_pool(
            settings.database_url, queue_schema=settings.queue_schema
        ) as pool:
            store = LocalStore(Path(settings.pdf_store_path))
            if seed is not None:
                await seed(pool, store)
            queue = Queue(pool, name=QUEUE_NAME, schema=settings.queue_schema)
            fetcher = recorded_fetcher(site)
            pipeline = Pipeline(pool, queue, store, fetcher, HashingEmbedder())
            pipeline.register()
            await pipeline.enqueue_unfinished()
            await Worker(queue, worker_id="test").drain()
            await fetcher.aclose()

    asyncio.run(run())
    assert site.requests == []


async def seed_downloaded(
    pool: Any, store: LocalStore, resolution_id: int, pdf: bytes
) -> None:
    key = f"resolutions/{resolution_id}.pdf"
    await store.put(key, pdf, content_type="application/pdf")
    async with pool.acquire() as connection:
        listed = ListedResolution(
            id=resolution_id,
            page_url=f"https://www.gob.pe/institucion/osiptel/normas-legales/{resolution_id}-x",
            expediente="Expediente N.° 0000123-2024/TRASU/ST-RA",
            published_on=date(2024, 3, 6),
        )
        await repository.upsert_listed(connection, [listed])
        await repository.record_download(
            connection,
            resolution_id,
            summary="",
            pdf_url=f"https://cdn.www.gob.pe/{resolution_id}.pdf",
            pdf_key=key,
            sha256="0" * 64,
            size=len(pdf),
        )


@pytest.mark.skipif(bool(missing_tools()), reason=f"needs {missing_tools()}")
def test_crawl_ingests_the_recorded_site_and_a_rerun_skips_finished_work(
    environment: Settings,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    first_site = RecordedSite()
    crawl_main(CRAWL, fetcher=recorded_fetcher(first_site))
    first_output = capsys.readouterr().out

    rows = {
        row["id"]: row
        for row in query(environment, "SELECT * FROM resolutions ORDER BY id")
    }
    assert sorted(rows) == sorted(LISTED_IDS)
    for resolution_id in RECORDED_IDS:
        row = rows[resolution_id]
        pdf = (FIXTURES / f"{resolution_id}.pdf").read_bytes()
        assert (
            tmp_path / "pdfs" / f"resolutions/{resolution_id}.pdf"
        ).read_bytes() == pdf
        assert row["pdf_bytes"] == len(pdf)
        assert row["text_method"] == "ocr"
        assert row["failure_reason"] is None
        assert row["parsed_at"] is not None
        assert row["operator"] == "AMERICA MOVIL PERU S.A.C."
        assert row["number"].endswith("-2022-TRASU/OSIPTEL")
    assert rows[6134934]["claim_type"] == "billing"
    assert rows[6134934]["outcome"] == "infundado"
    assert rows[6134935]["claim_type"] == "unrequested_contract"
    assert rows[6134941]["appeal_kind"] == "queja"
    assert rows[6134941]["outcome"] == "improcedente"

    unrecorded = set(LISTED_IDS) - set(RECORDED_IDS)
    for resolution_id in unrecorded:
        row = rows[resolution_id]
        assert (row["failure_stage"], row["failure_reason"]) == (
            "download",
            "page_http_404",
        )
        assert row["downloaded_at"] is None

    printed = first_output.splitlines()
    for line in (
        "resolutions found       8",
        "downloaded              3",
        "parsed                  3",
        "embedded                3",
        "failed                  5",
        stat("  download: page_http_404", 5),
        stat("  billing", 1),
        stat("  unrequested_contract", 1),
        stat("  procedural", 1),
        stat("  infundado", 2),
        stat("  improcedente", 1),
    ):
        assert line in printed, line

    pdf_requests = [r for r in first_site.requests if r.url.host == "cdn.www.gob.pe"]
    assert len(pdf_requests) == 3 + 1  # three PDFs and the CDN's robots.txt
    assert {r.headers["user-agent"] for r in first_site.requests} == {USER_AGENT}
    extracted_at = {
        row["id"]: row["extracted_at"] for row in rows.values() if row["extracted_at"]
    }

    second_site = RecordedSite()
    crawl_main(CRAWL, fetcher=recorded_fetcher(second_site))
    second_output = capsys.readouterr().out

    assert second_output == first_output
    assert [r for r in second_site.requests if r.url.host == "cdn.www.gob.pe"] == []
    fetched_pages = {
        r.url.path.split("/")[-1].split("-")[0] for r in second_site.requests
    }
    assert fetched_pages.isdisjoint(str(i) for i in RECORDED_IDS)
    after = {
        row["id"]: row["extracted_at"]
        for row in query(environment, "SELECT id, extracted_at FROM resolutions")
        if row["extracted_at"]
    }
    assert after == extracted_at


def test_a_wide_window_is_split_until_the_listing_is_complete(
    environment: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    site = RecordedSite()

    def narrow_only(request: httpx.Request) -> httpx.Response:
        params = request.url.params
        if "filter[start_date]" in params:
            start = date.fromisoformat(params["filter[start_date]"])
            end = date.fromisoformat(params["filter[end_date]"])
            if (end - start).days > 31:
                # A truncated page: more entries exist than the page shows.
                reply = site(request)
                return httpx.Response(
                    200, text=reply.text.replace('"8 resultados"', '"250 resultados"')
                )
        if request.url.path.startswith("/institucion/osiptel/normas-legales/"):
            # Only discovery is under test: leave the downloads out.
            site.requests.append(request)
            return httpx.Response(404)
        return site(request)

    fetcher = Fetcher.create(min_interval=0, transport=httpx.MockTransport(narrow_only))
    crawl_main(
        ["corpus", "crawl", "--since", "2022-01-01", "--until", "2022-12-31"],
        fetcher=fetcher,
    )
    capsys.readouterr()

    found = query(environment, "SELECT id FROM resolutions ORDER BY id")
    assert [row["id"] for row in found] == sorted(LISTED_IDS)
    windows = [
        (r.url.params["filter[start_date]"], r.url.params["filter[end_date]"])
        for r in site.requests
        if "filter[start_date]" in r.url.params
    ]
    assert windows[0] == ("2022-01-01", "2022-12-31")
    assert len(windows) > 3
    assert len(set(windows)) == len(windows)


def test_a_day_with_more_entries_than_a_page_is_recorded_as_a_failed_listing(
    environment: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    site = RecordedSite()
    day = ["corpus", "crawl", "--since", "2022-12-31", "--until", "2022-12-31"]

    def always_truncated(request: httpx.Request) -> httpx.Response:
        reply = site(request)
        if "filter[start_date]" in request.url.params:
            return httpx.Response(
                200, text=reply.text.replace('"8 resultados"', '"250 resultados"')
            )
        return reply

    crawl_main(
        day,
        fetcher=Fetcher.create(
            min_interval=0, transport=httpx.MockTransport(always_truncated)
        ),
    )
    output = capsys.readouterr().out

    (failure,) = query(environment, "SELECT * FROM listing_failures")
    assert (failure["start_date"], failure["end_date"]) == (
        date(2022, 12, 31),
        date(2022, 12, 31),
    )
    assert failure["reason"] == "listing_truncated"
    assert failure["detail"] == "250 entries on 2022-12-31, the page shows 8"
    assert stat("  listing_truncated", 1) in output.splitlines()
    assert "listing windows that could not be read" in output
    # The window is final: the job finished instead of failing and retrying.
    assert query(environment, "SELECT 1 FROM resolutions") == []
    assert len([r for r in site.requests if "filter[start_date]" in r.url.params]) == 1

    # A later crawl that lists the day in full clears the failure.
    def listing_only(request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith("/institucion/osiptel/normas-legales/"):
            return httpx.Response(404)
        return site(request)

    crawl_main(
        day,
        fetcher=Fetcher.create(
            min_interval=0, transport=httpx.MockTransport(listing_only)
        ),
    )
    capsys.readouterr()

    assert query(environment, "SELECT 1 FROM listing_failures") == []
    assert len(query(environment, "SELECT 1 FROM resolutions")) == len(LISTED_IDS)


def test_stats_of_an_empty_corpus(
    environment: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    crawl_main(["corpus", "stats"])

    assert capsys.readouterr().out.splitlines() == [
        "resolutions found       0",
        "downloaded              0",
        "extracted               0",
        "parsed                  0",
        "chunked                 0  (0 chunks)",
        "embedded                0",
        "failed                  0",
        "claim types",
        "  none",
        "outcomes",
        "  none",
    ]


@pytest.mark.skipif(
    bool(missing_tools(ocr=False)), reason=f"needs {missing_tools(ocr=False)}"
)
def test_a_text_layer_resolution_is_extracted_and_parsed_without_ocr(
    environment: Settings,
) -> None:
    async def seed(pool: Any, store: LocalStore) -> None:
        await seed_downloaded(pool, store, 101, text_pdf(RESOLUTION_LINES))

    run_jobs(environment, seed)

    (row,) = query(environment, "SELECT * FROM resolutions WHERE id = 101")
    assert row["text_method"] == "text-layer"
    assert row["ocr_pages"] == 0
    assert row["failure_reason"] is None
    assert row["number"] == "0000456-2024-TRASU/OSIPTEL"
    assert row["resolved_on"] == date(2024, 3, 5)
    assert row["operator"] == "ENTEL PERU S.A."
    assert row["claim_type"] == "porting"
    assert row["outcome"] == "fundado"


@pytest.mark.skipif(
    bool(missing_tools(ocr=False)), reason=f"needs {missing_tools(ocr=False)}"
)
def test_a_resolution_that_cannot_be_parsed_keeps_its_text_and_the_reason(
    environment: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    unrelated = ["Este documento no es una resolucion del Tribunal, solo una carta."]

    async def seed(pool: Any, store: LocalStore) -> None:
        await seed_downloaded(pool, store, 102, text_pdf(unrelated))

    run_jobs(environment, seed)
    run_jobs(environment)  # nothing new is queued: the failure is final

    (row,) = query(environment, "SELECT * FROM resolutions WHERE id = 102")
    assert "solo una carta" in row["body"]
    assert row["parsed_at"] is None
    assert (row["failure_stage"], row["failure_reason"]) == ("parse", "no_date")
    assert row["failure_detail"].startswith("missing: date, operator")
    # A damaged header costs the filters, not the search.
    assert row["chunked_at"] is not None
    assert row["embedded_at"] is not None
    assert (
        query(
            environment, "SELECT count(*) AS n FROM chunks WHERE resolution_id = 102"
        )[0]["n"]
        == 1
    )

    crawl_main(["corpus", "stats"])
    assert stat("  parse: no_date", 1) in capsys.readouterr().out.splitlines()


@pytest.mark.skipif(
    bool(missing_tools(ocr=False)), reason=f"needs {missing_tools(ocr=False)}"
)
def test_a_file_that_is_not_a_pdf_fails_extraction_with_a_reason(
    environment: Settings,
) -> None:
    async def seed(pool: Any, store: LocalStore) -> None:
        await seed_downloaded(pool, store, 103, b"<html>cloudflare</html>")

    run_jobs(environment, seed)

    (row,) = query(environment, "SELECT * FROM resolutions WHERE id = 103")
    assert row["extracted_at"] is None
    assert (row["failure_stage"], row["failure_reason"]) == (
        "extract",
        "unreadable_pdf",
    )
