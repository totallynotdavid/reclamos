import asyncio
import dataclasses
from collections.abc import Callable
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import anthropic
import httpx2
import pytest
from fakemodel import FakeModel
from reclamos_api.db import database_pool
from reclamos_api.llm.client import (
    MAX_PROMPT_CHARS,
    AnthropicModel,
    NotBilled,
    Prompt,
    UnpricedModel,
    worst_case_usd,
)
from reclamos_api.llm.quotes import is_quoted
from reclamos_api.llm.spend import Budget, Ledger, SpendRefused

HAIKU, SONNET = "claude-haiku-5-5", "claude-sonnet-5-5"
# What FakeModel's default 10 input and 20 output tokens cost, at the published
# prices per million tokens: Haiku 5.5 $0.10 and $0.50, Sonnet 5.5 $2 and $10.
HAIKU_CALL_USD = 0.000011

PROMPT = Prompt(model=HAIKU, system="s", text="t" * 100, schema={}, max_tokens=100)
PASSAGE = "Se declara FUNDADO el recurso de apelación\npresentado por el usuario."


@pytest.mark.parametrize(
    ("quote", "quoted"),
    [
        pytest.param("FUNDADO el recurso de apelación", True, id="exact"),
        pytest.param(
            chr(0x201C) + "se declara fundado el recurso  de APELACION" + chr(0x201D),
            True,
            id="case-accents-spaces-and-quote-marks-differ",
        ),
        pytest.param("INFUNDADO el recurso de apelación", False, id="not-in-text"),
        pytest.param("el usuario.", False, id="too-short"),
    ],
)
def test_a_quote_counts_only_when_it_is_long_and_in_the_text(
    quote: str, quoted: bool
) -> None:
    assert is_quoted(quote, PASSAGE) == quoted


class FailingMessages:
    def __init__(self, error: Exception) -> None:
        self.error = error

    async def create(self, **_: Any) -> Any:
        raise self.error


class FailingClient:
    def __init__(self, error: Exception) -> None:
        self.messages = FailingMessages(error)


REQUEST = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")


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
                    'card', $1, 50, 50
                )
                """,
                HAIKU,
            )
        ledger = Ledger(pool, daily_cap_usd=1.5 * worst)
        budget = Budget(limit_usd=1.0)
        assert await ledger.spent_today() == 0, "yesterday in Lima is not today"

        model = FakeModel(lambda _: "{}")
        metered = await ledger.complete(model, PROMPT, purpose="card", budget=budget)
        assert metered.usd == pytest.approx(HAIKU_CALL_USD)
        assert await ledger.spent_today() == pytest.approx(HAIKU_CALL_USD)

        model.error = NotBilled("529: overloaded")
        with pytest.raises(NotBilled):
            await ledger.complete(model, PROMPT, purpose="card", budget=budget)
        assert await ledger.spent_today() == pytest.approx(HAIKU_CALL_USD)

        model.error = TimeoutError()
        with pytest.raises(TimeoutError):
            await ledger.complete(model, PROMPT, purpose="judge", budget=budget)
        assert await ledger.spent_today() == pytest.approx(HAIKU_CALL_USD + worst)
        assert budget.spent_usd == pytest.approx(HAIKU_CALL_USD + worst)

        model.error = None
        with pytest.raises(SpendRefused, match="daily cap"):
            await ledger.complete(model, PROMPT, purpose="card", budget=budget)
        assert len(model.prompts) == 3, "a refused call never reaches the model"

        rows = (await llm_calls(pool))[1:]
        assert [r["purpose"] for r in rows] == ["card", "card", "judge"]
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
@pytest.mark.parametrize("limit", ["daily cap", "run"])
def test_a_call_in_flight_counts_against_the_daily_cap_and_the_run_budget(
    database_url: str, limit: str
) -> None:
    room = 1.5 * worst_case_usd(PROMPT)

    async def work(pool: Any) -> None:
        ledger = Ledger(pool, daily_cap_usd=room if limit == "daily cap" else 100.0)
        budget = Budget(limit_usd=room if limit == "run" else 100.0)
        slow = FakeModel(lambda _: "{}", gate=asyncio.Event())
        first = asyncio.create_task(
            ledger.complete(slow, PROMPT, purpose="card", budget=budget)
        )
        await slow.started.wait()

        second = FakeModel(lambda _: "{}")
        with pytest.raises(SpendRefused, match=limit):
            await ledger.complete(second, PROMPT, purpose="card", budget=budget)
        assert second.prompts == []

        assert slow.gate is not None
        slow.gate.set()
        await first
        assert await ledger.spent_today() == pytest.approx(HAIKU_CALL_USD)
        assert budget.spent_usd == pytest.approx(HAIKU_CALL_USD)

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
                model, prompt, purpose="card", budget=budget
            )
        assert model.prompts == []
        assert await llm_calls(pool) == []

    run_with_pool(database_url, work)
