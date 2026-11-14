"""Judge whether each field of a card says only what its quotes say.

The quotes passed the check against the body, so a field its quotes back is
backed by the resolution. The judge reads the fields and their quotes only,
not the whole body.
"""

from dataclasses import dataclass
from typing import Any, cast

from reclamos_api.cards.card import FIELDS, Field
from reclamos_api.llm.client import LanguageModel, Prompt
from reclamos_api.llm.spend import Budget, Ledger

MAX_JUDGE_TOKENS = 1_000

SYSTEM = """\
Revisas fichas escritas a partir de resoluciones del TRASU de OSIPTEL.

Cada campo de la ficha tiene un texto y citas copiadas de la resolución:
- claim: qué reclamó el usuario y qué pidió.
- operator_position: qué dijo o decidió la empresa operadora antes de la \
apelación o la queja.
- reasoning: por qué el tribunal decidió lo que decidió.
- evidence: la prueba que decidió el caso, o la prueba que faltó.

Un campo es fiel cuando cada afirmación de su texto está en sus citas y la \
atribuye a quien la dijo en ellas: el usuario, la empresa o el tribunal. \
Conocimiento que no está en las citas no lo respalda, aunque sea cierto.

En "unsupported" pon, por cada afirmación que sus citas no respaldan, el \
campo y la afirmación copiada. Si todo es fiel, deja "unsupported" vacío.
"""

SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "unsupported": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "field": {"type": "string", "enum": list(FIELDS)},
                    "claim": {"type": "string"},
                },
                "required": ["field", "claim"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["unsupported"],
    "additionalProperties": False,
}


@dataclass(frozen=True, slots=True)
class CardVerdict:
    # None when the judge's reply could not be read.
    unsupported: list[dict[str, str]] | None
    usd: float


async def judge_card(
    ledger: Ledger,
    model: LanguageModel,
    judge_model: str,
    fields: dict[str, Field],
    *,
    budget: Budget | None = None,
) -> CardVerdict:
    metered = await ledger.complete(
        model,
        Prompt(
            model=judge_model,
            system=SYSTEM,
            text=card_text(fields),
            schema=SCHEMA,
            max_tokens=MAX_JUDGE_TOKENS,
        ),
        purpose="judge",
        budget=budget,
    )
    reply = metered.completion.parsed()
    items: Any = None if reply is None else reply.get("unsupported")
    if metered.completion.stop_reason == "max_tokens" or not isinstance(items, list):
        return CardVerdict(None, metered.usd)
    claims = [
        cast("dict[str, Any]", item)
        for item in cast("list[Any]", items)
        if isinstance(item, dict)
    ]
    unsupported = [
        {"field": str(claim.get("field", "")), "claim": str(claim.get("claim", ""))}
        for claim in claims
    ]
    return CardVerdict(unsupported, metered.usd)


def card_text(fields: dict[str, Field]) -> str:
    blocks: list[str] = []
    for name in FIELDS:
        if name not in fields:
            continue
        quotes = "\n".join(f"- «{quote}»" for quote in fields[name].quotes)
        blocks.append(f"{name}: {fields[name].text}\nCitas:\n{quotes}")
    return "\n\n".join(blocks)
