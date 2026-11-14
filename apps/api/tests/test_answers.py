import asyncio
import dataclasses
import itertools
import json
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import anthropic
import httpx2
import pytest
from fakeembed import HashingEmbedder
from fakemodel import FakeModel, answer_reply, quoting_first_passage
from fastapi.testclient import TestClient
from reclamos_api import cli
from reclamos_api.answers.answer import (
    Answer,
    AnswerSettings,
    Citation,
    answer_question,
    check_citations,
)
from reclamos_api.answers.judge import cited_passages
from reclamos_api.answers.llm import (
    MAX_PROMPT_CHARS,
    AnthropicModel,
    NotBilled,
    Prompt,
    UnpricedModel,
    worst_case_usd,
)
from reclamos_api.answers.spend import Budget, Ledger, SpendRefused
from reclamos_api.config import Settings
from reclamos_api.db import database_pool
from reclamos_api.main import create_app
from reclamos_api.retrieval.search import Filters, Hit
from test_search import BILLING, CONTRACT, query, seed_and_run

HAIKU, SONNET = "claude-haiku-5-5", "claude-sonnet-5-5"
# What FakeModel's default 10 input and 20 output tokens cost, at the published
# prices per million tokens: Haiku 5.5 $0.10 and $0.50, Sonnet 5.5 $2 and $10.
HAIKU_CALL_USD = 0.000011
SONNET_CALL_USD = 0.00022

PROMPT = Prompt(model=HAIKU, system="s", text="t" * 100, schema={}, max_tokens=100)
INVENTED = "el tribunal ordenó devolver diez mil soles"


def hit(resolution_id: int, ordinal: int, text: str) -> Hit:
    return Hit(resolution_id, ordinal, score=1.0, fields={"text": text})


PASSAGES = [
    hit(1, 0, "La empresa operadora acreditó la contratación del servicio."),
    hit(2, 3, "Se declara FUNDADO el recurso de apelación\npresentado por el usuario."),
]


@pytest.mark.parametrize(
    ("citation", "kept"),
    [
        pytest.param(
            {"passage": 1, "quote": "acreditó la contratación del servicio"},
            True,
            id="exact",
        ),
        pytest.param(
            {
                "passage": 2,
                "quote": chr(0x201C)
                + "se declara fundado el recurso  de APELACION"
                + chr(0x201D),
            },
            True,
            id="case-accents-spaces-and-quote-marks-differ",
        ),
        pytest.param(
            {"passage": 1, "quote": "Se declara FUNDADO el recurso"},
            False,
            id="quote-is-in-another-passage",
        ),
        pytest.param(
            {"passage": 3, "quote": "Se declara FUNDADO el recurso"},
            False,
            id="no-such-passage",
        ),
        pytest.param(
            {"passage": 0, "quote": "acreditó la contratación"},
            False,
            id="passage-zero",
        ),
        pytest.param(
            {"passage": "1", "quote": "acreditó la contratación"},
            False,
            id="passage-not-a-number",
        ),
        pytest.param(
            {"passage": 1, "quote": "el servicio."}, False, id="quote-too-short"
        ),
        pytest.param({"passage": 1}, False, id="no-quote"),
        pytest.param(
            "acreditó la contratación del servicio", False, id="not-an-object"
        ),
    ],
)
def test_a_citation_is_kept_only_when_its_quote_is_in_the_passage_it_names(
    citation: Any, kept: bool
) -> None:
    accepted, rejected = check_citations([citation], PASSAGES)

    assert bool(accepted) == kept
    assert len(rejected) == (0 if kept else 1)
    if kept:
        number: int = citation["passage"]
        assert accepted[0].resolution_id == PASSAGES[number - 1].resolution_id
        assert accepted[0].ordinal == PASSAGES[number - 1].ordinal


class FailingMessages:
    def __init__(self, error: Exception) -> None:
        self.error = error

    async def create(self, **_: Any) -> Any:
        raise self.error


class FailingClient:
    def __init__(self, error: Exception) -> None:
        self.messages = FailingMessages(error)


REQUEST = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")


def test_the_judge_reads_every_passage_the_answer_marks_or_cites() -> None:
    answer = Answer(
        "¿Procede el cobro?",
        "answered",
        text="Se acreditó la contratación [1]. El recurso fue fundado [2]. Ver [7].",
        citations=[Citation(1, 1, 0, "acreditó la contratación del servicio")],
        passages=PASSAGES,
    )
    assert cited_passages(answer) == {1, 2}, (
        "passage 2 is marked but its quote failed; 7 does not exist"
    )


class RecordingMessages:
    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []

    async def create(self, **request: Any) -> Any:
        self.requests.append(request)
        return SimpleNamespace(
            content=[
                SimpleNamespace(type="thinking", thinking="..."),
                SimpleNamespace(type="text", text='{"supported": false}'),
            ],
            usage=SimpleNamespace(input_tokens=12, output_tokens=7),
            stop_reason="end_turn",
        )


class RecordingClient:
    def __init__(self) -> None:
        self.messages = RecordingMessages()


@pytest.mark.parametrize(
    ("model", "thinking"),
    [
        pytest.param(HAIKU, {"type": "disabled"}, id="haiku"),
        pytest.param(SONNET, {"type": "between_tools"}, id="sonnet"),
    ],
)
def test_every_request_turns_thinking_off_and_the_reply_is_its_text(
    model: str, thinking: dict[str, str]
) -> None:
    """Thinking tokens count against `max_tokens`. Left on, they spent the whole
    cap of a real answer before it wrote any text."""
    client = RecordingClient()
    prompt = dataclasses.replace(PROMPT, model=model)

    completion = asyncio.run(AnthropicModel(client=client).complete(prompt))

    assert client.messages.requests[0]["thinking"] == thinking
    assert client.messages.requests[0]["max_tokens"] == prompt.max_tokens
    assert completion.text == '{"supported": false}'
    assert (completion.input_tokens, completion.output_tokens) == (12, 7)


def test_only_an_error_status_from_the_api_counts_as_not_billed() -> None:
    """A status error is a refused request. A timeout may come after the model
    ran, so it must reach the ledger as it is and keep the reserved cost."""
    overloaded = anthropic.APIStatusError(
        "overloaded", response=httpx2.Response(529, request=REQUEST), body=None
    )
    with pytest.raises(NotBilled):
        asyncio.run(AnthropicModel(client=FailingClient(overloaded)).complete(PROMPT))
    with pytest.raises(anthropic.APITimeoutError):
        asyncio.run(
            AnthropicModel(
                client=FailingClient(anthropic.APITimeoutError(REQUEST))
            ).complete(PROMPT)
        )


def run_with_pool(url: str, work: Callable[[Any], Any]) -> Any:
    async def run() -> Any:
        async with database_pool(url, queue_schema="task_queue") as pool:
            return await work(pool)

    return asyncio.run(run())


async def llm_calls(pool: Any) -> list[Any]:
    async with pool.acquire() as connection:
        return await connection.fetch("SELECT * FROM llm_calls ORDER BY id")


@pytest.mark.integration
def test_the_daily_cap_counts_billed_failed_and_unbilled_calls_of_the_lima_day(
    database_url: str,
) -> None:
    worst = worst_case_usd(PROMPT)

    async def work(pool: Any) -> None:
        async with pool.acquire() as connection:
            await connection.execute(
                """
                INSERT INTO llm_calls (started_at, purpose, model, reserved_usd, usd)
                VALUES (
                    date_trunc('day', now() AT TIME ZONE 'America/Lima')
                        AT TIME ZONE 'America/Lima' - interval '1 second',
                    'answer', $1, 50, 50
                )
                """,
                HAIKU,
            )
        ledger = Ledger(pool, daily_cap_usd=1.5 * worst)
        budget = Budget(limit_usd=1.0)
        assert await ledger.spent_today() == 0, "yesterday in Lima is not today"

        model = FakeModel(lambda _: "{}")
        metered = await ledger.complete(model, PROMPT, purpose="answer", budget=budget)
        assert metered.usd == pytest.approx(HAIKU_CALL_USD)
        assert await ledger.spent_today() == pytest.approx(HAIKU_CALL_USD)

        model.error = NotBilled("529: overloaded")
        with pytest.raises(NotBilled):
            await ledger.complete(model, PROMPT, purpose="answer", budget=budget)
        assert await ledger.spent_today() == pytest.approx(HAIKU_CALL_USD)

        model.error = TimeoutError()
        with pytest.raises(TimeoutError):
            await ledger.complete(model, PROMPT, purpose="judge", budget=budget)
        assert await ledger.spent_today() == pytest.approx(HAIKU_CALL_USD + worst)
        assert budget.spent_usd == pytest.approx(HAIKU_CALL_USD + worst)

        model.error = None
        with pytest.raises(SpendRefused, match="daily cap"):
            await ledger.complete(model, PROMPT, purpose="answer", budget=budget)
        assert len(model.prompts) == 3, "a refused call never reaches the model"

        rows = (await llm_calls(pool))[1:]
        assert [r["purpose"] for r in rows] == ["answer", "answer", "judge"]
        assert all(r["finished_at"] is not None for r in rows)
        assert [r["input_tokens"] for r in rows] == [10, None, None]
        assert [float(r["usd"]) for r in rows] == pytest.approx(
            [HAIKU_CALL_USD, 0.0, worst]
        )
        assert [float(r["reserved_usd"]) for r in rows] == pytest.approx([worst] * 3)
        assert rows[0]["usd"] == Decimal(repr(HAIKU_CALL_USD)), (
            "an amount is stored as the float's shortest decimal"
        )
        assert rows[0]["reserved_usd"] == Decimal(repr(worst))
        assert rows[0]["error"] is None
        assert "overloaded" in rows[1]["error"]

    run_with_pool(database_url, work)


@pytest.mark.integration
def test_a_call_in_flight_counts_against_the_daily_cap(database_url: str) -> None:
    async def work(pool: Any) -> None:
        ledger = Ledger(pool, daily_cap_usd=1.5 * worst_case_usd(PROMPT))
        slow = FakeModel(lambda _: "{}", gate=asyncio.Event())
        first = asyncio.create_task(ledger.complete(slow, PROMPT, purpose="answer"))
        await slow.started.wait()

        second = FakeModel(lambda _: "{}")
        with pytest.raises(SpendRefused):
            await ledger.complete(second, PROMPT, purpose="answer")
        assert second.prompts == []

        assert slow.gate is not None
        slow.gate.set()
        await first
        assert await ledger.spent_today() == pytest.approx(HAIKU_CALL_USD)

    run_with_pool(database_url, work)


@pytest.mark.integration
@pytest.mark.parametrize(
    ("prompt", "budget", "error"),
    [
        pytest.param(
            Prompt(HAIKU, "s", "t" * MAX_PROMPT_CHARS, {}, 100),
            None,
            SpendRefused,
            id="prompt-too-long",
        ),
        pytest.param(
            PROMPT, Budget(limit_usd=0.00001), SpendRefused, id="over-the-run-budget"
        ),
        pytest.param(
            Prompt("claude-unpriced", "s", "t", {}, 100),
            None,
            UnpricedModel,
            id="no-known-price",
        ),
    ],
)
def test_a_call_that_could_pass_a_limit_is_never_made_or_logged(
    database_url: str, prompt: Prompt, budget: Budget | None, error: type[Exception]
) -> None:
    async def work(pool: Any) -> None:
        model = FakeModel(lambda _: "{}")
        with pytest.raises(error):
            await Ledger(pool, daily_cap_usd=100.0).complete(
                model, prompt, purpose="answer", budget=budget
            )
        assert model.prompts == []
        assert await llm_calls(pool) == []

    run_with_pool(database_url, work)


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
    asyncio.run(seed_and_run(settings, embedder, tmp_path / "pdfs"))
    return settings


QUESTION = "¿Me pueden cobrar el cargo fijo y la venta en cuotas del equipo?"


@pytest.mark.integration
def test_an_answer_keeps_only_verified_citations_and_otherwise_refuses(
    indexed: Settings, embedder: HashingEmbedder
) -> None:
    always = AnswerSettings(model=HAIKU, min_similarity=0.0)
    cases: list[tuple[str, FakeModel, AnswerSettings, Filters, str | None]] = [
        ("answered", FakeModel(quoting_first_passage), always, Filters(), None),
        (
            "reply cut at max_tokens",
            FakeModel(quoting_first_passage, stop_reason="max_tokens"),
            always,
            Filters(),
            "unverified",
        ),
        (
            "reply not JSON",
            FakeModel(lambda _: "Sí, puede."),
            always,
            Filters(),
            "unverified",
        ),
        (
            "every quote invented",
            FakeModel(lambda _: answer_reply([{"passage": 1, "quote": INVENTED}])),
            always,
            Filters(),
            "unverified",
        ),
        (
            "model says the passages do not answer",
            FakeModel(lambda _: answer_reply([], supported=False)),
            always,
            Filters(),
            "model_declined",
        ),
        (
            "best passage too far from the question",
            FakeModel(quoting_first_passage),
            AnswerSettings(model=HAIKU, min_similarity=1.01),
            Filters(),
            "low_similarity",
        ),
        (
            "no passage matches the filters",
            FakeModel(quoting_first_passage),
            always,
            Filters(operator="VIETTEL PERU S.A.C."),
            "no_passages",
        ),
    ]

    async def work(pool: Any) -> list[Answer]:
        ledger = Ledger(pool, daily_cap_usd=1.0)
        return [
            await answer_question(
                pool,
                embedder,
                ledger,
                model,
                answer_settings,
                QUESTION,
                filters=filters,
            )
            for _, model, answer_settings, filters, _ in cases
        ]

    results = run_with_pool(indexed.database_url, work)

    for (name, model, _, _, reason), result in zip(cases, results, strict=True):
        assert result.reason == reason, name
        assert result.outcome == ("answered" if reason is None else "refused"), name
        calls = 0 if reason in ("low_similarity", "no_passages") else 1
        assert len(model.prompts) == calls, name
        assert result.usd == pytest.approx(calls * HAIKU_CALL_USD), name
        if reason is not None:
            assert result.text == "" and result.citations == [], name
    answered = results[0]
    assert answered.text
    assert [c.resolution_id for c in answered.citations] == [
        answered.passages[0].resolution_id
    ]
    assert [r["passage"] for r in answered.rejected] == [1, 99]
    assert answered.rejected[0]["quote"] == INVENTED
    assert len(query(indexed, "SELECT id FROM llm_calls")) == 5


@pytest.mark.integration
def test_the_answer_route_answers_with_sources_and_reports_why_it_cannot(
    indexed: Settings, embedder: HashingEmbedder, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("REFUSAL_MIN_SIMILARITY", "0")
    model = FakeModel(quoting_first_passage)
    with TestClient(create_app(embedder, model)) as client:
        response = client.post("/answer", json={"question": QUESTION})
        assert client.post("/answer", json={"question": " "}).status_code == 422
        assert (
            client.post("/answer", json={"question": QUESTION, "k": 50}).status_code
            == 422
        )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["outcome"] == "answered"
    assert body["reason"] is None
    [citation] = body["citations"]
    assert citation["url"].startswith("https://www.gob.pe/institucion/osiptel/")
    assert citation["expediente"]
    assert citation["page_start"] <= citation["page_end"]
    assert len(model.prompts) == 1

    with TestClient(create_app(embedder)) as client:
        unconfigured = client.post("/answer", json={"question": QUESTION})
    assert unconfigured.status_code == 503

    monkeypatch.setenv("DAILY_USD_CAP", "0")
    capped_model = FakeModel(quoting_first_passage)
    with TestClient(create_app(embedder, capped_model)) as client:
        capped = client.post("/answer", json={"question": QUESTION})
    assert capped.status_code == 429
    assert "US$" not in capped.text, "a visitor is not shown what was spent"
    assert capped_model.prompts == []


def write_questions(path: Path) -> Path:
    lines: list[dict[str, Any]] = [
        {
            "id": "a1",
            "query": QUESTION,
            "split": "tune",
            "answerable": True,
            "relevant": [BILLING],
            "evidence": [{"resolution_id": BILLING, "quote": "cargo fijo"}],
        },
        {
            "id": "a2",
            "query": "Renovaron mi contrato sin que yo lo pidiera",
            "split": "tune",
            "answerable": True,
            "relevant": [CONTRACT],
            "evidence": [{"resolution_id": CONTRACT, "quote": "Renovación"}],
        },
        {
            "id": "u1",
            "query": "¿Cuánto cuesta un plan de internet satelital?",
            "split": "held_out",
            "answerable": False,
            "relevant": [],
            "evidence": [],
            "note": "No resolution prices plans.",
        },
    ]
    path.write_text("".join(json.dumps(line) + "\n" for line in lines))
    return path


def answers_eval(
    tmp_path: Path, embedder: HashingEmbedder, model: FakeModel, *flags: str
) -> dict[str, Any]:
    output = tmp_path / "answers.json"
    cli.main(
        [
            "answers",
            "eval",
            "--questions",
            str(write_questions(tmp_path / "questions.jsonl")),
            "--output",
            str(output),
            "--min-similarity",
            "0",
            *flags,
        ],
        embedder=embedder,
        model=model,
    )
    return json.loads(output.read_text())


@pytest.mark.integration
def test_the_answers_eval_judges_each_answer_and_writes_the_report(
    indexed: Settings,
    embedder: HashingEmbedder,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    model = FakeModel(quoting_first_passage)
    report = answers_eval(tmp_path, embedder, model, "--budget-usd", "1")

    assert "results written to" in capsys.readouterr().out
    assert report["stopped"] is None
    assert report["answer_model"] == HAIKU
    assert report["judge_model"] == SONNET
    assert [p.model for p in model.prompts] == [HAIKU, SONNET] * 3
    for asked, judged in itertools.batched(model.prompts, 2, strict=True):
        label = asked.text.split("\n")[2]
        assert label.startswith("[1] Resolución ") and "resultado" in label
        assert label in judged.text.split("\n"), (
            "the judge reads the cited passage with the label the answer model saw"
        )
    summary = report["summary"]
    assert summary["run"] == summary["questions"] == 3
    assert summary["answerable"] == 2 and summary["unanswerable"] == 1
    assert summary["faithful"] == 1.0 and summary["judged"] == 3
    assert summary["refused_unanswerable"] == 0.0
    assert summary["rejected_citations"] == 6
    assert summary["usd"] == pytest.approx(3 * (HAIKU_CALL_USD + SONNET_CALL_USD))
    assert sum(q["usd"] for q in report["questions"]) == pytest.approx(summary["usd"])
    assert [q["id"] for q in report["questions"]] == ["a1", "a2", "u1"]
    assert all(q["faithful"] is True for q in report["questions"])
    sweep = {row["threshold"]: row for row in summary["sweep"]}
    assert sweep[0.7]["answerable_kept"] >= sweep[0.9]["answerable_kept"]


@pytest.mark.integration
def test_the_answers_eval_stops_before_it_would_pass_its_budget(
    indexed: Settings, embedder: HashingEmbedder, tmp_path: Path
) -> None:
    # Each answer is billed US$0.00095; every reservation is at least US$0.00045,
    # what 900 output tokens cost. So one or two of the three fit in US$0.002.
    model = FakeModel(quoting_first_passage, input_tokens=5000, output_tokens=900)
    report = answers_eval(
        tmp_path, embedder, model, "--budget-usd", "0.002", "--no-judge"
    )

    run = report["summary"]["run"]
    assert 1 <= run < 3
    assert report["stopped"].startswith(f"stopped before {['a1', 'a2', 'u1'][run]}")
    assert report["judge_model"] is None
    assert len(model.prompts) == run
    assert report["summary"]["usd"] == pytest.approx(run * 0.00095)
    assert report["summary"]["usd"] <= 0.002
    assert len(query(indexed, "SELECT id FROM llm_calls")) == run
