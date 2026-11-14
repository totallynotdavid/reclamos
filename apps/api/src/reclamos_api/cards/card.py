"""Write one resolution's case card with a language model, and check it.

A card holds four fields a reader needs to compare their case with this one,
and the model's own reading of the outcome, the operator and the date. Each
field and reading carries quotes from the body. A quote that is not in the body
word for word is dropped, and a field with no quote left is dropped with it.
"""

import hashlib
import json
from dataclasses import dataclass, field
from datetime import date
from typing import Any, cast

from reclamos_api.llm.client import LanguageModel, Prompt
from reclamos_api.llm.quotes import is_quoted
from reclamos_api.llm.spend import Budget, Ledger

# The longest body in the corpus is 47,053 characters (2026-11-14).
MAX_CARD_PROMPT_CHARS = 60_000
MAX_CARD_TOKENS = 3_000
CARD_TIMEOUT_S = 180.0
# Quotes kept per field. More would make the card harder to read, not truer.
MAX_QUOTES = 3

FIELDS = ("claim", "operator_position", "reasoning", "evidence")
READINGS = ("outcome", "operator", "resolved_on")
OUTCOMES = (
    "fundado",
    "infundado",
    "fundado_en_parte",
    "improcedente",
    "mixto",
    "sin_efecto",
)

SYSTEM = """\
Lees resoluciones del TRASU, el tribunal de OSIPTEL que resuelve las \
apelaciones y quejas de usuarios de telecomunicaciones del Perú. De cada \
resolución escribes una ficha para que un usuario compare su caso con este.

Campos de la ficha, cada uno en una o dos oraciones de español sencillo:
- claim: qué reclamó el usuario y qué pidió.
- operator_position: qué dijo o decidió la empresa operadora antes de la \
apelación o la queja.
- reasoning: por qué el tribunal decidió lo que decidió.
- evidence: la prueba que decidió el caso, o la prueba que faltó.

Reglas:
- Usa solo el texto de la resolución. No uses otro conocimiento.
- Cada campo dice solo lo que dicen sus citas. No agregues motivos, detalles \
ni consejos.
- Atribuye cada cosa a quien la dijo: el usuario, la empresa o el tribunal.
- Cada cita copia, carácter por carácter, una o dos oraciones del texto. No \
corrijas errores de escritura ni de OCR, y no cambies palabras.
- Si el texto no dice algo que un campo pide, deja su "text" vacío y sus \
"quotes" vacías.

Además lee del texto:
- outcome: lo que el tribunal declaró sobre la apelación o la queja en la \
parte resolutiva. Usa "mixto" si declaró cosas distintas sobre partes del \
caso.
- operator: el nombre de la empresa operadora como aparece en el texto.
- resolved_on: la fecha de la resolución, en formato AAAA-MM-DD.
Cada lectura lleva la cita del texto que la muestra. Si el texto no la \
muestra, deja "value" y "quote" vacíos.
"""


def _field_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "text": {"type": "string"},
            "quotes": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["text", "quotes"],
        "additionalProperties": False,
    }


def _reading_schema(value: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {"value": value, "quote": {"type": "string"}},
        "required": ["value", "quote"],
        "additionalProperties": False,
    }


SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        **{name: _field_schema() for name in FIELDS},
        "outcome": _reading_schema({"type": "string", "enum": [*OUTCOMES, ""]}),
        "operator": _reading_schema({"type": "string"}),
        "resolved_on": _reading_schema({"type": "string"}),
    },
    "required": [*FIELDS, *READINGS],
    "additionalProperties": False,
}

# A card written with another prompt or schema is out of date.
PROMPT_VERSION = hashlib.sha256(
    (SYSTEM + json.dumps(SCHEMA, sort_keys=True)).encode()
).hexdigest()[:12]


@dataclass(frozen=True, slots=True)
class Field:
    text: str
    quotes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Reading:
    value: str
    quote: str


@dataclass(slots=True)
class Card:
    fields: dict[str, Field] = field(default_factory=dict[str, Field])
    readings: dict[str, Reading] = field(default_factory=dict[str, Reading])
    # What the check dropped: quotes not in the body, fields left with no
    # quote, and readings whose value or quote failed.
    rejected: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])


class Unreadable(Exception):
    """The reply was cut at the token cap or is not JSON. Its cost is spent."""

    def __init__(self, reason: str, usd: float) -> None:
        super().__init__(reason)
        self.usd = usd


async def write_card(
    ledger: Ledger,
    model: LanguageModel,
    card_model: str,
    body: str,
    *,
    budget: Budget | None = None,
) -> tuple[Card, float]:
    """The checked card for `body` and what the call cost."""
    metered = await ledger.complete(
        model,
        Prompt(
            model=card_model,
            system=SYSTEM,
            text=f"Texto de la resolución:\n\n{body}",
            schema=SCHEMA,
            max_tokens=MAX_CARD_TOKENS,
            timeout_s=CARD_TIMEOUT_S,
        ),
        purpose="card",
        budget=budget,
        max_prompt_chars=MAX_CARD_PROMPT_CHARS,
    )
    if metered.completion.stop_reason == "max_tokens":
        raise Unreadable("cut at the token cap", metered.usd)
    reply = metered.completion.parsed()
    if reply is None:
        raise Unreadable("not a JSON object", metered.usd)
    return check_card(reply, body), metered.usd


def check_card(reply: dict[str, Any], body: str) -> Card:
    """Keep what the body backs word for word."""
    card = Card()
    for name in FIELDS:
        entry = _object(reply.get(name))
        text = str(entry.get("text", "")).strip()
        raw: Any = entry.get("quotes")
        quotes = (
            [str(q).strip() for q in cast("list[Any]", raw)]
            if isinstance(raw, list)
            else []
        )
        kept: list[str] = []
        for quote in quotes:
            if is_quoted(quote, body) and len(kept) < MAX_QUOTES:
                kept.append(quote)
            elif quote:
                card.rejected.append({"field": name, "quote": quote})
        if text and kept:
            card.fields[name] = Field(text, tuple(kept))
        elif text:
            card.rejected.append({"field": name, "text": text})
    for name in READINGS:
        entry = _object(reply.get(name))
        value = str(entry.get("value", "")).strip()
        quote = str(entry.get("quote", "")).strip()
        if not value:
            continue
        if _valid_reading(name, value) and is_quoted(quote, body):
            card.readings[name] = Reading(value, quote)
        else:
            card.rejected.append({"reading": name, "value": value, "quote": quote})
    return card


def _valid_reading(name: str, value: str) -> bool:
    if name == "outcome":
        return value in OUTCOMES
    if name == "resolved_on":
        try:
            date.fromisoformat(value)
        except ValueError:
            return False
    return True


def _object(value: Any) -> dict[str, Any]:
    return cast("dict[str, Any]", value) if isinstance(value, dict) else {}
