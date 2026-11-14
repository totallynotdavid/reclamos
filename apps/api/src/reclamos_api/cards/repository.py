"""The `cards` table."""

import hashlib
import json
from dataclasses import asdict, dataclass
from typing import Any

from reclamos_api.cards.card import PROMPT_VERSION, Card, Field


@dataclass(frozen=True, slots=True)
class Source:
    resolution_id: int
    body: str
    body_sha256: str


def body_sha256(body: str) -> str:
    return hashlib.sha256(body.encode()).hexdigest()


async def stale_sources(connection: Any) -> list[Source]:
    """Resolutions with a body and no current card, smallest id first."""
    rows = await connection.fetch(
        """
        SELECT r.id, r.body, c.prompt_version, c.body_sha256
        FROM resolutions r LEFT JOIN cards c ON c.resolution_id = r.id
        WHERE r.body IS NOT NULL AND r.body <> ''
        ORDER BY r.id
        """
    )
    sources: list[Source] = []
    for row in rows:
        digest = body_sha256(row["body"])
        if row["prompt_version"] != PROMPT_VERSION or row["body_sha256"] != digest:
            sources.append(Source(row["id"], row["body"], digest))
    return sources


async def save_card(
    connection: Any, source: Source, card: Card, *, model: str, usd: float
) -> None:
    """Write the card, replacing an older one and its verdict."""
    await connection.execute(
        """
        INSERT INTO cards (resolution_id, prompt_version, body_sha256, model,
                           fields, readings, rejected, usd)
        VALUES ($1, $2, $3, $4, $5::jsonb, $6::jsonb, $7::jsonb, $8::float8)
        ON CONFLICT (resolution_id) DO UPDATE SET
            prompt_version = excluded.prompt_version,
            body_sha256 = excluded.body_sha256,
            model = excluded.model,
            fields = excluded.fields,
            readings = excluded.readings,
            rejected = excluded.rejected,
            usd = excluded.usd,
            written_at = now(),
            judge_model = NULL,
            unsupported = NULL,
            judged_at = NULL
        """,
        source.resolution_id,
        PROMPT_VERSION,
        source.body_sha256,
        model,
        json.dumps({name: asdict(f) for name, f in card.fields.items()}),
        json.dumps({name: asdict(r) for name, r in card.readings.items()}),
        json.dumps(card.rejected),
        usd,
    )


async def unjudged(connection: Any) -> list[tuple[int, dict[str, Field]]]:
    """Cards with at least one field and no verdict, smallest id first."""
    rows = await connection.fetch(
        """
        SELECT resolution_id, fields FROM cards
        WHERE judged_at IS NULL AND fields <> '{}'::jsonb
        ORDER BY resolution_id
        """
    )
    return [
        (
            row["resolution_id"],
            {
                name: Field(value["text"], tuple(value["quotes"]))
                for name, value in json.loads(row["fields"]).items()
            },
        )
        for row in rows
    ]


async def save_verdict(
    connection: Any,
    resolution_id: int,
    unsupported: list[dict[str, str]],
    *,
    judge_model: str,
) -> None:
    await connection.execute(
        """
        UPDATE cards
        SET judge_model = $2, unsupported = $3::jsonb, judged_at = now()
        WHERE resolution_id = $1
        """,
        resolution_id,
        judge_model,
        json.dumps(unsupported),
    )


async def report_rows(connection: Any) -> list[Any]:
    """Every card beside what parse read from the same resolution."""
    return await connection.fetch(
        """
        SELECT c.resolution_id, c.prompt_version, c.fields, c.readings, c.rejected,
               c.usd::float8 AS usd, c.unsupported,
               r.outcome, r.operator, r.resolved_on
        FROM cards c JOIN resolutions r ON r.id = c.resolution_id
        ORDER BY c.resolution_id
        """
    )
