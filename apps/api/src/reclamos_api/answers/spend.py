"""The cost log, and the limits checked against it before every call.

Every call to a language model goes through `Ledger.complete`. It writes a row
to `llm_calls` holding the most the call can cost before the call is made, so
calls in flight count against the daily cap, and replaces it with the billed
cost when the call returns.
"""

import asyncio
from dataclasses import dataclass
from typing import Any, Literal

from reclamos_api.answers.llm import (
    MAX_PROMPT_CHARS,
    Completion,
    LanguageModel,
    NotBilled,
    Prompt,
    price_of,
    worst_case_usd,
)

Purpose = Literal["answer", "judge"]

# Any constant works; it only has to be the same in every process that spends.
_SPEND_LOCK = 7_351_402

# The day the cap counts is the calendar day in Lima.
_TODAY_SPENT = """
SELECT coalesce(sum(coalesce(usd, reserved_usd)), 0)::float8
FROM llm_calls
WHERE started_at >= date_trunc('day', now() AT TIME ZONE 'America/Lima')
                    AT TIME ZONE 'America/Lima'
"""


class SpendRefused(Exception):
    """The call was not made, because it could pass a limit."""


@dataclass(slots=True)
class Budget:
    """A limit on one run, such as an evaluation, on top of the daily cap."""

    limit_usd: float
    spent_usd: float = 0.0


@dataclass(frozen=True, slots=True)
class Metered:
    completion: Completion
    usd: float


class Ledger:
    def __init__(self, pool: Any, *, daily_cap_usd: float) -> None:
        self._pool = pool
        self.daily_cap_usd = daily_cap_usd

    async def spent_today(self) -> float:
        async with self._pool.acquire() as connection:
            return await connection.fetchval(_TODAY_SPENT)

    async def complete(
        self,
        model: LanguageModel,
        prompt: Prompt,
        *,
        purpose: Purpose,
        budget: Budget | None = None,
    ) -> Metered:
        if len(prompt.system) + len(prompt.text) > MAX_PROMPT_CHARS:
            raise SpendRefused(f"the prompt is over {MAX_PROMPT_CHARS} characters")
        reserved = worst_case_usd(prompt)
        if budget is not None and budget.spent_usd + reserved > budget.limit_usd:
            raise SpendRefused(
                f"the run has spent US${budget.spent_usd:.4f} of US${budget.limit_usd:.2f}"
                f" and this call can cost US${reserved:.4f}"
            )
        call_id = await self._reserve(prompt.model, purpose, reserved)
        try:
            completion = await model.complete(prompt)
        except BaseException as error:
            billed = 0.0 if isinstance(error, NotBilled) else reserved
            await asyncio.shield(self._fail(call_id, billed, repr(error)))
            if budget is not None:
                budget.spent_usd += billed
            raise
        usd = price_of(prompt.model).usd(
            completion.input_tokens, completion.output_tokens
        )
        await asyncio.shield(self._settle(call_id, completion, usd))
        if budget is not None:
            budget.spent_usd += usd
        return Metered(completion, usd)

    async def _reserve(self, model: str, purpose: Purpose, reserved: float) -> int:
        async with self._pool.acquire() as connection, connection.transaction():
            # Serializes the check and the insert across processes, so two calls
            # cannot both fit under the cap that only one of them fits under.
            await connection.execute("SELECT pg_advisory_xact_lock($1)", _SPEND_LOCK)
            spent = await connection.fetchval(_TODAY_SPENT)
            if spent + reserved > self.daily_cap_usd:
                raise SpendRefused(
                    f"today's spend is US${spent:.4f} of the US${self.daily_cap_usd:.2f}"
                    f" daily cap and this call can cost US${reserved:.4f}"
                )
            return await connection.fetchval(
                """
                INSERT INTO llm_calls (purpose, model, reserved_usd)
                VALUES ($1, $2, $3) RETURNING id
                """,
                purpose,
                model,
                reserved,
            )

    async def _settle(self, call_id: int, completion: Completion, usd: float) -> None:
        async with self._pool.acquire() as connection:
            await connection.execute(
                """
                UPDATE llm_calls
                SET finished_at = now(), input_tokens = $2, output_tokens = $3,
                    usd = $4
                WHERE id = $1
                """,
                call_id,
                completion.input_tokens,
                completion.output_tokens,
                usd,
            )

    async def _fail(self, call_id: int, usd: float, error: str) -> None:
        async with self._pool.acquire() as connection:
            await connection.execute(
                "UPDATE llm_calls SET finished_at = now(), usd = $2, error = $3"
                " WHERE id = $1",
                call_id,
                usd,
                error[:500],
            )
