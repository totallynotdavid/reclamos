"""The `reclamos cards` commands. Each one runs offline: cards are written and
judged ahead of time, so no visitor request calls a model."""

import asyncio
import json
import re
from collections import Counter
from collections.abc import Awaitable, Callable, Sequence
from datetime import date
from typing import Any

from reclamos_api.cards import repository
from reclamos_api.cards.card import FIELDS, PROMPT_VERSION, Unreadable, write_card
from reclamos_api.cards.judge import judge_card
from reclamos_api.config import Settings
from reclamos_api.db import database_pool
from reclamos_api.llm.client import LanguageModel
from reclamos_api.llm.quotes import normalize
from reclamos_api.llm.spend import Budget, Ledger, SpendRefused

DEFAULT_CONCURRENCY = 4


async def build(
    settings: Settings,
    model: LanguageModel,
    *,
    budget_usd: float,
    concurrency: int = DEFAULT_CONCURRENCY,
    limit: int | None = None,
) -> str:
    """Write a card for every resolution without a current one. Stops before
    the run would pass `budget_usd` or the daily cap."""
    budget = Budget(budget_usd)
    failed: list[str] = []
    written = 0
    async with database_pool(
        settings.database_url, queue_schema=settings.queue_schema
    ) as pool:
        ledger = Ledger(pool, daily_cap_usd=settings.daily_usd_cap)
        async with pool.acquire() as connection:
            sources = (await repository.stale_sources(connection))[:limit]

        async def one(source: repository.Source) -> None:
            nonlocal written
            try:
                card, usd = await write_card(
                    ledger, model, settings.card_model, source.body, budget=budget
                )
            except Unreadable as error:
                failed.append(f"{source.resolution_id}: {error}")
                return
            async with pool.acquire() as connection:
                await repository.save_card(
                    connection, source, card, model=settings.card_model, usd=usd
                )
            written += 1

        stopped = await _run_all(sources, one, concurrency)
    lines = [f"cards written: {written} of {len(sources)} without a current card"]
    lines += [f"unreadable {line}" for line in failed]
    if stopped:
        lines.append(f"stopped: {stopped}")
    lines.append(f"spent US${budget.spent_usd:.4f} of US${budget_usd:.2f}")
    return "\n".join(lines)


async def judge(
    settings: Settings,
    model: LanguageModel,
    *,
    budget_usd: float,
    concurrency: int = DEFAULT_CONCURRENCY,
) -> str:
    """Judge every card that has no verdict."""
    budget = Budget(budget_usd)
    unread: list[int] = []
    judged = 0
    async with database_pool(
        settings.database_url, queue_schema=settings.queue_schema
    ) as pool:
        ledger = Ledger(pool, daily_cap_usd=settings.daily_usd_cap)
        async with pool.acquire() as connection:
            cards = await repository.unjudged(connection)

        async def one(card: tuple[int, Any]) -> None:
            nonlocal judged
            resolution_id, fields = card
            verdict = await judge_card(
                ledger, model, settings.judge_model, fields, budget=budget
            )
            if verdict.unsupported is None:
                unread.append(resolution_id)
                return
            async with pool.acquire() as connection:
                await repository.save_verdict(
                    connection,
                    resolution_id,
                    verdict.unsupported,
                    judge_model=settings.judge_model,
                )
            judged += 1

        stopped = await _run_all(cards, one, concurrency)
    lines = [f"cards judged: {judged} of {len(cards)} without a verdict"]
    if unread:
        lines.append(f"verdict unreadable: {', '.join(map(str, unread))}")
    if stopped:
        lines.append(f"stopped: {stopped}")
    lines.append(f"spent US${budget.spent_usd:.4f} of US${budget_usd:.2f}")
    return "\n".join(lines)


async def _run_all[T](
    items: Sequence[T], work: Callable[[T], Awaitable[None]], concurrency: int
) -> str | None:
    """Run `work` on each item, `concurrency` at a time. The first refused call
    stops new work; calls in flight finish. Returns the refusal."""
    pending = iter(items)
    stopped: str | None = None

    async def worker() -> None:
        nonlocal stopped
        for item in pending:
            if stopped is not None:
                return
            try:
                await work(item)
            except SpendRefused as refused:
                stopped = stopped or str(refused)
                return

    await asyncio.gather(*(worker() for _ in range(concurrency)))
    return stopped


async def report(settings: Settings) -> str:
    """What the cards hold, what the checks dropped, and where the model's
    reading and parse disagree."""
    async with (
        database_pool(
            settings.database_url, queue_schema=settings.queue_schema
        ) as pool,
        pool.acquire() as connection,
    ):
        rows = await repository.report_rows(connection)
    return format_report(rows)


def format_report(rows: Sequence[Any]) -> str:
    fields: Counter[str] = Counter()
    dropped_quotes = dropped_fields = dropped_readings = 0
    disagree: list[str] = []
    filled: list[str] = []
    judged = faithful = 0
    unsupported: Counter[str] = Counter()
    for row in rows:
        card_fields = json.loads(row["fields"])
        fields.update(card_fields.keys())
        for entry in json.loads(row["rejected"]):
            if "reading" in entry:
                dropped_readings += 1
            elif "quote" in entry:
                dropped_quotes += 1
            else:
                dropped_fields += 1
        readings = json.loads(row["readings"])
        for name, parsed in (
            ("outcome", row["outcome"]),
            ("operator", row["operator"]),
            ("resolved_on", row["resolved_on"]),
        ):
            if name not in readings:
                continue
            read = readings[name]["value"]
            if parsed is None:
                filled.append(f"{row['resolution_id']} {name}: {read}")
            elif not _same(name, parsed, read):
                disagree.append(
                    f"{row['resolution_id']} {name}: parse {parsed}, card {read}"
                )
        if row["unsupported"] is not None:
            judged += 1
            claims = json.loads(row["unsupported"])
            faithful += not claims
            unsupported.update(claim["field"] for claim in claims)
    current = sum(row["prompt_version"] == PROMPT_VERSION for row in rows)
    lines = [
        f"cards: {len(rows)} ({current} current), US${sum(r['usd'] for r in rows):.4f}",
        "fields: " + ", ".join(f"{name} {fields[name]}" for name in FIELDS),
        f"dropped: {dropped_quotes} quotes, {dropped_fields} fields with no quote"
        f" left, {dropped_readings} readings",
        f"judged: {judged}, faithful {faithful}"
        + (f" ({faithful / judged:.1%})" if judged else ""),
        "unsupported by field: "
        + (
            ", ".join(f"{n} {unsupported[n]}" for n in FIELDS if unsupported[n])
            or "none"
        ),
        f"disagreements with parse: {len(disagree)}",
        *(f"  {line}" for line in disagree),
        f"readings where parse has none: {len(filled)}",
        *(f"  {line}" for line in filled),
    ]
    return "\n".join(lines)


def _same(name: str, parsed: Any, read: str) -> bool:
    if name == "resolved_on":
        return isinstance(parsed, date) and parsed.isoformat() == read
    if name == "operator":
        return _letters(str(parsed)) == _letters(read)
    return parsed == read


def _letters(name: str) -> str:
    return re.sub(r"[^a-z]", "", normalize(name))
